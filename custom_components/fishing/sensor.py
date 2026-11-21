"""Sensors: the score and the numbers and advice behind it."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import DEGREE, PERCENTAGE, UnitOfSpeed, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FishingConfigEntry
from .entity import FishingEntity


def _get(data: dict[str, Any], *path: str) -> Any:
    """Walk a nested dict; anything missing along the way is None, never an exception."""
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _timestamp(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        return None


def _score_attrs(d: dict[str, Any]) -> dict[str, Any]:
    return {k: d.get(k) for k in ("band", "headline", "reasons", "tactics", "window", "conditions", "notes", "species", "species_name", "factors")}


def _forecast_peak(d: dict[str, Any]) -> float | None:
    scores = [p["score"] for p in d.get("forecast") or []]
    return max(scores) if scores else None


@dataclass(frozen=True, kw_only=True)
class FishingSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


SENSORS: tuple[FishingSensorDescription, ...] = (
    FishingSensorDescription(
        key="score",
        translation_key="score",
        icon="mdi:fish",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.get("score"),
        attrs_fn=_score_attrs,
    ),
    FishingSensorDescription(
        key="confidence",
        translation_key="confidence",
        icon="mdi:shield-check-outline",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: None if d.get("confidence") is None else round(d["confidence"] * 100),
        attrs_fn=lambda d: {"coverage": d.get("coverage")},
    ),
    FishingSensorDescription(
        key="water_temperature",
        translation_key="water_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: _get(d, "water", "temp"),
        attrs_fn=lambda d: dict(d.get("water") or {}),
    ),
    FishingSensorDescription(
        key="pressure_tendency",
        translation_key="pressure_tendency",
        icon="mdi:gauge",
        native_unit_of_measurement="hPa/3h",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: _get(d, "pressure", "tendency_3h"),
        attrs_fn=lambda d: dict(d.get("pressure") or {}),
    ),
    FishingSensorDescription(
        key="wind_speed",
        translation_key="wind_speed",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: _get(d, "wind", "speed"),
        attrs_fn=lambda d: {"direction": _get(d, "wind", "direction"), "cardinal": _get(d, "wind", "cardinal")},
    ),
    FishingSensorDescription(
        key="wind_direction",
        translation_key="wind_direction",
        device_class=SensorDeviceClass.WIND_DIRECTION,
        native_unit_of_measurement=DEGREE,
        state_class=SensorStateClass.MEASUREMENT_ANGLE,
        suggested_display_precision=0,
        value_fn=lambda d: _get(d, "wind", "direction"),
        attrs_fn=lambda d: {"cardinal": _get(d, "wind", "cardinal"), "from": "the direction the wind blows from"},
    ),
    FishingSensorDescription(
        key="forecast",
        translation_key="forecast",
        icon="mdi:chart-line",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_forecast_peak,
        attrs_fn=lambda d: {"forecast": d.get("forecast"), "best_windows": d.get("best_windows")},
    ),
    FishingSensorDescription(
        key="best_window",
        translation_key="best_window",
        icon="mdi:clock-star-four-points-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: _timestamp(_get(d, "next_best", "start_utc")),
        attrs_fn=lambda d: dict(d.get("next_best") or {}),
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: FishingConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(FishingSensor(entry.runtime_data, description) for description in SENSORS)


class FishingSensor(FishingEntity, SensorEntity):
    entity_description: FishingSensorDescription

    def __init__(self, coordinator, description: FishingSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        try:
            return self.entity_description.value_fn(data)
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data or {}) if fn else None
