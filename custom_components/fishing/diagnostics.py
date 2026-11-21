"""Diagnostics: what the integration knows about itself, with the location removed."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import FishingConfigEntry, const

#: where the user fishes is theirs; the numbers are what a bug report needs
TO_REDACT = {const.CONF_LATITUDE, const.CONF_LONGITUDE, "lat", "lon", const.CONF_WATER_TEMP_ENTITY}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: FishingConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    snapshot = dict(coordinator.data or {})
    forecast = snapshot.pop("forecast", None)              # 48 points would drown everything else
    return {
        "entry": async_redact_data({**entry.data}, TO_REDACT),
        "options": async_redact_data({**entry.options}, TO_REDACT),
        "snapshot": async_redact_data(snapshot, TO_REDACT),
        "forecast_points": 0 if forecast is None else len(forecast),
        "climatology": None if coordinator.clim is None else coordinator.clim.health(),
        "last_update_success": coordinator.last_update_success,
    }
