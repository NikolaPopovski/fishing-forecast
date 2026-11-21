"""Fishing Forecast: how good is the fishing right now, and why - for one mark per config entry."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from . import const
from .card import async_register_card
from .coordinator import FishingCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT]

SERVICE_REFRESH = "refresh"

type FishingConfigEntry = ConfigEntry[FishingCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the domain-level service once (services belong here, not in a config entry)."""

    async def _refresh(call: ServiceCall) -> None:
        """Fetch fresh weather now and re-score, for every configured spot."""
        for entry in hass.config_entries.async_loaded_entries(const.DOMAIN):
            await entry.runtime_data.async_force_refresh()

    hass.services.async_register(const.DOMAIN, SERVICE_REFRESH, _refresh)
    await async_register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: FishingConfigEntry) -> bool:
    coordinator = FishingCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # local normals are built after setup so a slow archive request never delays the first score
    entry.async_create_background_task(hass, coordinator.async_update_climatology(), f"{const.DOMAIN} climatology")
    entry.async_on_unload(async_track_time_interval(hass, coordinator.async_update_climatology, const.CLIMATOLOGY_INTERVAL))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FishingConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
