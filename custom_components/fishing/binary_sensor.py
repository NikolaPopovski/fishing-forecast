"""Binary sensors: is it feeding time, and can the data be trusted right now."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FishingConfigEntry
from .entity import FishingEntity


@dataclass(frozen=True, kw_only=True)
class FishingBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], bool | None]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


BINARY_SENSORS: tuple[FishingBinarySensorDescription, ...] = (
    FishingBinarySensorDescription(
        key="feeding_time",
        translation_key="feeding_time",
        icon="mdi:fishbowl-outline",
        # inside a solunar window or a golden hour, and conditions at least fair
        value_fn=lambda d: d.get("feeding_time"),
        attrs_fn=lambda d: {"solunar": d.get("solunar"), "window": d.get("window")},
    ),
    FishingBinarySensorDescription(
        key="stale_data",
        translation_key="stale_data",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        # on when the forecast being scored is older than it should be (the last refresh failed)
        value_fn=lambda d: (d.get("network") or {}).get("state") != "fresh",
        attrs_fn=lambda d: dict(d.get("network") or {}),
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: FishingConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(FishingBinarySensor(entry.runtime_data, description) for description in BINARY_SENSORS)


class FishingBinarySensor(FishingEntity, BinarySensorEntity):
    entity_description: FishingBinarySensorDescription

    def __init__(self, coordinator, description: FishingBinarySensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data or {}) if fn else None
