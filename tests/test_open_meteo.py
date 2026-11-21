"""Offline tests for fishing.api.open_meteo, run against recorded real responses (tests/fixtures)."""
from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from glue import fixture  # noqa: E402  (registers the `fishing` stand-in package)

from fishing.api import open_meteo as om  # noqa: E402


class ParamsTest(unittest.TestCase):
    def test_forecast_params_ask_for_local_time_and_metres_per_second(self):
        p = om.forecast_params(52.70333333, 5.29166667, past_days=35, forecast_days=4)
        self.assertEqual((p["latitude"], p["longitude"]), (52.7033, 5.2917))
        self.assertEqual((p["past_days"], p["forecast_days"]), (35, 4))
        self.assertEqual(p["wind_speed_unit"], "ms")
        self.assertEqual(p["timezone"], "auto")
        asked = set(p["hourly"].split(","))
        self.assertEqual(asked, set(om.HOURLY_VARIABLES))
        for needed in ("temperature_2m", "pressure_msl", "surface_pressure", "cloud_cover_low", "uv_index", "cape"):
            self.assertIn(needed, asked)

    def test_other_requests(self):
        self.assertEqual(om.marine_params(52.5, 4.4, past_days=1, forecast_days=1)["hourly"], "sea_surface_temperature,wave_height")
        self.assertEqual(om.flood_params(51.9, 5.9, past_days=5, forecast_days=5)["daily"], "river_discharge")
        a = om.archive_params(52.7, 5.3, date(2023, 1, 1), date(2025, 12, 31))
        self.assertEqual((a["start_date"], a["end_date"]), ("2023-01-01", "2025-12-31"))
        self.assertEqual(set(a["daily"].split(",")), set(om.ARCHIVE_DAILY))
        self.assertEqual(om.geocoding_params("  Enkhuizen ", 3)["name"], "Enkhuizen")

    def test_every_mapped_series_is_a_name_the_model_knows(self):
        from fishing.model.context import SERIES_NAMES

        for name in list(om.HOURLY_TO_SERIES.values()) + ["pressure"]:
            self.assertIn(name, SERIES_NAMES)


class ForecastParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = fixture("open_meteo_forecast")
        cls.fc = om.parse_forecast(cls.payload)

    def test_metadata(self):
        self.assertEqual(self.fc.timezone, "Europe/Amsterdam")
        self.assertEqual(self.fc.utc_offset_seconds, 3600)
        self.assertEqual(self.fc.elevation_m, 0.0)
        self.assertAlmostEqual(self.fc.latitude, 52.7, delta=0.1)

    def test_series_are_renamed_for_the_model_and_complete(self):
        for name in ("air_temp", "humidity", "wind_speed", "wind_dir", "cloud", "precip", "uv_index", "pressure", "shortwave", "cape", "dewpoint"):
            self.assertEqual(len(self.fc.series[name]), 96, name)
        self.assertNotIn("temperature_2m", self.fc.series)
        self.assertIn("temperature_2m", self.fc.raw)

    def test_times_are_naive_local_hourly_and_ordered(self):
        times = [t for t, _ in self.fc.series["air_temp"]]
        self.assertIsNone(times[0].tzinfo)
        self.assertEqual(times[0], datetime(2026, 11, 19, 0, 0))
        self.assertEqual({b - a for a, b in zip(times, times[1:])}, {timedelta(hours=1)})

    def test_values_keep_the_units_the_model_expects(self):
        values = {n: [v for _, v in s] for n, s in self.fc.series.items()}
        self.assertTrue(all(0 <= v <= 100 for v in values["cloud"]))            # percent, not a fraction
        self.assertTrue(all(0 <= v <= 100 for v in values["humidity"]))
        self.assertTrue(max(values["cloud"]) > 1.5)
        self.assertTrue(all(0 <= v <= 360 for v in values["wind_dir"]))
        self.assertTrue(all(940 < v < 1060 for v in values["pressure"]))        # hPa

    def test_pressure_follows_the_chosen_source(self):
        mslp = om.parse_forecast(self.payload, "mslp")
        station = om.parse_forecast(self.payload, "station")
        self.assertEqual(mslp.series["pressure"], mslp.raw["pressure_msl"])
        self.assertEqual(station.series["pressure"], station.raw["surface_pressure"])
        self.assertEqual(om.parse_forecast(self.payload, "nonsense").series["pressure"], mslp.series["pressure"])

    def test_missing_nan_and_text_values_make_no_sample(self):
        p = copy.deepcopy(self.payload)
        p["hourly"]["temperature_2m"][3] = None
        p["hourly"]["temperature_2m"][4] = "n/a"
        p["hourly"]["temperature_2m"][5] = float("nan")
        p["hourly"]["temperature_2m"][6] = True
        fc = om.parse_forecast(p)
        self.assertEqual(len(fc.series["air_temp"]), 96 - 4)
        self.assertEqual(len(fc.series["wind_speed"]), 96)

    def test_a_missing_variable_or_short_array_is_tolerated(self):
        p = copy.deepcopy(self.payload)
        del p["hourly"]["wind_speed_10m"]
        p["hourly"]["cloud_cover"] = p["hourly"]["cloud_cover"][:10]
        fc = om.parse_forecast(p)
        self.assertNotIn("wind_speed", fc.series)
        self.assertEqual(len(fc.series["cloud"]), 10)

    def test_an_all_null_variable_produces_no_series(self):
        p = copy.deepcopy(self.payload)
        p["hourly"]["uv_index"] = [None] * 96
        self.assertNotIn("uv_index", om.parse_forecast(p).series)

    def test_bad_payloads_raise_one_error_type(self):
        for bad in (None, [], "x", {"error": True, "reason": "Latitude must be in range"}, {"hourly": {}}, {"hourly": {"time": []}}, {"latitude": 1}):
            with self.subTest(payload=bad):
                with self.assertRaises(om.OpenMeteoError):
                    om.parse_forecast(bad)
        with self.assertRaisesRegex(om.OpenMeteoError, "Latitude"):
            om.parse_forecast({"error": True, "reason": "Latitude must be in range"})

    def test_unknown_elevation_marker_and_missing_zone(self):
        p = copy.deepcopy(self.payload)
        p["elevation"] = -9999.0
        del p["timezone"]
        fc = om.parse_forecast(p)
        self.assertIsNone(fc.elevation_m)
        self.assertIsNone(fc.timezone)


class MarineFloodTest(unittest.TestCase):
    def test_sea_point_has_sea_surface_temperature(self):
        m = om.parse_marine(fixture("open_meteo_marine_sea"))
        self.assertTrue(m.has_sea_surface_temp)
        self.assertEqual(len(m.sea_surface_temp), 48)
        self.assertTrue(5.0 < m.sea_surface_temp[0][1] < 25.0)
        self.assertEqual(len(m.wave_height), 48)

    def test_inland_point_has_no_sea_temperature_but_keeps_waves(self):
        # regression: the API answers inland points with an all-null SST series, not an error
        m = om.parse_marine(fixture("open_meteo_marine_inland"))
        self.assertFalse(m.has_sea_surface_temp)
        self.assertEqual(m.sea_surface_temp, [])
        self.assertEqual(len(m.wave_height), 48)

    def test_flood_is_daily_discharge(self):
        q = om.parse_flood(fixture("open_meteo_flood"))
        self.assertEqual(len(q), 10)
        self.assertEqual(q[0][0], datetime(2026, 11, 16))
        self.assertTrue(all(v >= 0 for _, v in q))

    def test_flood_without_a_gauge_cell_is_empty(self):
        self.assertEqual(om.parse_flood({"daily": {"time": ["2026-11-16"], "river_discharge": [None]}}), [])
        self.assertEqual(om.parse_flood({"latitude": 1.0}), [])


class ArchiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = om.parse_archive_daily(fixture("open_meteo_archive"))

    def test_ten_days_in_order_with_dates(self):
        self.assertEqual(len(self.rows), 10)
        self.assertEqual(self.rows[0][0], date(2025, 11, 1))
        self.assertEqual([d for d, _ in self.rows], sorted(d for d, _ in self.rows))

    def test_rows_use_the_climatology_names_and_units(self):
        from fishing.model.climatology import VARIABLES

        day, row = self.rows[0]
        self.assertTrue(set(row) <= set(VARIABLES))
        self.assertEqual(set(row), {"air_temp", "air_temp_max", "air_temp_min", "air_temp_range", "pressure", "pressure_slp", "wind_speed", "cloud", "humidity", "precip"})
        self.assertTrue(0.0 <= row["cloud"] <= 1.0)            # climatology documents 0..1, the API says percent
        self.assertTrue(0.0 <= row["humidity"] <= 1.0)
        self.assertAlmostEqual(row["air_temp_range"], row["air_temp_max"] - row["air_temp_min"])

    def test_rows_feed_a_climatology(self):
        from fishing.model.climatology import Climatology

        clim = Climatology(52.7, 0.0)
        self.assertEqual(clim.ingest_series(self.rows), 10 * 10)     # ten days x ten variables
        self.assertEqual(clim.days_observed, 10)
        self.assertGreater(clim.coverage("pressure_slp"), 0)


class GeocodingTest(unittest.TestCase):
    def test_results_and_label(self):
        places = om.parse_geocoding(fixture("open_meteo_geocoding"))
        self.assertEqual(len(places), 2)
        first = places[0]
        self.assertEqual(first.label, "Enkhuizen, North Holland, The Netherlands")
        self.assertEqual((first.latitude, first.longitude, first.elevation_m, first.timezone), (52.70333, 5.29167, 2.0, "Europe/Amsterdam"))

    def test_unknown_elevation_is_none_not_minus_9999(self):
        # regression: the geocoder reports -9999.0 for "no elevation known"
        self.assertIsNone(om.parse_geocoding(fixture("open_meteo_geocoding"))[1].elevation_m)

    def test_no_results_or_junk_entries(self):
        self.assertEqual(om.parse_geocoding({"generationtime_ms": 0.1}), [])
        junk = {"results": [None, "x", {"name": "No coords"}, {"latitude": 1.0, "longitude": 2.0}, {"name": "Ok", "latitude": 1.0, "longitude": 2.0}]}
        self.assertEqual([p.name for p in om.parse_geocoding(junk)], ["Ok"])
        self.assertEqual(om.Place("Ok", 1.0, 2.0, None, None).label, "Ok")


class ValueAtTest(unittest.TestCase):
    def test_nearest_within_the_age_bound(self):
        s = [(datetime(2026, 1, 1, h), float(h)) for h in range(0, 24, 3)]
        self.assertEqual(om.value_at(s, datetime(2026, 1, 1, 7, 10)), 6.0)
        self.assertIsNone(om.value_at(s, datetime(2026, 1, 2, 12)))
        self.assertIsNone(om.value_at([], datetime(2026, 1, 1)))
        self.assertEqual(om.value_at(s, datetime(2026, 1, 2, 12), max_age_hours=24), 21.0)


class ClientTest(unittest.TestCase):
    @staticmethod
    def fake(routes, calls):
        async def fetch(url, params):
            calls.append((url, dict(params)))
            result = routes[url]
            if isinstance(result, Exception):
                raise result
            return result

        return fetch

    def test_forecast_requests_and_parses(self):
        calls = []
        client = om.OpenMeteoClient(self.fake({om.FORECAST_URL: fixture("open_meteo_forecast")}, calls))
        fc = asyncio.run(client.forecast(52.7, 5.3, past_days=2, forecast_days=2, pressure_source="station"))
        self.assertEqual(calls[0][0], om.FORECAST_URL)
        self.assertEqual(calls[0][1]["past_days"], 2)
        self.assertEqual(fc.series["pressure"], fc.raw["surface_pressure"])

    def test_forecast_failures_become_openmeteoerror(self):
        for exc in (OSError("no route"), TimeoutError(), ValueError("bad json")):
            client = om.OpenMeteoClient(self.fake({om.FORECAST_URL: exc}, []))
            with self.subTest(exc=type(exc).__name__):
                with self.assertRaises(om.OpenMeteoError):
                    asyncio.run(client.forecast(1.0, 2.0, past_days=1, forecast_days=1))

    def test_optional_endpoints_degrade_to_none(self):
        routes = {
            om.MARINE_URL: {"error": True, "reason": "no marine data here"},
            om.FLOOD_URL: OSError("down"),
        }
        client = om.OpenMeteoClient(self.fake(routes, []))
        self.assertIsNone(asyncio.run(client.marine(1.0, 2.0, past_days=1, forecast_days=1)))
        self.assertIsNone(asyncio.run(client.flood(1.0, 2.0, past_days=1, forecast_days=1)))

    def test_flood_with_no_gauge_is_none_and_marine_inland_is_empty_not_none(self):
        routes = {
            om.FLOOD_URL: {"daily": {"time": ["2026-11-16"], "river_discharge": [None]}},
            om.MARINE_URL: fixture("open_meteo_marine_inland"),
        }
        client = om.OpenMeteoClient(self.fake(routes, []))
        self.assertIsNone(asyncio.run(client.flood(1.0, 2.0, past_days=1, forecast_days=1)))
        marine = asyncio.run(client.marine(1.0, 2.0, past_days=1, forecast_days=1))
        self.assertIsNotNone(marine)
        self.assertFalse(marine.has_sea_surface_temp)

    def test_geocode_and_archive(self):
        calls = []
        client = om.OpenMeteoClient(self.fake({om.GEOCODING_URL: fixture("open_meteo_geocoding"), om.ARCHIVE_URL: fixture("open_meteo_archive")}, calls))
        self.assertEqual(asyncio.run(client.geocode("Enkhuizen"))[0].name, "Enkhuizen")
        self.assertEqual(len(asyncio.run(client.archive(52.7, 5.3, date(2025, 11, 1), date(2025, 11, 10)))), 10)
        self.assertEqual(asyncio.run(client.geocode("   ")), [])
        self.assertEqual(len(calls), 2)                      # the blank search never hit the network


if __name__ == "__main__":
    unittest.main()
