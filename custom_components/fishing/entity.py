"""The base entity: one device per fishing spot, entities that only read the latest snapshot."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import const
from .coordinator import FishingCoordinator


class FishingEntity(CoordinatorEntity[FishingCoordinator]):
    """Entities are named after what they show ("Score"); the device carries the spot's name."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FishingCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(const.DOMAIN, entry.entry_id)},
            name=coordinator.cfg.name,
            manufacturer=const.NAME,
            model="Fishing spot",
            entry_type=DeviceEntryType.SERVICE,
        )
