"""From a config entry plus parsed API data to the model's SpotContext (the HA <-> model seam).

Pure Python on purpose: everything Home Assistant specific (entries, coordinators, storage) lives
elsewhere and calls this with plain values, so the mapping can be tested with recorded responses.

Decisions made here, with the reason:

* **The zone is kept by name** (`tz_name`), so a DST change inside the forecast window stays right.
  Only when the name cannot be resolved (no tz database) is the response's fixed offset used,
  which is correct for today and off by an hour across a DST edge - better than silently UTC.
* **Pressure comes from one flavour only** (`pressure_source`): station and sea-level pressure are
  never mixed in one series, because the model's tendency and anomaly assume a homogeneous one.
* **The diurnal pressure tide is learned from the past only** (never from forecast hours), so the
  correction reflects what the barometer did, not what the model predicts it will do.
* A missing marine or flood answer is simply absent data (no SST, no discharge): the model drops
  those factors and says so, it never gets a guessed value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from . import const
from .api.open_meteo import Forecast, Marine, Sample, value_at
from .model import climatology as cl
from .model import hydro, pressure
from .model.context import SERIES_NAMES, ShelterGeometry, SpotContext
from .model.water_temp import BODY_CLASSES, WaterBody


@dataclass
class SpotConfig:
    """The user's description of one fishing mark (one config entry = one spot)."""

    name: str
    latitude: float
    longitude: float
    elevation_m: float | None = None
    spot_kind: str = const.DEFAULT_SPOT_KIND
    water_body: str = const.DEFAULT_WATER_BODY
    tidal: bool = False
    tide_range_m: float = 0.0
    catchment: str | float = const.DEFAULT_CATCHMENT
    pressure_source: str = const.DEFAULT_PRESSURE_SOURCE
    shelter: dict[str, float] = field(default_factory=dict)
    bank_aspect_deg: float | None = None
    species: str = const.DEFAULT_SPECIES
    water_temp_entity: str | None = None

    @classmethod
    def from_entry(cls, data: Mapping[str, Any], options: Mapping[str, Any] | None = None) -> "SpotConfig":
        """Entry data overlaid with options; unknown or invalid values fall back to defaults."""
        merged = {**data, **(options or {})}

        def pick(key: str, allowed: tuple[str, ...], default: str) -> str:
            value = merged.get(key, default)
            return value if value in allowed else default

        shelter = merged.get(const.CONF_SHELTER) or {}
        aspect = merged.get(const.CONF_BANK_ASPECT)
        elevation = merged.get(const.CONF_ELEVATION)
        return cls(
            name=str(merged.get(const.CONF_NAME) or const.NAME),
            latitude=float(merged[const.CONF_LATITUDE]),
            longitude=float(merged[const.CONF_LONGITUDE]),
            elevation_m=None if elevation in (None, "") else float(elevation),
            spot_kind=pick(const.CONF_SPOT_KIND, const.SPOT_KINDS, const.DEFAULT_SPOT_KIND),
            water_body=pick(const.CONF_WATER_BODY, tuple(BODY_CLASSES), const.DEFAULT_WATER_BODY),
            tidal=bool(merged.get(const.CONF_TIDAL, False)),
            tide_range_m=max(0.0, float(merged.get(const.CONF_TIDE_RANGE) or 0.0)),
            catchment=merged.get(const.CONF_CATCHMENT) or const.DEFAULT_CATCHMENT,
            pressure_source=pick(const.CONF_PRESSURE_SOURCE, const.PRESSURE_SOURCES, const.DEFAULT_PRESSURE_SOURCE),
            shelter={str(k): float(v) for k, v in dict(shelter).items()},
            bank_aspect_deg=None if aspect in (None, "") else float(aspect),
            species=str(merged.get(const.CONF_SPECIES) or const.DEFAULT_SPECIES),
            water_temp_entity=str(merged.get(const.CONF_WATER_TEMP_ENTITY) or "") or None,
        )

    @property
    def catchment_response(self) -> float:
        if isinstance(self.catchment, (int, float)):
            return min(1.0, max(0.0, float(self.catchment)))
        return hydro.CATCHMENT_RESPONSES.get(str(self.catchment), hydro.CATCHMENT_RESPONSES[const.DEFAULT_CATCHMENT])


@dataclass
class Readings:
    """Live sensor values the user pointed the integration at (all optional)."""

    water_temp: float | None = None
    water_temp_age_hours: float = 0.0


def to_celsius(value: float, unit: str | None) -> float | None:
    """A temperature reading in degC, whatever unit the user's sensor reports; None if unusable."""
    if unit in (None, "", "°C", "C", "degC"):
        return value
    if unit in ("°F", "F", "degF"):
        return (value - 32.0) / 1.8
    if unit in ("K", "Kelvin"):
        return value - 273.15
    return None


def resolve_timezone(forecast: Forecast) -> tuple[str, int | None]:
    """(tz_name, fixed offset minutes or None): the offset is only a fallback for a missing tz database."""
    name = forecast.timezone or "UTC"
    if name in ("UTC", "Etc/UTC", "GMT", "Etc/GMT"):
        return "UTC", None                      # needs no database, whatever offset the response quotes
    try:
        ZoneInfo(name)
        return name, None
    except Exception:
        if forecast.utc_offset_seconds is not None:
            return name, int(round(forecast.utc_offset_seconds / 60.0))
        return "UTC", None


def local_now(forecast: Forecast, utc_now: datetime) -> datetime:
    """The spot's wall clock as a naive datetime (the convention of every series in the model)."""
    name, offset = resolve_timezone(forecast)
    utc = utc_now.astimezone(timezone.utc)
    if offset is not None:
        return (utc + timedelta(minutes=offset)).replace(tzinfo=None)
    if name == "UTC":
        return utc.replace(tzinfo=None)
    return utc.astimezone(ZoneInfo(name)).replace(tzinfo=None)


def new_climatology(cfg: SpotConfig, elevation_m: float) -> cl.Climatology:
    return cl.Climatology(cfg.latitude, elevation_m, cfg.name)


def build_context(
    cfg: SpotConfig,
    forecast: Forecast,
    *,
    now: datetime,
    marine: Marine | None = None,
    flood: list[Sample] | None = None,
    clim: cl.Climatology | None = None,
    readings: Readings | None = None,
) -> SpotContext:
    """Assemble everything the scoring engine reads. `now` is the spot-local wall clock."""
    readings = readings or Readings()
    tz_name, tz_offset = resolve_timezone(forecast)
    elevation = cfg.elevation_m if cfg.elevation_m is not None else (forecast.elevation_m or 0.0)

    ctx = SpotContext(
        name=cfg.name,
        lat=cfg.latitude,
        lon=cfg.longitude,
        elevation_m=elevation,
        tz_name=tz_name,
        tz_offset_min=tz_offset,
        spot_kind=cfg.spot_kind,
        tidal=cfg.tidal,
        tide_range_m=cfg.tide_range_m,
        water_body=WaterBody(kind=cfg.water_body),
        catchment_response=cfg.catchment_response,
        pressure_source=cfg.pressure_source,
        shelter=ShelterGeometry.from_dict({"exposure": cfg.shelter, "bank_aspect_deg": cfg.bank_aspect_deg}),
        clim=clim,
        water_sensor=readings.water_temp,
        water_sensor_age_hours=readings.water_temp_age_hours,
    )
    for name, series in forecast.series.items():
        if name in SERIES_NAMES:
            ctx.series[name] = series
    if flood:
        ctx.series["discharge"] = flood
    if marine is not None and marine.has_sea_surface_temp:
        ctx.sea_surface_temp = value_at(marine.sea_surface_temp, now, max_age_hours=6.0)

    past_pressure = [(t, v) for t, v in forecast.series.get("pressure", []) if t <= now]
    ctx.diurnal_pressure_cycle = pressure.build_diurnal_cycle(past_pressure)
    return ctx


def ingest_into(clim: cl.Climatology, rows: list[tuple[Any, dict[str, float]]]) -> int:
    """Feed archive rows into a climatology; returns how many individual values were ingested."""
    return clim.ingest_series(rows)
