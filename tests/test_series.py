"""Offline tests for model.series: tolerant sampling, regression slopes, diurnal-tide extraction."""
from __future__ import annotations

import math
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "custom_components" / "fishing"))   # appended, never prepended: select.py must not shadow the stdlib

from model import series as sr  # noqa: E402

T0 = datetime(2026, 6, 1, 0, 0)


def hourly(values, start=T0, step_h=1.0):
    return [(start + timedelta(hours=i * step_h), v) for i, v in enumerate(values)]


class CleanTest(unittest.TestCase):
    def test_sorts_dedupes_and_strips_bad_values(self):
        raw = [
            (T0 + timedelta(hours=2), 3.0),
            (T0, 1.0),
            (T0 + timedelta(hours=1), float("nan")),
            (T0 + timedelta(hours=3), None),
            (T0 + timedelta(hours=4), "x"),
            (T0 + timedelta(hours=5), "5.5"),   # numeric strings are coerced
            (T0, 9.0),                          # duplicate timestamp: last one wins
        ]
        out = sr.clean(raw)
        self.assertEqual(out, [(T0, 9.0), (T0 + timedelta(hours=2), 3.0), (T0 + timedelta(hours=5), 5.5)])

    def test_none_and_empty_are_safe(self):
        self.assertEqual(sr.clean(None), [])
        self.assertEqual(sr.clean([]), [])


class AtTest(unittest.TestCase):
    def test_exact_and_nearest_sample(self):
        s = hourly([10.0, 11.0, 12.0])
        self.assertEqual(sr.at(s, T0 + timedelta(hours=1)), 11.0)
        self.assertEqual(sr.at(s, T0 + timedelta(hours=1, minutes=20)), 11.0)
        self.assertEqual(sr.at(s, T0 + timedelta(hours=1, minutes=40)), 12.0)

    def test_age_bound_refuses_to_invent_across_a_gap(self):
        s = hourly([10.0, 11.0])
        self.assertIsNone(sr.at(s, T0 + timedelta(hours=8), max_age_hours=6.0))
        self.assertEqual(sr.at(s, T0 + timedelta(hours=7), max_age_hours=6.0), 11.0)  # last known, in bound

    def test_empty_series(self):
        self.assertIsNone(sr.at([], T0))


class TendencyTest(unittest.TestCase):
    def test_least_squares_recovers_a_known_ramp(self):
        s = hourly([1000.0 - 0.8 * i for i in range(10)])
        when = T0 + timedelta(hours=9)
        value, slope = sr.value_and_slope(s, when, 6.0)
        self.assertAlmostEqual(slope, -0.8, places=9)
        self.assertAlmostEqual(value, 1000.0 - 0.8 * 9, places=6)
        self.assertAlmostEqual(sr.tendency(s, when, 3.0), -2.4, places=6)

    def test_one_noisy_sample_does_not_become_a_fake_swing(self):
        flat = [1015.0] * 7
        flat[-1] = 1019.0                      # a single glitch on the last sample
        s = hourly(flat)
        slope = sr.value_and_slope(s, T0 + timedelta(hours=6), 6.0)[1]
        naive = (1019.0 - 1015.0) / 6.0        # "last minus first" would claim this
        self.assertLess(abs(slope), naive)

    def test_too_few_points_gives_no_slope(self):
        s = hourly([1000.0, 1001.0])
        value, slope = sr.value_and_slope(s, T0 + timedelta(hours=1), 3.0)
        self.assertIsNone(slope)
        self.assertEqual(value, 1001.0)
        self.assertIsNone(sr.tendency(s, T0 + timedelta(hours=1), 3.0))

    def test_degenerate_windows(self):
        self.assertEqual(sr.value_and_slope([], T0, 3.0), (None, None))
        self.assertEqual(sr.value_and_slope(hourly([1, 2, 3]), T0, 0.0), (None, None))


class WindowStatsTest(unittest.TestCase):
    def setUp(self):
        self.s = hourly([5.0, 1.0, 9.0, 3.0])
        self.when = T0 + timedelta(hours=3)

    def test_extrema_returns_min_then_max(self):
        self.assertEqual(sr.extrema(self.s, self.when, 3.0), (1.0, 9.0))
        self.assertEqual(sr.extrema([], self.when, 3.0), (None, None))

    def test_mean(self):
        self.assertAlmostEqual(sr.mean(self.s, self.when, 3.0), 4.5)
        self.assertIsNone(sr.mean(self.s, T0 + timedelta(days=5), 3.0))

    def test_sum_over_window_is_half_open_and_clamps_negatives(self):
        rain = hourly([1.0, 2.0, -5.0, 4.0])           # -5 is a feed glitch, not "negative rain"
        # window (T0, T0+3h]: excludes the first sample, includes the last
        self.assertAlmostEqual(sr.sum_over(rain, T0 + timedelta(hours=3), 3.0), 2.0 + 0.0 + 4.0)
        self.assertIsNone(sr.sum_over(rain, T0 + timedelta(days=9), 3.0))

    def test_decayed_sum_discounts_older_rain(self):
        when = T0 + timedelta(days=3)
        s = [(when - timedelta(days=2), 10.0)]
        self.assertAlmostEqual(sr.decayed_sum(s, when, 5.0, 0.8), 10.0 * 0.8 ** 2, places=9)
        self.assertAlmostEqual(sr.decayed_sum([(when, 10.0)], when, 5.0, 0.8), 10.0)
        self.assertIsNone(sr.decayed_sum([(when - timedelta(days=9), 10.0)], when, 5.0))


class DiurnalCycleTest(unittest.TestCase):
    @staticmethod
    def tide_series(days=6, amp=0.6):
        # pure semi-diurnal tide peaking at 10:00 and 22:00 local, as in the docstring
        out = []
        for i in range(days * 24):
            t = T0 + timedelta(hours=i)
            out.append((t, 1015.0 + amp * math.cos(2 * math.pi * (t.hour - 10) / 12.0)))
        return out

    def test_hour_of_day_cycle_recovers_an_injected_tide(self):
        cyc = sr.hour_of_day_cycle(self.tide_series())
        self.assertEqual(len(cyc), 24)
        self.assertAlmostEqual(cyc[10], 1015.6, places=6)
        self.assertAlmostEqual(cyc[16], 1014.4, places=6)

    def test_empty_hours_are_none(self):
        cyc = sr.hour_of_day_cycle([(T0, 1.0)])
        self.assertEqual(cyc[0], 1.0)
        self.assertIsNone(cyc[1])

    def test_cycle_tendency_matches_the_tide_slope(self):
        cyc = sr.hour_of_day_cycle(self.tide_series())
        # 10:00 -> 13:00 on the tide: 0.6*(cos(pi/2) - cos(0)) = -0.6
        self.assertAlmostEqual(sr.cycle_tendency(cyc, 10, 3.0), -0.6, places=6)
        # wraps across midnight
        self.assertIsNotNone(sr.cycle_tendency(cyc, 22, 4.0))

    def test_cycle_tendency_needs_data_at_both_ends(self):
        cyc = [None] * 24
        cyc[3] = 1.0
        cyc[4] = 2.0
        self.assertIsNone(sr.cycle_tendency(cyc, 3, 5.0))
        self.assertIsNone(sr.cycle_tendency([], 3, 3.0))


class AggregationTest(unittest.TestCase):
    def test_daily_from_hourly(self):
        s = hourly([1.0, 3.0] + [10.0] * 24)
        daily = sr.daily_from_hourly(s)
        self.assertEqual(daily[0][0], datetime(2026, 6, 1))
        self.assertAlmostEqual(daily[0][1], (1.0 + 3.0 + 10.0 * 22) / 24)

    def test_resample_is_linear(self):
        s = hourly([0.0, 10.0])
        out = sr.resample(s, 30)
        self.assertEqual([round(v, 6) for _, v in out][:3], [0.0, 5.0, 10.0])

    def test_resample_does_not_bridge_a_real_gap(self):
        s = hourly([0.0, 1.0]) + hourly([50.0, 51.0], start=T0 + timedelta(hours=12))
        out = sr.resample(s, 60)
        in_gap = [t for t, _ in out if T0 + timedelta(hours=2) < t < T0 + timedelta(hours=12)]
        self.assertEqual(in_gap, [])

    def test_empty(self):
        self.assertEqual(sr.resample([], 30), [])
        self.assertEqual(sr.daily_from_hourly([]), [])


class WindowTest(unittest.TestCase):
    def test_window(self):
        w = sr.Window(T0, T0 + timedelta(hours=4))
        self.assertEqual(w.hours, 4.0)
        self.assertEqual(w.mid, T0 + timedelta(hours=2))
        self.assertTrue(w.contains(T0 + timedelta(hours=4)))
        self.assertFalse(w.contains(T0 + timedelta(hours=5)))


if __name__ == "__main__":
    unittest.main()
