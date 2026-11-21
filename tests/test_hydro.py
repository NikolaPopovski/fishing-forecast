"""Offline tests for model.hydro: log-space discharge, level trend, turbidity, plume, degradation."""
from __future__ import annotations

import json
import math
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "custom_components" / "fishing"))   # appended, never prepended: select.py must not shadow the stdlib

from model import hydro as hy  # noqa: E402

T0 = datetime(2025, 3, 1)
NOW = T0 + timedelta(days=39)
SPOT_KINDS = ("stream", "river", "lake", "reservoir", "estuary", "coast")


def daily_gauge(scale=100.0, days=40, final=None):
    """A skewed-looking daily gauge: smooth wobble around *scale*; optional final-day override."""
    out = []
    for d in range(days):
        q = scale * (1.0 + 0.3 * math.sin(d / 4.0))
        out.append((T0 + timedelta(days=d), q))
    if final is not None:
        out[-1] = (out[-1][0], final)
    return out


def hourly_rain(events, days=22):
    """events: {hours_before_NOW: mm}; hourly series with zeros elsewhere."""
    out = []
    for h in range(24 * days):
        t = NOW - timedelta(hours=24 * days - 1 - h)
        mm = 0.0
        for ago, amount in events.items():
            if abs((NOW - t).total_seconds() / 3600.0 - ago) < 0.5:
                mm = amount
        out.append((t, mm))
    return out


class DischargeStatsTest(unittest.TestCase):
    def test_log_stats_need_enough_history(self):
        self.assertEqual(hy.log_discharge_stats(daily_gauge(days=7))[2], 7)
        self.assertEqual(hy.log_discharge_stats(daily_gauge(days=7))[:2], (0.0, 0.0))
        mu, sd, n = hy.log_discharge_stats(daily_gauge(days=40))
        self.assertEqual(n, 40)
        self.assertTrue(mu > 0 and sd > 0)

    def test_negative_and_missing_values_are_ignored(self):
        g = daily_gauge(days=12) + [(T0 + timedelta(days=50), -5.0), (T0 + timedelta(days=51), None)]
        self.assertEqual(hy.log_discharge_stats(g)[2], 12)

    def test_percentile_rank(self):
        g = [(T0 + timedelta(days=i), float(i + 1)) for i in range(10)]
        self.assertAlmostEqual(hy.percentile_rank(g, 5.5), 0.5)
        self.assertAlmostEqual(hy.percentile_rank(g, 5.0), 0.45)       # ties count half
        self.assertEqual(hy.percentile_rank(g, 0.1), 0.0)
        self.assertEqual(hy.percentile_rank(g, 99.0), 1.0)
        self.assertIsNone(hy.percentile_rank(g[:5], 3.0))

    def test_discharge_z_is_in_log_space_so_up_and_down_by_the_same_factor_mirror(self):
        g = daily_gauge(100.0)
        z_up = hy.analyse(NOW, discharge=g[:-1] + [(NOW, 300.0)]).discharge_z
        z_down = hy.analyse(NOW, discharge=g[:-1] + [(NOW, 100.0 / 3.0)]).discharge_z
        mu, sd, _ = hy.log_discharge_stats(g[:-1] + [(NOW, 300.0)])
        self.assertAlmostEqual(z_up, (math.log(301.0) - mu) / sd, places=9)
        self.assertGreater(z_up, 0.0)
        self.assertLess(z_down, 0.0)
        # raw-space z would be wildly asymmetric (x3 is +200, /3 is -67); log space is the same size
        self.assertAlmostEqual(abs(z_up), abs(z_down), delta=0.6 * abs(z_up))


class LevelTrendTest(unittest.TestCase):
    def test_rising_flat_and_falling(self):
        flat = [(T0 + timedelta(days=d), 100.0) for d in range(5)]
        rising = [(T0 + timedelta(days=d), 100.0 * 1.3 ** d) for d in range(5)]
        falling = [(T0 + timedelta(days=d), 100.0 * 0.8 ** d) for d in range(5)]
        when = T0 + timedelta(days=4)
        self.assertAlmostEqual(hy.level_slope_per_day(flat, when), 0.0, places=9)
        self.assertGreater(hy.level_slope_per_day(rising, when), 0.2)
        self.assertLess(hy.level_slope_per_day(falling, when), -0.15)

    def test_works_on_a_daily_gauge(self):
        # regression: a 24 h regression window holds <3 daily points, so the trend was always None
        daily = [(T0 + timedelta(days=d), 100.0 * 1.2 ** d) for d in range(6)]
        self.assertIsNotNone(hy.level_slope_per_day(daily, T0 + timedelta(days=5)))

    def test_scale_free(self):
        # regression: slope was taken on raw discharge but divided by a log-space sd
        results = []
        for scale in (3.0, 100.0, 1000.0):
            st = hy.analyse(NOW, discharge=daily_gauge(scale, final=scale * 1.5))
            results.append(st.level_trend_z)
        self.assertTrue(all(r is not None for r in results))
        self.assertAlmostEqual(results[1], results[2], delta=0.03)
        self.assertAlmostEqual(results[0], results[1], delta=0.06)       # tiny flows feel the +1 in log(Q+1)

    def test_too_few_points(self):
        self.assertIsNone(hy.level_slope_per_day([(T0, 5.0), (T0 + timedelta(days=1), 6.0)], T0 + timedelta(days=1)))
        self.assertIsNone(hy.level_slope_per_day([], T0))


class TurbidityTest(unittest.TestCase):
    def turbidity(self, rain, **kw):
        return hy.turbidity_proxy(rain, kw.pop("discharge", []), kw.pop("when", NOW), kw.pop("catchment", 0.6))

    def test_no_rain_series_means_no_estimate(self):
        self.assertIsNone(hy.turbidity_proxy([], [], NOW, 0.6))

    def test_rises_after_rain_and_decays_on_the_recession(self):
        # 30 mm fell 40 h ago; ask at +0 days, +2, +4, +6 after the event
        event = T0 + timedelta(days=30)
        rain = [(event + timedelta(hours=h), 30.0 if h == 0 else 0.0) for h in range(24 * 12)]
        before = hy.turbidity_proxy([r for r in rain if r[0] < event] or [(event - timedelta(hours=1), 0.0)], [], event - timedelta(hours=1), 0.6)
        values = [hy.turbidity_proxy(rain, [], event + timedelta(days=d, hours=1), 0.6) for d in (0, 2, 4, 6)]
        self.assertGreater(values[0], before)
        self.assertTrue(values[0] > values[1] > values[2] > values[3], values)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in values))

    def test_antecedent_wetness_amplifies_the_same_storm(self):
        storm = {6: 25.0}
        dry = hourly_rain(storm)
        wet = hourly_rain({**storm, 96: 20.0, 150: 25.0, 220: 20.0, 300: 25.0})
        self.assertGreater(self.turbidity(wet), self.turbidity(dry))

    def test_flashy_catchments_run_muddier_than_buffered_ones(self):
        rain = hourly_rain({6: 25.0})
        self.assertGreater(self.turbidity(rain, catchment=hy.CATCHMENT_RESPONSES["urban_storm"]), self.turbidity(rain, catchment=hy.CATCHMENT_RESPONSES["forest_sandy"]))

    def test_rising_limb_is_muddier_than_the_recession(self):
        rain = hourly_rain({30: 20.0})
        # a long steady history keeps the log-sd small, so a sharp 3-day move is a real signal
        history = [(NOW - timedelta(days=k), 100.0 * (1.0 + 0.3 * math.sin(k / 4.0))) for k in range(4, 130)]
        rising = [(NOW - timedelta(days=k), 100.0 * 1.5 ** (3 - k)) for k in range(3, -1, -1)]
        falling = [(NOW - timedelta(days=k), 100.0 * 0.67 ** (3 - k)) for k in range(3, -1, -1)]
        up = self.turbidity(rain, discharge=history + rising)
        down = self.turbidity(rain, discharge=history + falling)
        flat = self.turbidity(rain)
        self.assertGreater(up, flat)
        self.assertLess(down, flat)


class PlumeTest(unittest.TestCase):
    def test_only_for_standing_or_salt_water(self):
        for kind in ("estuary", "coast", "lake", "reservoir"):
            self.assertIsNotNone(hy.plume_index(30.0, 10.0, kind), kind)
        for kind in ("river", "stream", "pond"):
            self.assertIsNone(hy.plume_index(30.0, 10.0, kind), kind)

    def test_grows_with_rain_is_bounded_and_needs_rain_data(self):
        self.assertLess(hy.plume_index(5.0, 0.0, "estuary"), hy.plume_index(40.0, 0.0, "estuary"))
        self.assertEqual(hy.plume_index(500.0, 500.0, "estuary"), 1.0)
        self.assertEqual(hy.plume_index(0.0, None, "coast"), 0.0)
        self.assertIsNone(hy.plume_index(None, 5.0, "coast"))


class AnalyseTest(unittest.TestCase):
    def test_every_spot_kind_evaluates_with_everything_or_nothing(self):
        for kind in SPOT_KINDS:
            with self.subTest(kind=kind):
                full = hy.analyse(NOW, spot_kind=kind, discharge=daily_gauge(), rain_hourly=hourly_rain({5: 12.0, 40: 6.0}))
                empty = hy.analyse(NOW, spot_kind=kind)
                self.assertTrue(-1.0 <= full.score <= 1.0)
                self.assertEqual(empty.score, 0.0)
                self.assertEqual(empty.parts, {})
                json.dumps(full.as_dict())

    def test_missing_gauge_degrades_with_a_note_and_keeps_the_rain_factors(self):
        st = hy.analyse(NOW, spot_kind="river", rain_hourly=hourly_rain({5: 12.0}))
        self.assertIsNone(st.discharge)
        self.assertIsNone(st.discharge_z)
        self.assertNotIn("state", st.parts)
        self.assertIn("turbidity", st.parts)
        self.assertTrue(any("no discharge" in n for n in st.notes))

    def test_short_gauge_history_is_reported(self):
        st = hy.analyse(NOW, discharge=daily_gauge(days=40)[-5:])
        self.assertIsNone(st.discharge_z)
        self.assertTrue(any("too short" in n for n in st.notes))

    def test_rain_totals(self):
        st = hy.analyse(NOW, rain_hourly=hourly_rain({2: 10.0, 30: 5.0, 60: 2.0, 100: 40.0}))
        self.assertAlmostEqual(st.rain_24h_mm, 10.0)
        self.assertAlmostEqual(st.rain_72h_mm, 17.0)
        self.assertGreater(st.antecedent_mm, 17.0 * 0.5)

    def test_negative_rain_glitches_do_not_subtract(self):
        st = hy.analyse(NOW, rain_hourly=hourly_rain({2: 10.0, 3: -50.0}))
        self.assertAlmostEqual(st.rain_24h_mm, 10.0)

    def test_daily_rain_has_totals_but_no_intensity(self):
        # regression: a 10 mm *day* was read as a 10 mm/h cloudburst by the raindrop curve
        rain = [(T0 + timedelta(days=d), 10.0 if d >= 38 else 0.0) for d in range(40)]
        st = hy.analyse(NOW, spot_kind="lake", rain_daily=rain)
        self.assertIsNone(st.rain_intensity_mmh)
        self.assertNotIn("raindrop", st.parts)
        self.assertGreaterEqual(st.rain_72h_mm, 20.0)

    def test_raindrop_factor_is_for_still_water_only(self):
        rain = hourly_rain({0: 1.0})
        self.assertIn("raindrop", hy.analyse(NOW, spot_kind="lake", rain_hourly=rain).parts)
        self.assertNotIn("raindrop", hy.analyse(NOW, spot_kind="river", rain_hourly=rain).parts)

    def test_plume_only_appears_where_it_applies(self):
        rain = hourly_rain({5: 30.0})
        self.assertIn("plume", hy.analyse(NOW, spot_kind="estuary", rain_hourly=rain).parts)
        self.assertNotIn("plume", hy.analyse(NOW, spot_kind="river", rain_hourly=rain).parts)

    def test_unknown_spot_kind_uses_river_weights(self):
        rain, gauge = hourly_rain({5: 20.0}), daily_gauge()
        self.assertAlmostEqual(
            hy.analyse(NOW, spot_kind="pond", discharge=gauge, rain_hourly=rain).score,
            hy.analyse(NOW, spot_kind="river", discharge=gauge, rain_hourly=rain).score,
        )

    def test_completeness_says_how_much_of_the_picture_was_there(self):
        rain = hourly_rain({5: 12.0})
        full = hy.analyse(NOW, spot_kind="river", discharge=daily_gauge(), rain_hourly=rain)
        rain_only = hy.analyse(NOW, spot_kind="river", rain_hourly=rain)
        nothing = hy.analyse(NOW, spot_kind="river")
        self.assertAlmostEqual(full.completeness, 1.0)
        self.assertTrue(0.0 < rain_only.completeness < 0.6)
        self.assertEqual(nothing.completeness, 0.0)
        self.assertAlmostEqual(hy.analyse(NOW, spot_kind="lake", rain_hourly=rain).completeness, 1.0)
        self.assertIn("completeness", full.as_dict())

    def test_a_flood_scores_worse_than_normal_flow(self):
        normal = hy.analyse(NOW, discharge=daily_gauge(100.0, final=110.0))
        flood = hy.analyse(NOW, discharge=daily_gauge(100.0, final=2000.0))
        self.assertGreater(flood.discharge_z, 2.0)
        self.assertLess(flood.parts["state"], normal.parts["state"])

    def test_removing_the_gauge_changes_the_score_only_modestly(self):
        rain = hourly_rain({5: 4.0})
        with_gauge = hy.analyse(NOW, discharge=daily_gauge(100.0, final=105.0), rain_hourly=rain)
        without = hy.analyse(NOW, rain_hourly=rain)
        self.assertLess(abs(with_gauge.score - without.score), 0.6)


class WordsTest(unittest.TestCase):
    def test_level_words_follow_the_percentile(self):
        def words(p):
            return hy.HydroState(NOW, "river", 1.0, 0.0, p, None, None, None, None, None, None, None).level_words

        self.assertEqual(words(None), "unknown")
        self.assertEqual(words(0.05), "very low for this water")
        self.assertEqual(words(0.2), "low for this water")
        self.assertEqual(words(0.5), "normal for this water")
        self.assertEqual(words(0.8), "high for this water")
        self.assertEqual(words(0.95), "very high for this water")

    def test_clarity_words_are_monotone(self):
        order = ["gin clear", "lightly stained", "stained", "murky", "flood-coloured"]
        got = [hy.fishing_water_clarity_words(x) for x in (0.05, 0.25, 0.45, 0.65, 0.9)]
        self.assertEqual(got, order)
        self.assertEqual(hy.fishing_water_clarity_words(None), "unknown")

    def test_catchment_table_is_ordered_buffered_to_flashy(self):
        c = hy.CATCHMENT_RESPONSES
        self.assertLess(c["forest_sandy"], c["peat_bog"])
        self.assertLess(c["mixed_farmland"], c["urban_storm"])
        self.assertTrue(all(0.0 < v <= 1.0 for v in c.values()))
        self.assertTrue(hasattr(hy, "ANTECEDENT_DRYNESS"))


if __name__ == "__main__":
    unittest.main()
