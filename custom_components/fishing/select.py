"""The species selector: pick what you are fishing for and the whole spot re-scores for it."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FishingConfigEntry
from .entity import FishingEntity
from .model import species as sp


async def async_setup_entry(hass: HomeAssistant, entry: FishingConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([FishingSpeciesSelect(entry.runtime_data)])


class FishingSpeciesSelect(FishingEntity, SelectEntity):
    """Options are the species' display names (ids are an implementation detail)."""

    _attr_translation_key = "species"
    _attr_icon = "mdi:fish"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "species")
        names = sp.species_names()
        self._by_name = {name: sid for sid, name in names.items()}
        self._attr_options = list(self._by_name)

    @property
    def current_option(self) -> str | None:
        return sp.profile(self.coordinator.cfg.species).name

    async def async_select_option(self, option: str) -> None:
        species_id = self._by_name.get(option)
        if species_id is not None:
            await self.coordinator.async_set_species(species_id)
