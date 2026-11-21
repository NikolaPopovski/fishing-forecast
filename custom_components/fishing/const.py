"""Constants shared by the integration. No Home Assistant imports: tests load this module bare."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "fishing"
NAME = "Fishing Forecast"
#: one version for the integration (manifest.json) and the card (www/fishing-card.js inside the integration): a test keeps them equal
VERSION = "0.1.0"

# -- config entry keys (data = what the user chose at setup; options = what they may change) --
CONF_NAME = "name"
CONF_LATITUDE = "latitude"
CONF_LONGITUDE = "longitude"
CONF_ELEVATION = "elevation"                    # metres; None = take it from the weather API
CONF_SPOT_KIND = "spot_kind"
CONF_WATER_BODY = "water_body"
CONF_TIDAL = "tidal"
CONF_TIDE_RANGE = "tide_range_m"
CONF_CATCHMENT = "catchment"
CONF_PRESSURE_SOURCE = "pressure_source"
CONF_SHELTER = "shelter"                        # {"N": 0..1, ...} exposure per compass sector
CONF_BANK_ASPECT = "bank_aspect_deg"
CONF_SPECIES = "species"
CONF_WATER_TEMP_ENTITY = "water_temp_entity"    # optional real probe: sets the water-temperature level

SPOT_KINDS = ("lake", "reservoir", "river", "stream", "estuary", "coast")
PRESSURE_SOURCES = ("mslp", "station")

DEFAULT_SPOT_KIND = "lake"
DEFAULT_WATER_BODY = "lake_small"
DEFAULT_CATCHMENT = "mixed_farmland"
DEFAULT_PRESSURE_SOURCE = "mslp"
DEFAULT_SPECIES = "mixed_general"

# -- how often, and how much ---------------------------------------------------------------
#: network refresh of the forecast (Open-Meteo updates hourly; this is polite and plenty)
UPDATE_INTERVAL = timedelta(minutes=30)
#: re-score from the cached arrays: no network, so it can be frequent and cheap
RESCORE_INTERVAL = timedelta(minutes=5)
CLIMATOLOGY_INTERVAL = timedelta(hours=12)
#: hourly history to fetch: enough to warm up the water model and see the diurnal pressure tide
PAST_DAYS = 35
FORECAST_DAYS = 4
#: how far ahead the published forecast sensors look
FORECAST_HOURS = 48
#: years of daily archive used to build local normals on day one
CLIMATOLOGY_YEARS = 3

STORAGE_VERSION = 1
