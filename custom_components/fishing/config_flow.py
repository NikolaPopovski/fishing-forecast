"""Config flow: find the place, describe the water, choose the fish. And an options flow to change it.

Setup, in plain terms:

1. **Where?** Type a place name (searched with Open-Meteo's geocoder) or leave it empty to enter
   coordinates. If the search finds nothing the user is told so and can try again or go manual.
2. **What water?** Spot kind, water body, tide, catchment, elevation, pressure source, and
   optionally a real water-temperature probe. Everything has a sensible default: a lake,
   a small one, mixed farmland round it.
3. **What fish?** The species the whole spot is scored for (changeable any time from the
   `select` entity).

One config entry is one fishing spot; entries are identified by their rounded coordinates so the
same mark cannot be added twice.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import const
from .api.open_meteo import OpenMeteoError, Place
from .model import hydro
from .model import species as sp
from .model.context import SECTOR_ORDER
from .model.water_temp import BODY_CLASSES
from .transport import client_for

_LOGGER = logging.getLogger(__name__)

MANUAL = "manual"


def _dropdown(options: tuple[str, ...], translation_key: str) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(options=list(options), mode=selector.SelectSelectorMode.DROPDOWN, translation_key=translation_key)
    )


def _number(minimum: float, maximum: float, step: float, unit: str | None = None) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=selector.NumberSelectorMode.BOX, unit_of_measurement=unit)
    )


def species_selector() -> selector.SelectSelector:
    names = sp.species_names()
    options = [selector.SelectOptionDict(value=sid, label=name) for sid, name in sorted(names.items(), key=lambda kv: kv[1].lower())]
    return selector.SelectSelector(selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN))


def water_schema(current: Mapping[str, Any], *, with_species: bool = False) -> vol.Schema:
    """The questions about the water (shared by setup and the options flow).

    Optional numbers use `suggested_value` so a cleared field really is cleared, instead of a
    stale default silently coming back.
    """
    def suggest(key: str) -> dict[str, Any]:
        return {"suggested_value": current.get(key)}

    fields: dict[Any, Any] = {}
    if with_species:
        fields[vol.Required(const.CONF_SPECIES, default=current.get(const.CONF_SPECIES, const.DEFAULT_SPECIES))] = species_selector()
    fields.update(
        {
            vol.Required(const.CONF_SPOT_KIND, default=current.get(const.CONF_SPOT_KIND, const.DEFAULT_SPOT_KIND)): _dropdown(const.SPOT_KINDS, "spot_kind"),
            vol.Required(const.CONF_WATER_BODY, default=current.get(const.CONF_WATER_BODY, const.DEFAULT_WATER_BODY)): _dropdown(tuple(BODY_CLASSES), "water_body"),
            vol.Required(const.CONF_TIDAL, default=bool(current.get(const.CONF_TIDAL, False))): selector.BooleanSelector(),
            vol.Optional(const.CONF_TIDE_RANGE, description=suggest(const.CONF_TIDE_RANGE)): _number(0, 15, 0.1, "m"),
            vol.Required(const.CONF_CATCHMENT, default=current.get(const.CONF_CATCHMENT, const.DEFAULT_CATCHMENT)): _dropdown(tuple(hydro.CATCHMENT_RESPONSES), "catchment"),
            vol.Optional(const.CONF_ELEVATION, description=suggest(const.CONF_ELEVATION)): _number(-100, 6000, 1, "m"),
            vol.Required(const.CONF_PRESSURE_SOURCE, default=current.get(const.CONF_PRESSURE_SOURCE, const.DEFAULT_PRESSURE_SOURCE)): _dropdown(const.PRESSURE_SOURCES, "pressure_source"),
            vol.Optional(const.CONF_WATER_TEMP_ENTITY, description=suggest(const.CONF_WATER_TEMP_ENTITY)): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
        }
    )
    return vol.Schema(fields)


def shelter_schema(current: Mapping[str, Any]) -> vol.Schema:
    """How exposed the bank is to each wind direction (0 = fully sheltered, 1 = full fetch)."""
    exposure = current.get(const.CONF_SHELTER) or {}
    fields: dict[Any, Any] = {
        vol.Optional(sector, default=float(exposure.get(sector, 0.5))): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=1, step=0.05, mode=selector.NumberSelectorMode.SLIDER)
        )
        for sector in SECTOR_ORDER
    }
    fields[vol.Optional(const.CONF_BANK_ASPECT, description={"suggested_value": current.get(const.CONF_BANK_ASPECT)})] = _number(0, 359, 1, "°")
    return vol.Schema(fields)


def _clean_water(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Form output as stored values: blank optionals become explicit None so they override older ones."""
    out = dict(user_input)
    for key in (const.CONF_TIDE_RANGE, const.CONF_ELEVATION, const.CONF_WATER_TEMP_ENTITY):
        if out.get(key) in ("", None):
            out[key] = None
    return out


class FishingConfigFlow(ConfigFlow, domain=const.DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._places: list[Place] = []
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> FishingOptionsFlow:
        return FishingOptionsFlow()

    # -- 1. where ---------------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            query = (user_input.get("place") or "").strip()
            if not query:
                return await self.async_step_manual()
            try:
                self._places = await client_for(self.hass).geocode(query)
            except OpenMeteoError:
                errors["base"] = "cannot_connect"
            else:
                if self._places:
                    return await self.async_step_pick()
                errors["base"] = "no_results"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Optional("place"): selector.TextSelector()}),
            errors=errors,
        )

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            choice = user_input["place"]
            if choice == MANUAL:
                return await self.async_step_manual()
            place = self._places[int(choice)]
            self._data.update({const.CONF_NAME: place.name, const.CONF_LATITUDE: place.latitude, const.CONF_LONGITUDE: place.longitude})
            if place.elevation_m is not None:
                self._data[const.CONF_ELEVATION] = place.elevation_m
            return await self.async_step_spot()
        options = [selector.SelectOptionDict(value=str(i), label=p.label) for i, p in enumerate(self._places)]
        options.append(selector.SelectOptionDict(value=MANUAL, label="Enter coordinates manually"))
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {vol.Required("place"): selector.SelectSelector(selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.LIST))}
            ),
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(
                {
                    const.CONF_NAME: self._data.get(const.CONF_NAME) or const.NAME,
                    const.CONF_LATITUDE: float(user_input[const.CONF_LATITUDE]),
                    const.CONF_LONGITUDE: float(user_input[const.CONF_LONGITUDE]),
                }
            )
            return await self.async_step_spot()
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(const.CONF_LATITUDE, default=self.hass.config.latitude): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=-90, max=90, step="any", mode=selector.NumberSelectorMode.BOX)
                    ),
                    vol.Required(const.CONF_LONGITUDE, default=self.hass.config.longitude): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=-180, max=180, step="any", mode=selector.NumberSelectorMode.BOX)
                    ),
                }
            ),
        )

    # -- 2. what water -----------------------------------------------------------------

    async def async_step_spot(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            name = (user_input.pop(const.CONF_NAME, "") or self._data.get(const.CONF_NAME) or const.NAME).strip()
            self._data.update(_clean_water(user_input))
            self._data[const.CONF_NAME] = name
            await self.async_set_unique_id(f"{self._data[const.CONF_LATITUDE]:.4f}_{self._data[const.CONF_LONGITUDE]:.4f}")
            self._abort_if_unique_id_configured()
            return await self.async_step_species()
        schema = water_schema(self._data).extend(
            {vol.Required(const.CONF_NAME, default=self._data.get(const.CONF_NAME) or const.NAME): selector.TextSelector()}
        )
        return self.async_show_form(step_id="spot", data_schema=schema)

    # -- 3. what fish -------------------------------------------------------------------

    async def async_step_species(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=self._data[const.CONF_NAME],
                data=self._data,
                options={const.CONF_SPECIES: user_input[const.CONF_SPECIES]},
            )
        return self.async_show_form(
            step_id="species",
            data_schema=vol.Schema({vol.Required(const.CONF_SPECIES, default=const.DEFAULT_SPECIES): species_selector()}),
        )


class FishingOptionsFlow(OptionsFlowWithReload):
    """Change the water, the species or the bank's exposure; the entry reloads on save."""

    def __init__(self) -> None:
        self._pending: dict[str, Any] = {}

    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._pending = _clean_water(user_input)
            return await self.async_step_shelter()
        return self.async_show_form(step_id="init", data_schema=water_schema(self._current(), with_species=True))

    async def async_step_shelter(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            aspect = user_input.get(const.CONF_BANK_ASPECT)
            shelter = {sector: float(user_input.get(sector, 0.5)) for sector in SECTOR_ORDER}
            return self.async_create_entry(
                data={**self._pending, const.CONF_SHELTER: shelter, const.CONF_BANK_ASPECT: None if aspect in ("", None) else float(aspect)}
            )
        return self.async_show_form(step_id="shelter", data_schema=shelter_schema(self._current()))
