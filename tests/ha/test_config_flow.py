"""The setup and options flows, driven the way the Home Assistant UI drives them."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.fishing import const
from custom_components.fishing.api import open_meteo as om
from custom_components.fishing.diagnostics import async_get_config_entry_diagnostics

from conftest import make_entry

pytestmark = pytest.mark.usefixtures("now")

WATER = {
    const.CONF_NAME: "My lake",
    const.CONF_SPOT_KIND: "lake",
    const.CONF_WATER_BODY: "lake_small",
    const.CONF_TIDAL: False,
    const.CONF_CATCHMENT: "mixed_farmland",
    const.CONF_PRESSURE_SOURCE: "mslp",
}


async def start(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(const.DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    return result


async def test_manual_coordinates_flow_creates_an_entry(hass: HomeAssistant, open_meteo):
    with patch("custom_components.fishing.async_setup_entry", return_value=True):
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"place": ""})
        assert result["step_id"] == "manual"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {const.CONF_LATITUDE: 52.7, const.CONF_LONGITUDE: 5.3})
        assert result["step_id"] == "spot"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(WATER))
        assert result["step_id"] == "species"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {const.CONF_SPECIES: "perch"})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "My lake"
    assert result["data"][const.CONF_LATITUDE] == 52.7
    assert result["data"][const.CONF_WATER_BODY] == "lake_small"
    assert result["options"] == {const.CONF_SPECIES: "perch"}
    assert result["result"].unique_id == "52.7000_5.3000"
    assert open_meteo.calls == []                              # coordinates entered by hand: no search made


async def test_place_search_flow_uses_the_geocoder_result(hass: HomeAssistant, open_meteo):
    with patch("custom_components.fishing.async_setup_entry", return_value=True):
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"place": "Enkhuizen"})
        assert result["step_id"] == "pick"
        assert open_meteo.count(om.GEOCODING_URL) == 1
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"place": "0"})
        assert result["step_id"] == "spot"
        # the form is pre-filled from the search result (the UI submits these values back)
        suggested = {
            str(key): key.description["suggested_value"]
            for key in result["data_schema"].schema
            if key.description and "suggested_value" in key.description
        }
        assert suggested[const.CONF_ELEVATION] == 2.0
        defaults = {str(key): key.default() for key in result["data_schema"].schema if key.default is not None and callable(key.default)}
        assert defaults[const.CONF_NAME] == "Enkhuizen"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {**WATER, const.CONF_NAME: "Enkhuizen", const.CONF_ELEVATION: 2.0})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {const.CONF_SPECIES: "pike"})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][const.CONF_LATITUDE] == 52.70333
    assert result["data"][const.CONF_ELEVATION] == 2.0            # carried over from the search result
    assert result["result"].unique_id == "52.7033_5.2917"


async def test_choosing_manual_from_the_search_results(hass: HomeAssistant, open_meteo):
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"place": "Enkhuizen"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"place": "manual"})
    assert result["step_id"] == "manual"


async def test_a_search_with_no_results_says_so_and_stays_on_the_form(hass: HomeAssistant, open_meteo):
    open_meteo.routes[om.GEOCODING_URL] = {"generationtime_ms": 0.1}
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"place": "Nowhereville"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    assert result["errors"] == {"base": "no_results"}


async def test_a_search_that_cannot_connect_says_so(hass: HomeAssistant, open_meteo):
    open_meteo.routes[om.GEOCODING_URL] = om.OpenMeteoError("offline")
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"place": "Enkhuizen"})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_the_same_coordinates_cannot_be_added_twice(hass: HomeAssistant, open_meteo):
    make_entry().add_to_hass(hass)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"place": ""})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {const.CONF_LATITUDE: 52.7, const.CONF_LONGITUDE: 5.3})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(WATER))
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_a_full_flow_produces_a_working_integration(hass: HomeAssistant, open_meteo):
    """No patching of setup: the entry the flow creates must actually load and score."""
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"place": ""})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {const.CONF_LATITUDE: 52.7, const.CONF_LONGITUDE: 5.3})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(WATER))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {const.CONF_SPECIES: "zander"})
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.cfg.species == "zander"
    assert entry.runtime_data.data["species"] == "zander"


async def test_the_options_flow_changes_the_spot_and_reloads_it(hass: HomeAssistant, open_meteo):
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            const.CONF_SPECIES: "zander",
            const.CONF_SPOT_KIND: "reservoir",
            const.CONF_WATER_BODY: "reservoir",
            const.CONF_TIDAL: False,
            const.CONF_CATCHMENT: "clay_drained",
            const.CONF_PRESSURE_SOURCE: "mslp",
        },
    )
    assert result["step_id"] == "shelter"
    shelter = {sector: 0.2 for sector in ("N", "NE", "E", "SE", "S", "SW", "W", "NW")}
    shelter["SW"] = 1.0
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**shelter, const.CONF_BANK_ASPECT: 200})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED                        # reloaded automatically
    cfg = entry.runtime_data.cfg
    assert (cfg.species, cfg.spot_kind, cfg.water_body, cfg.catchment) == ("zander", "reservoir", "reservoir", "clay_drained")
    assert cfg.shelter["SW"] == 1.0 and cfg.bank_aspect_deg == 200.0
    assert entry.runtime_data.data["species"] == "zander"


async def test_clearing_an_optional_field_in_the_options_really_clears_it(hass: HomeAssistant, open_meteo):
    hass.states.async_set("sensor.pond_probe", "21.0", {"unit_of_measurement": "°C"})
    entry = make_entry(data={const.CONF_WATER_TEMP_ENTITY: "sensor.pond_probe", const.CONF_ELEVATION: 30.0})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.cfg.water_temp_entity == "sensor.pond_probe"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {const.CONF_SPECIES: "pike", const.CONF_SPOT_KIND: "lake", const.CONF_WATER_BODY: "lake_small", const.CONF_TIDAL: False,
         const.CONF_CATCHMENT: "mixed_farmland", const.CONF_PRESSURE_SOURCE: "mslp"},          # probe and elevation left out
    )
    await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert entry.runtime_data.cfg.water_temp_entity is None
    assert entry.runtime_data.cfg.elevation_m is None


async def test_diagnostics_redact_the_location_and_keep_the_numbers(hass: HomeAssistant, open_meteo):
    entry = make_entry(data={const.CONF_WATER_TEMP_ENTITY: "sensor.pond_probe"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"][const.CONF_LATITUDE] == "**REDACTED**"
    assert diag["entry"][const.CONF_LONGITUDE] == "**REDACTED**"
    assert diag["entry"][const.CONF_WATER_TEMP_ENTITY] == "**REDACTED**"
    assert diag["snapshot"]["context"]["lat"] == "**REDACTED**"
    assert "forecast" not in diag["snapshot"] and diag["forecast_points"] == const.FORECAST_HOURS
    assert 0 <= diag["snapshot"]["score"] <= 10
    assert diag["last_update_success"] is True
    assert "52.7" not in str(diag)
