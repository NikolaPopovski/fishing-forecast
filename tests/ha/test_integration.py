"""The integration loaded into a real Home Assistant: setup, entities, species, refresh, failures."""
from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.fishing import const
from custom_components.fishing.api import open_meteo as om
from custom_components.fishing.model import species as sp

from conftest import fixture, make_entry

pytestmark = pytest.mark.usefixtures("now")


async def setup(hass: HomeAssistant, entry=None):
    entry = entry or make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def entity_id(hass: HomeAssistant, entry, platform: str, key: str) -> str:
    found = er.async_get(hass).async_get_entity_id(platform, const.DOMAIN, f"{entry.entry_id}_{key}")
    assert found is not None, f"no {platform} entity for {key}"
    return found


async def test_setup_loads_the_entry_and_creates_every_entity(hass: HomeAssistant, open_meteo):
    entry = await setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    entities = [e for e in registry.entities.values() if e.config_entry_id == entry.entry_id]
    assert {e.unique_id.removeprefix(f"{entry.entry_id}_") for e in entities} == {
        "score", "confidence", "water_temperature", "pressure_tendency", "forecast", "best_window",
        "feeding_time", "stale_data", "species",
    }


async def test_the_score_sensor_reports_a_real_score_with_advice(hass: HomeAssistant, open_meteo):
    entry = await setup(hass)
    state = hass.states.get(entity_id(hass, entry, "sensor", "score"))
    assert state is not None and state.state not in ("unknown", "unavailable")
    assert 0.0 <= float(state.state) <= 10.0
    assert state.attributes["species"] == "pike"
    assert state.attributes["band"] in ("terrible", "poor", "fair", "good", "excellent")
    assert state.attributes["headline"] and state.attributes["reasons"] and state.attributes["tactics"]
    assert len(state.attributes["factors"]) >= 8


async def test_other_sensors_have_sane_values(hass: HomeAssistant, open_meteo):
    entry = await setup(hass)
    confidence = hass.states.get(entity_id(hass, entry, "sensor", "confidence"))
    assert 0 <= float(confidence.state) <= 100 and confidence.attributes["unit_of_measurement"] == "%"
    water = hass.states.get(entity_id(hass, entry, "sensor", "water_temperature"))
    assert 0.0 < float(water.state) < 25.0
    assert water.attributes["device_class"] == "temperature"
    assert water.attributes["source"] == "model"
    forecast = hass.states.get(entity_id(hass, entry, "sensor", "forecast"))
    assert len(forecast.attributes["forecast"]) == const.FORECAST_HOURS
    assert 0.0 <= float(forecast.state) <= 10.0
    pressure = hass.states.get(entity_id(hass, entry, "sensor", "pressure_tendency"))
    assert pressure.state != "unavailable"


async def test_binary_sensors(hass: HomeAssistant, open_meteo):
    entry = await setup(hass)
    assert hass.states.get(entity_id(hass, entry, "binary_sensor", "feeding_time")).state in ("on", "off")
    assert hass.states.get(entity_id(hass, entry, "binary_sensor", "stale_data")).state == "off"


async def test_the_species_select_shows_and_changes_the_species(hass: HomeAssistant, open_meteo):
    entry = await setup(hass)
    select = entity_id(hass, entry, "select", "species")
    assert hass.states.get(select).state == sp.profile("pike").name
    assert sp.profile("zander").name in hass.states.get(select).attributes["options"]

    await hass.services.async_call("select", "select_option", {"entity_id": select, "option": sp.profile("zander").name}, blocking=True)
    await hass.async_block_till_done()

    assert hass.states.get(select).state == sp.profile("zander").name
    assert hass.states.get(entity_id(hass, entry, "sensor", "score")).attributes["species"] == "zander"
    assert entry.options[const.CONF_SPECIES] == "zander"                      # persisted for the next restart


async def test_the_refresh_service_fetches_again(hass: HomeAssistant, open_meteo):
    await setup(hass)
    before = open_meteo.count(om.FORECAST_URL)
    await hass.services.async_call(const.DOMAIN, "refresh", {}, blocking=True)
    await hass.async_block_till_done()
    assert open_meteo.count(om.FORECAST_URL) == before + 1


async def test_a_quiet_five_minutes_rescored_without_touching_the_network(hass: HomeAssistant, open_meteo, now):
    entry = await setup(hass)
    fetched = open_meteo.count(om.FORECAST_URL)
    first = hass.states.get(entity_id(hass, entry, "sensor", "score")).last_updated
    now.tick(const.RESCORE_INTERVAL + timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert open_meteo.count(om.FORECAST_URL) == fetched                      # no new request
    assert hass.states.get(entity_id(hass, entry, "sensor", "score")).last_updated >= first


async def test_the_forecast_is_refetched_after_the_interval(hass: HomeAssistant, open_meteo, now):
    await setup(hass)
    fetched = open_meteo.count(om.FORECAST_URL)
    now.tick(const.UPDATE_INTERVAL + timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert open_meteo.count(om.FORECAST_URL) == fetched + 1


async def test_setup_is_retried_when_the_forecast_cannot_be_fetched(hass: HomeAssistant, open_meteo):
    open_meteo.routes[om.FORECAST_URL] = om.OpenMeteoError("down")
    entry = await setup(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_failed_refresh_keeps_serving_the_last_forecast_and_flags_it_stale(hass: HomeAssistant, open_meteo, now):
    entry = await setup(hass)
    score = entity_id(hass, entry, "sensor", "score")
    stale = entity_id(hass, entry, "binary_sensor", "stale_data")
    assert hass.states.get(stale).state == "off"

    open_meteo.routes[om.FORECAST_URL] = om.OpenMeteoError("down")
    now.tick(timedelta(hours=4))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(score).state not in ("unknown", "unavailable")      # still scored from the cached forecast
    assert hass.states.get(stale).state == "on"
    assert hass.states.get(stale).attributes["error"]


async def test_the_entry_unloads_cleanly(hass: HomeAssistant, open_meteo):
    entry = await setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_a_river_spot_fetches_discharge_and_a_coastal_one_sea_temperature(hass: HomeAssistant, open_meteo):
    await setup(hass, make_entry(data={const.CONF_SPOT_KIND: "river", const.CONF_WATER_BODY: "river"}))
    assert open_meteo.count(om.FLOOD_URL) == 1 and open_meteo.count(om.MARINE_URL) == 0
    open_meteo.calls.clear()
    coast = make_entry(data={const.CONF_NAME: "Coast", const.CONF_LATITUDE: 52.5, const.CONF_LONGITUDE: 4.4, const.CONF_SPOT_KIND: "coast", const.CONF_TIDAL: True, const.CONF_WATER_BODY: "coastal_sea"})
    open_meteo.routes[om.MARINE_URL] = fixture("open_meteo_marine_sea")
    await setup(hass, coast)
    assert open_meteo.count(om.MARINE_URL) == 1 and open_meteo.count(om.FLOOD_URL) == 0


async def test_a_water_probe_sets_the_water_temperature_level(hass: HomeAssistant, open_meteo):
    hass.states.async_set("sensor.pond_probe", "21.0", {"unit_of_measurement": "°C"})
    entry = await setup(hass, make_entry(data={const.CONF_WATER_TEMP_ENTITY: "sensor.pond_probe"}))
    water = hass.states.get(entity_id(hass, entry, "sensor", "water_temperature"))
    assert water.attributes["source"] == "sensor"
    assert abs(float(water.state) - 21.0) < 0.2


async def test_a_probe_in_fahrenheit_is_converted(hass: HomeAssistant, open_meteo):
    hass.states.async_set("sensor.pond_probe", "69.8", {"unit_of_measurement": "°F"})
    entry = await setup(hass, make_entry(data={const.CONF_WATER_TEMP_ENTITY: "sensor.pond_probe"}))
    assert abs(float(hass.states.get(entity_id(hass, entry, "sensor", "water_temperature")).state) - 21.0) < 0.2


async def test_an_unavailable_probe_falls_back_to_the_model(hass: HomeAssistant, open_meteo):
    hass.states.async_set("sensor.pond_probe", "unavailable")
    entry = await setup(hass, make_entry(data={const.CONF_WATER_TEMP_ENTITY: "sensor.pond_probe"}))
    assert hass.states.get(entity_id(hass, entry, "sensor", "water_temperature")).attributes["source"] == "model"


async def test_the_local_normals_are_built_from_the_archive_and_persisted(hass: HomeAssistant, open_meteo, hass_storage):
    entry = await setup(hass)
    coordinator = entry.runtime_data
    await coordinator.async_update_climatology()
    assert open_meteo.count(om.ARCHIVE_URL) >= 1
    assert coordinator.clim is not None and coordinator.clim.days_observed > 0
    assert any(key.startswith(f"{const.DOMAIN}.climatology_") for key in hass_storage)
