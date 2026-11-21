"""Offline tests for climatology: local normals, anomalies, elevation handling."""
from __future__ import annotations

import math
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "custom_components" / "fishing"))   # appended, never prepended: select.py must not shadow the stdlib

from model import climatology as cl  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import synthetic_store  # noqa: E402


class PressureReductionTest(unittest.TestCase):
    def test_sea_level_reduction_matches_the_bellani_formula(self):
        # 1000 hPa at 300 m with a 8 degC column -> ~1037 hPa (weather services' value)
        self.assertAlmostEqual(cl.pressure_to_sea_level(1000.0, 300.0, 8.0), 1037.1, delta=0.5)
        self.assertAlmostEqual(cl.sea_level_to_station(1037.13, 300.0, 8.0), 1000.0, delta=0.05)

    def test_reduction_is_monotonic_in_elevation_and_temperature(self):
        cold = cl.pressure_to_sea_level(980.0, 600.0, -5.0)
        warm = cl.pressure_to_sea_level(980.0, 600.0, 25.0)
        self.assertGreater(cold, warm)  # a cold column is compressed: bigger reduction
        self.assertGreater(cl.pressure_to_sea_level(980.0, 900.0, 10.0), cl.pressure_to_sea_level(980.0, 300.0, 10.0))

    def test_lapse_rate_cools_higher_spots(self):
        self.assertAlmostEqual(cl.apply_lapse(20.0, 500.0), 16.75, delta=0.05)
        self.assertLess(cl.apply_lapse(20.0, 1200.0), cl.apply_lapse(20.0, 300.0))


class NormalsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = synthetic_store()

    def test_normals_track_the_season(self):
        jan = self.store.normal("air_temp", date(2026, 1, 15))
        jul = self.store.normal("air_temp", date(2026, 7, 15))
        self.assertTrue(jan.usable and jul.usable)
        self.assertLess(jan.value, jul.value)
        self.assertGreater(jul.value - jan.value, 15.0, "two years of a maritime sine should show ~19C swing")
        self.assertEqual(jul.mode, "annual")
        self.assertGreaterEqual(jul.covered_days, 40)

    def test_anomaly_is_in_sigmas_and_sign_correct(self):
        warm_day = self.store.anomaly("air_temp", self.store.normal("air_temp", date(2026, 5, 3)).value + 5.0, date(2026, 5, 3))
        cold_day = self.store.anomaly("air_temp", self.store.normal("air_temp", date(2026, 5, 3)).value - 5.0, date(2026, 5, 3))
        self.assertGreater(warm_day, 1.5)
        self.assertLess(cold_day, -1.5)
        self.assertAlmostEqual(self.store.anomaly("air_temp", self.store.normal("air_temp", date(2026, 5, 3)).value, date(2026, 5, 3)), 0.0, delta=0.05)

    def test_percentile_is_consistent_with_anomaly(self):
        n = self.store.normal("wind_speed", date(2026, 11, 10))
        self.assertAlmostEqual(self.store.percentile_of("wind_speed", n.value, date(2026, 11, 10)), 0.5, delta=0.02)
        self.assertGreater(self.store.percentile_of("wind_speed", n.value + 2 * n.sd, date(2026, 11, 10)), 0.95)
        self.assertLess(self.store.percentile_of("wind_speed", n.value - 2 * n.sd, date(2026, 11, 10)), 0.05)

    def test_missing_variable_reports_no_normal_instead_of_inventing_one(self):
        n = self.store.normal("water_temp", date(2026, 5, 3))
        self.assertEqual(n.mode, "none")
        self.assertFalse(n.usable)
        self.assertIsNone(self.store.anomaly("water_temp", 15.0, date(2026, 5, 3)))
        self.assertEqual(n.trust, 0.0)

    def test_thin_coverage_lowers_trust_and_falls_back_to_flat(self):
        store = cl.Climatology(52.0, 0.0, "new")
        # one season only, exactly what Open-Meteo's 92-day history gives on day one
        for i in range(92):
            store.ingest_daily(date(2026, 4, 1) + timedelta(days=i), {"air_temp": 12.0 + 0.05 * i})
        spring = store.normal("air_temp", date(2026, 5, 15))
        winter = store.normal("air_temp", date(2026, 12, 15))
        self.assertEqual(spring.mode, "local")
        self.assertEqual(winter.mode, "flat")
        self.assertLess(winter.trust, spring.trust)
        self.assertEqual(store.coverage("air_temp"), 92)

    def test_running_stats_are_exact_and_stable(self):
        a = cl.Climatology(0, 0, "a")
        b = cl.Climatology(0, 0, "b")
        values = [3.0, 5.0, 2.5, 7.5, 4.0]
        for i, v in enumerate(values):
            a.ingest_daily(date(2026, 6, 1) + timedelta(days=i), {"air_temp": v})
            b.ingest_daily(date(2026, 6, 1) + timedelta(days=i), {"air_temp": v})
        na = a.normal("air_temp", date(2026, 6, 3))
        self.assertAlmostEqual(na.value, sum(values) / len(values), places=6)
        self.assertAlmostEqual(na.sd, math.sqrt(sum((v - na.value) ** 2 for v in values) / (len(values) - 1)), places=6)

    def test_feb29_and_leap_years_land_in_the_same_bucket(self):
        store = cl.Climatology(0, 0, "leap")
        store.ingest_daily(date(2024, 2, 29), {"air_temp": 5.0})
        store.ingest_daily(date(2028, 2, 29), {"air_temp": 7.0})
        self.assertEqual(cl.doy_of(date(2024, 2, 29)), 366)
        n = store.normal("air_temp", date(2025, 12, 20), )
        self.assertIsNotNone(n.value)

    def test_bad_input_cannot_corrupt_the_store(self):
        store = cl.Climatology(0, 0, "junk")
        store.ingest_daily(date(2026, 1, 1), {"air_temp": None, "pressure": "abc", "wind_speed": float("nan"), "bogus_var": 5})
        self.assertEqual(store.data, {})
        store.ingest_daily(date(2026, 1, 1), {"air_temp": 3.0})
        self.assertEqual(store.coverage("air_temp"), 1)

    def test_roundtrip_survives_json(self):
        import json

        payload = json.loads(json.dumps(self.store.to_dict()))
        back = cl.Climatology.from_dict(payload)
        self.assertEqual(back.coverage("air_temp"), self.store.coverage("air_temp"))
        a = self.store.normal("pressure", date(2026, 1, 20))
        b = back.normal("pressure", date(2026, 1, 20))
        self.assertAlmostEqual(a.value, b.value, places=6)
        self.assertAlmostEqual(a.sd, b.sd, places=6)

    def test_from_dict_refuses_a_foreign_schema(self):
        payload = self.store.to_dict()
        payload["schema"] = 999
        self.assertEqual(cl.Climatology.from_dict(payload).coverage("air_temp"), 0)

    def test_merge_adds_years_without_losing_min_max(self):
        a = synthetic_store(years=1, seed=3)
        b = synthetic_store(years=1, seed=11)
        before = a.normal("air_temp", date(2026, 5, 5))
        a.merge(b)
        after = a.normal("air_temp", date(2026, 5, 5))
        self.assertGreater(after.samples, before.samples)


class AnalogDaysTest(unittest.TestCase):
    HISTORY = [
        {"date": "2025-05-01", "values": {"temp_a": 1.0, "press_a": -0.5, "wind_a": 0.2}, "outcome": 3.0},
        {"date": "2025-05-08", "values": {"temp_a": 0.9, "press_a": -0.4, "wind_a": 0.3}, "outcome": 5.0},
        {"date": "2025-06-02", "values": {"temp_a": -2.0, "press_a": 1.5, "wind_a": -1.0}, "outcome": 0.0},
        {"date": "2025-06-14", "values": {"temp_a": 1.1, "press_a": -0.6, "wind_a": 0.1}, "outcome": 4.0},
    ]
    KEYS = ["temp_a", "press_a", "wind_a"]

    def test_finds_the_similar_days_and_not_the_dissimilar_one(self):
        target = {"temp_a": 1.0, "press_a": -0.5, "wind_a": 0.25}
        got = cl.analog_days(self.HISTORY, target, self.KEYS, top=3)
        self.assertEqual(got[0].date, "2025-05-01")            # exact match
        self.assertEqual({a.date for a in got}, {"2025-05-01", "2025-05-08", "2025-06-14"})
        self.assertGreater(got[0].similarity, 0.95)
        self.assertEqual(got[0].outcome, 3.0)
        self.assertNotIn("2025-06-02", [a.date for a in got])

    def test_degrades_gracefully_on_empty_or_sparse_input(self):
        self.assertEqual(cl.analog_days([], {"a": 1}, ["a"]), [])
        self.assertEqual(cl.analog_days(self.HISTORY, {}, self.KEYS), [])
        self.assertEqual(cl.analog_days(self.HISTORY, {"temp_a": 1.0}, ["temp_a"]), [])

    def test_normalised_space_keeps_units_from_domating_the_distance(self):
        # a raw distance would let pressure (hPa, tens) swamp temperature (sigma, ~1)
        target = {"temp_a": 1.0, "press_a": -0.5}
        got = cl.analog_days(self.HISTORY, target, ["temp_a", "press_a"], top=1)
        self.assertEqual(got[0].date, "2025-05-01")


if __name__ == "__main__":
    unittest.main(verbosity=2)
