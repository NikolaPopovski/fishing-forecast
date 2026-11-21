"""The coordinator: network on a slow clock, scoring on a fast one.

Home Assistant calls `_async_update_data` every few minutes. Most calls touch no network: they
re-score the cached forecast for "now" (the score drifts with the sun and the moon even when the
weather has not changed). The forecast itself is refetched every `UPDATE_INTERVAL`, and a failed
refresh keeps serving the last good forecast - flagged stale - rather than blanking the spot.

What to do when is decided by pure functions in `refresh.py`; the arithmetic is in `snapshot.py`
and `model/`. This class only moves data between Home Assistant and them.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import const, refresh, snapshot, spot
from .api.open_meteo import Forecast, Marine, OpenMeteoError, Sample
from .transport import client_for
from .model import climatology as cl
from .model import scoring
from .model.context import SpotContext

_LOGGER = logging.getLogger(__name__)

#: how soon to retry after a failed fetch (sooner than the normal interval: a blip should not cost 30 min)
RETRY_AFTER = timedelta(minutes=10)


class FishingCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """One spot: its forecast, its local normals and its latest snapshot."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.cfg = spot.SpotConfig.from_entry(entry.data, entry.options)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{const.DOMAIN} {self.cfg.name}",
            update_interval=const.RESCORE_INTERVAL,
        )
        self.client = client_for(hass)
        self._store: Store[dict[str, Any]] = Store(hass, const.STORAGE_VERSION, f"{const.DOMAIN}.climatology_{entry.entry_id}")
        self.clim: cl.Climatology | None = None
        self.ctx: SpotContext | None = None
        self._forecast: Forecast | None = None
        self._marine: Marine | None = None
        self._flood: list[Sample] | None = None
        self._last_attempt: datetime | None = None
        self._last_success: datetime | None = None
        self._last_error: str | None = None

    # -- Home Assistant hooks ----------------------------------------------------------

    async def _async_setup(self) -> None:
        """Load the persisted local normals before the first refresh."""
        stored = await self._store.async_load()
        if isinstance(stored, dict) and stored:
            self.clim = cl.Climatology.from_dict(stored)

    async def _async_update_data(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        if refresh.needs_fetch(self._last_attempt, now):
            await self._async_fetch(now)

        state = refresh.data_state(self._last_success, now)
        if state in ("none", "expired") or self._forecast is None:
            raise UpdateFailed(self._last_error or "no forecast available yet")

        local = spot.local_now(self._forecast, now)
        readings = self._read_water_probe(now)
        return await self.hass.async_add_executor_job(self._compute, local, readings, state)

    # -- network -----------------------------------------------------------------------

    async def _async_fetch(self, now: datetime) -> None:
        cfg = self.cfg
        self._last_attempt = now
        try:
            forecast = await self.client.forecast(
                cfg.latitude, cfg.longitude, past_days=const.PAST_DAYS, forecast_days=const.FORECAST_DAYS, pressure_source=cfg.pressure_source
            )
        except OpenMeteoError as err:
            self._last_error = str(err)
            self._last_attempt = now - (const.UPDATE_INTERVAL - RETRY_AFTER)
            _LOGGER.warning("Could not refresh the forecast for %s: %s", cfg.name, err)
            return

        marine = flood = None
        if cfg.tidal or cfg.spot_kind in ("coast", "estuary"):
            marine = await self.client.marine(cfg.latitude, cfg.longitude, past_days=const.PAST_DAYS, forecast_days=const.FORECAST_DAYS)
        if cfg.spot_kind in ("river", "stream", "estuary"):
            flood = await self.client.flood(cfg.latitude, cfg.longitude, past_days=const.PAST_DAYS, forecast_days=const.FORECAST_DAYS)

        self._forecast, self._marine, self._flood = forecast, marine, flood
        self._last_success = now
        self._last_error = None
        self.ctx = None                                    # rebuilt from the new data on the next compute
        if self.clim is None:
            elevation = cfg.elevation_m if cfg.elevation_m is not None else (forecast.elevation_m or 0.0)
            self.clim = spot.new_climatology(cfg, elevation)

    async def async_force_refresh(self) -> None:
        """The `fishing.refresh` service: fetch now, whatever the clock says."""
        self._last_attempt = None
        await self.async_request_refresh()

    # -- live readings -----------------------------------------------------------------

    def _read_water_probe(self, now: datetime) -> spot.Readings:
        """The user's water-temperature sensor, in degC, with how old the reading is."""
        entity_id = self.cfg.water_temp_entity
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return spot.Readings()
        try:
            value = float(state.state)
        except ValueError:
            return spot.Readings()
        celsius = spot.to_celsius(value, state.attributes.get("unit_of_measurement"))
        if celsius is None:
            return spot.Readings()
        return spot.Readings(water_temp=celsius, water_temp_age_hours=max(0.0, (now - state.last_updated).total_seconds() / 3600.0))

    # -- computation (runs in the executor: the ephemeris is CPU work) ---------------------

    def _compute(self, local: datetime, readings: spot.Readings, state: str) -> dict[str, Any]:
        assert self._forecast is not None
        if self.ctx is None:
            self.ctx = spot.build_context(
                self.cfg, self._forecast, now=local, marine=self._marine, flood=self._flood, clim=self.clim, readings=readings
            )
        else:
            self.ctx.water_sensor = readings.water_temp
            self.ctx.water_sensor_age_hours = readings.water_temp_age_hours
            self.ctx.clim = self.clim
        data = snapshot.compute(self.ctx, local, self.cfg.species, hours=const.FORECAST_HOURS)
        data["network"] = {
            "state": state,
            "last_success": None if self._last_success is None else self._last_success.isoformat(),
            "error": self._last_error,
        }
        return data

    # -- the user's choice of species --------------------------------------------------

    async def async_set_species(self, species_id: str) -> None:
        self.cfg.species = species_id
        self.hass.config_entries.async_update_entry(self.config_entry, options={**self.config_entry.options, const.CONF_SPECIES: species_id})
        await self.async_request_refresh()

    # -- local normals -----------------------------------------------------------------

    async def async_update_climatology(self, _now: datetime | None = None) -> None:
        """Top up the local normals: archive days not yet ingested, and the modelled water temperature."""
        clim, forecast = self.clim, self._forecast
        if clim is None or forecast is None:
            return
        today = dt_util.now().date()
        try:
            window = refresh.archive_window(refresh.parse_day(clim.updated.get("all")), today)
            if window is not None:
                rows = await self.client.archive(self.cfg.latitude, self.cfg.longitude, *window)
                ingested = await self.hass.async_add_executor_job(spot.ingest_into, clim, rows)
                _LOGGER.debug("Ingested %s archive values into the %s normals", ingested, self.cfg.name)
            if self.ctx is not None:
                water = refresh.daily_rows(scoring.water_model_series(self.ctx), "water_temp", refresh.parse_day(clim.updated.get("water_temp")), today)
                if water:
                    await self.hass.async_add_executor_job(spot.ingest_into, clim, water)
                    clim.updated["water_temp"] = water[-1][0].isoformat()
        except OpenMeteoError as err:
            _LOGGER.debug("Could not update the local normals for %s: %s", self.cfg.name, err)
        await self._store.async_save(clim.to_dict())
