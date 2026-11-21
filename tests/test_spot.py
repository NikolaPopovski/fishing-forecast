"""Offline tests for fishing.spot and fishing.snapshot: recorded API data -> context -> score -> JSON."""
from __future__ import annotations

import json
import math
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from glue import fixture  # noqa: E402

from fishing import const, snapshot, spot  # noqa: E402
from fishing.api import open_meteo as om  # noqa: E402
from fishing.model import climatology as cl  # noqa: E402
from fishing.model import species as sp  # noqa: E402

NOW = datetime(2026, 11, 21, 12, 0)             # inside every recorded fixture's window


def entry(**extra):
    base = {const.CONF_NAME: "Test mark", const.CONF_LATITUDE: 52.7, const.CONF_LONGITUDE: 5.3}
    base.update(extra)
    return base


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = spot.SpotConfig.from_entry(entry())
        self.assertEqual((cfg.spot_kind, cfg.water_body, cfg.pressure_source, cfg.species), ("lake", "lake_small", "mslp", "mixed_general"))
        self.assertIsNone(cfg.elevation_m)
        self.assertFalse(cfg.tidal)
        self.assertEqual(cfg.tide_range_m, 0.0)
        self.assertEqual(cfg.catchment_response, 0.60)

    def test_options_override_data_and_invalid_values_fall_back(self):
        cfg = spot.SpotConfig.from_entry(
            entry(**{const.CONF_SPOT_KIND: "river", const.CONF_SPECIES: "pike"}),
            {const.CONF_SPECIES: "carp", const.CONF_SPOT_KIND: "swamp", const.CONF_WATER_BODY: "bathtub", const.CONF_PRESSURE_SOURCE: "x"},
        )
        self.assertEqual(cfg.species, "carp")
        self.assertEqual(cfg.spot_kind, "lake")                 # invalid -> default, not a crash
        self.assertEqual(cfg.water_body, "lake_small")
        self.assertEqual(cfg.pressure_source, "mslp")

    def test_numbers_are_coerced_and_blank_means_unset(self):
        cfg = spot.SpotConfig.from_entry(entry(**{const.CONF_ELEVATION: "", const.CONF_BANK_ASPECT: "", const.CONF_TIDE_RANGE: "-3", const.CONF_SHELTER: {"N": "0.2", "SW": 1}}))
        self.assertIsNone(cfg.elevation_m)
        self.assertIsNone(cfg.bank_aspect_deg)
        self.assertEqual(cfg.tide_range_m, 0.0)                 # negative range is meaningless
        self.assertEqual(cfg.shelter, {"N": 0.2, "SW": 1.0})
        self.assertEqual(spot.SpotConfig.from_entry(entry(**{const.CONF_ELEVATION: 12})).elevation_m, 12.0)

    def test_catchment_by_name_or_number(self):
        self.assertEqual(spot.SpotConfig.from_entry(entry(**{const.CONF_CATCHMENT: "urban_storm"})).catchment_response, 0.95)
        self.assertEqual(spot.SpotConfig.from_entry(entry(**{const.CONF_CATCHMENT: 1.7})).catchment_response, 1.0)
        self.assertEqual(spot.SpotConfig.from_entry(entry(**{const.CONF_CATCHMENT: "nonsense"})).catchment_response, 0.60)

    def test_missing_coordinates_are_an_error_not_a_default(self):
        with self.assertRaises(KeyError):
            spot.SpotConfig.from_entry({const.CONF_NAME: "x"})


class TimezoneTest(unittest.TestCase):
    def test_unresolvable_zone_uses_the_fixed_offset_and_keeps_the_name(self):
        fc = om.Forecast(timezone="Not/AZone", utc_offset_seconds=7200)
        self.assertEqual(spot.resolve_timezone(fc), ("Not/AZone", 120))

    def test_nothing_known_is_utc(self):
        self.assertEqual(spot.resolve_timezone(om.Forecast()), ("UTC", None))
        self.assertEqual(spot.resolve_timezone(om.Forecast(timezone="Not/AZone")), ("UTC", None))

    def test_utc_needs_no_tz_database_and_ignores_a_quoted_offset(self):
        # the system Python here has no tz database (even ZoneInfo("UTC") fails), so UTC is special-cased
        for name in ("UTC", "Etc/UTC", "GMT"):
            self.assertEqual(spot.resolve_timezone(om.Forecast(timezone=name, utc_offset_seconds=3600)), ("UTC", None))


class BuildContextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.forecast = om.parse_forecast(fixture("open_meteo_forecast"))
        cls.sea = om.parse_marine(fixture("open_meteo_marine_sea"))
        cls.inland = om.parse_marine(fixture("open_meteo_marine_inland"))
        cls.flood = om.parse_flood(fixture("open_meteo_flood"))
        cls.cfg = spot.SpotConfig.from_entry(entry())

    def build(self, cfg=None, **kw):
        return spot.build_context(cfg or self.cfg, self.forecast, now=NOW, **kw)

    def test_series_and_geometry(self):
        ctx = self.build()
        self.assertEqual(ctx.lat, 52.7)
        self.assertEqual(ctx.elevation_m, 0.0)                  # taken from the forecast
        self.assertEqual(len(ctx.get("air_temp")), 96)
        self.assertEqual(ctx.get("pressure"), self.forecast.series["pressure"])
        self.assertEqual(ctx.tz_name, "Europe/Amsterdam")
        # CET in November, whether the zone came from a tz database or from the fixed-offset fallback
        self.assertEqual(ctx.tz.utcoffset(datetime(2026, 11, 21, 12)).total_seconds(), 3600)

    def test_configured_elevation_beats_the_api(self):
        self.assertEqual(self.build(spot.SpotConfig.from_entry(entry(**{const.CONF_ELEVATION: 140}))).elevation_m, 140.0)

    def test_pressure_source_selects_the_series(self):
        station = om.parse_forecast(fixture("open_meteo_forecast"), "station")
        ctx = spot.build_context(spot.SpotConfig.from_entry(entry(**{const.CONF_PRESSURE_SOURCE: "station"})), station, now=NOW)
        self.assertEqual(ctx.pressure_source, "station")
        self.assertEqual(ctx.get("pressure"), station.raw["surface_pressure"])

    def test_sea_surface_temperature_only_when_the_api_has_it(self):
        self.assertIsNotNone(self.build(marine=self.sea).sea_surface_temp)
        self.assertTrue(5.0 < self.build(marine=self.sea).sea_surface_temp < 25.0)
        self.assertIsNone(self.build(marine=self.inland).sea_surface_temp)       # all-null upstream
        self.assertIsNone(self.build(marine=None).sea_surface_temp)

    def test_discharge_only_when_a_gauge_exists(self):
        self.assertEqual(len(self.build(flood=self.flood).get("discharge")), 10)
        self.assertEqual(self.build(flood=None).get("discharge"), [])
        self.assertEqual(self.build(flood=[]).get("discharge"), [])

    def test_the_diurnal_pressure_tide_is_learned_from_the_past_only(self):
        ctx = self.build()
        self.assertEqual(len(ctx.diurnal_pressure_cycle), 24)
        early = spot.build_context(self.cfg, self.forecast, now=datetime(2026, 11, 19, 4, 0))
        self.assertEqual(early.diurnal_pressure_cycle, [])      # only five hours of past: too little to claim a tide

    def test_shelter_aspect_and_catchment_reach_the_context(self):
        cfg = spot.SpotConfig.from_entry(entry(**{const.CONF_SHELTER: {"SW": 1.0}, const.CONF_BANK_ASPECT: 220, const.CONF_CATCHMENT: "urban_storm", const.CONF_TIDAL: True, const.CONF_TIDE_RANGE: 2.5}))
        ctx = self.build(cfg)
        self.assertEqual(ctx.shelter.bank_aspect_deg, 220.0)
        self.assertEqual(ctx.shelter.exposure["SW"], 1.0)
        self.assertEqual(ctx.catchment_response, 0.95)
        self.assertTrue(ctx.tidal)
        self.assertEqual(ctx.tide_range_m, 2.5)

    def test_a_probe_reading_is_passed_through(self):
        ctx = self.build(readings=spot.Readings(water_temp=9.5, water_temp_age_hours=2.0))
        self.assertEqual((ctx.water_sensor, ctx.water_sensor_age_hours), (9.5, 2.0))

    def test_climatology_from_archive_rows(self):
        clim = spot.new_climatology(self.cfg, 0.0)
        self.assertEqual(spot.ingest_into(clim, om.parse_archive_daily(fixture("open_meteo_archive"))), 100)
        self.assertIs(self.build(clim=clim).clim, clim)
        self.assertIsInstance(clim, cl.Climatology)


class SnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        forecast = om.parse_forecast(fixture("open_meteo_forecast"))
        cls.ctx = spot.build_context(
            spot.SpotConfig.from_entry(entry()), forecast, now=NOW, marine=om.parse_marine(fixture("open_meteo_marine_inland")), flood=None
        )
        cls.snap = snapshot.compute(cls.ctx, NOW, "pike")

    def test_it_is_plain_json(self):
        json.dumps(self.snap)

    def test_headline_numbers_are_sane_on_real_data(self):
        s = self.snap
        self.assertEqual(s["species"], "pike")
        self.assertTrue(0.0 <= s["score"] <= 10.0)
        self.assertIn(s["band"], ("terrible", "poor", "fair", "good", "excellent"))
        self.assertTrue(0.0 <= s["confidence"] <= 1.0)
        self.assertTrue(s["headline"] and s["reasons"] and s["tactics"] and s["window"])
        self.assertIsInstance(s["feeding_time"], bool)
        self.assertEqual(s["when"], NOW.isoformat())

    def test_real_weather_feeds_the_real_factors(self):
        names = {f["name"] for f in self.snap["factors"] if f["source"] != "unavailable"}
        self.assertTrue({"water_temp", "pressure", "wind", "cloud", "precip", "time_of_day"} <= names, names)
        self.assertIsNotNone(self.snap["water"]["temp"])
        self.assertEqual(self.snap["water"]["source"], "model")          # no probe, no SST at an inland point
        self.assertIsNotNone(self.snap["pressure"]["value"])
        self.assertTrue(940 < self.snap["pressure"]["value"] < 1060)

    def test_the_forecast_curve_covers_48_hours_from_now(self):
        fc = self.snap["forecast"]
        self.assertEqual(len(fc), 48)
        self.assertEqual(fc[0]["time"], NOW.isoformat())
        self.assertTrue(all(0.0 <= p["score"] <= 10.0 and math.isfinite(p["score"]) for p in fc))

    def test_best_windows_are_consistent_with_the_curve(self):
        for w in self.snap["best_windows"]:
            self.assertGreaterEqual(w["mean_score"], 6.0)
        self.assertEqual(self.snap["next_best"], (self.snap["best_windows"] or [None])[0])

    def test_every_species_produces_a_snapshot_on_real_data(self):
        for prof in sp.PROFILE_LIST:
            with self.subTest(species=prof.id):
                snap = snapshot.compute(self.ctx, NOW, prof.id, hours=6)
                self.assertEqual(snap["species"], prof.id)
                self.assertTrue(0.0 <= snap["score"] <= 10.0)
                self.assertEqual(len(snap["forecast"]), 6)

    def test_an_unknown_species_falls_back_instead_of_failing(self):
        self.assertEqual(snapshot.compute(self.ctx, NOW, "no_such_fish", hours=3)["species"], "mixed_general")

    def test_a_context_with_no_weather_still_snapshots_and_says_so(self):
        bare = spot.build_context(
            spot.SpotConfig.from_entry(entry()), om.Forecast(series={}, timezone="UTC"), now=NOW
        )
        snap = snapshot.compute(bare, NOW, "pike", hours=3)
        self.assertLess(snap["confidence"], 0.5)
        self.assertTrue(snap["notes"])
        json.dumps(snap)

    def test_a_coastal_spot_with_sea_data_uses_it(self):
        forecast = om.parse_forecast(fixture("open_meteo_forecast"))
        cfg = spot.SpotConfig.from_entry(entry(**{const.CONF_SPOT_KIND: "coast", const.CONF_TIDAL: True, const.CONF_TIDE_RANGE: 2.0, const.CONF_WATER_BODY: "coastal_sea"}))
        ctx = spot.build_context(cfg, forecast, now=NOW, marine=om.parse_marine(fixture("open_meteo_marine_sea")))
        snap = snapshot.compute(ctx, NOW, "European_bass", hours=3)
        self.assertEqual(snap["water"]["source"], "sst")
        self.assertTrue(5.0 < snap["water"]["temp"] < 25.0)
        self.assertIn("tide", {f["name"] for f in snap["factors"]})


class ProbeAndWindowTest(unittest.TestCase):
    def test_temperatures_are_converted_to_celsius(self):
        self.assertEqual(spot.to_celsius(12.5, "°C"), 12.5)
        self.assertEqual(spot.to_celsius(12.5, None), 12.5)
        self.assertAlmostEqual(spot.to_celsius(50.0, "°F"), 10.0)
        self.assertAlmostEqual(spot.to_celsius(283.15, "K"), 10.0)
        self.assertIsNone(spot.to_celsius(5.0, "bananas"))

    def test_the_probe_entity_is_optional_and_blank_means_none(self):
        self.assertIsNone(spot.SpotConfig.from_entry(entry()).water_temp_entity)
        self.assertIsNone(spot.SpotConfig.from_entry(entry(**{const.CONF_WATER_TEMP_ENTITY: ""})).water_temp_entity)
        self.assertEqual(spot.SpotConfig.from_entry(entry(**{const.CONF_WATER_TEMP_ENTITY: "sensor.pond"})).water_temp_entity, "sensor.pond")

    def test_best_windows_carry_timezone_aware_utc_instants(self):
        forecast = om.parse_forecast(fixture("open_meteo_forecast"))
        ctx = spot.build_context(spot.SpotConfig.from_entry(entry()), forecast, now=NOW)
        snap = snapshot.compute(ctx, NOW, "pike", hours=48)
        wins = snap["best_windows"] or [snap["next_best"]]
        self.assertTrue(wins and wins[0])
        for w in wins:
            start, end = datetime.fromisoformat(w["start_utc"]), datetime.fromisoformat(w["end_utc"])
            self.assertIsNotNone(start.tzinfo)
            self.assertLess(start, end)
            # the spot's clock is UTC+1 here, so the UTC instant is an hour before the local one
            self.assertEqual((datetime.fromisoformat(w["start"]) - start.replace(tzinfo=None)).total_seconds(), 3600)

    def test_water_model_is_exposed_for_the_climatology(self):
        from fishing.model import scoring

        forecast = om.parse_forecast(fixture("open_meteo_forecast"))
        ctx = spot.build_context(spot.SpotConfig.from_entry(entry()), forecast, now=NOW)
        series = scoring.water_model_series(ctx)
        self.assertEqual(len(series), 96)
        self.assertIs(scoring.water_model_series(ctx), series)          # built once


if __name__ == "__main__":
    unittest.main()
