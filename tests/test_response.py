"""Tests for the response-curve layer: shapes, weights, degradation."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "custom_components" / "fishing"))   # appended, never prepended: select.py must not shadow the stdlib

from model import response as rs  # noqa: E402


class CurveTest(unittest.TestCase):
    def test_interpolation_is_linear_flat_outside_and_clamped(self):
        c = rs.Curve("t", [(0.0, -1.0), (1.0, 0.0), (2.0, 1.0)])
        self.assertEqual(c(-5.0), -1.0)
        self.assertEqual(c(9.0), 1.0)
        self.assertEqual(c(0.5), -0.5)
        self.assertEqual(c(1.5), 0.5)

    def test_none_is_neutral_not_zero_score_by_accident(self):
        # an unknown factor must be *dropped* by the caller; if it reaches a curve it must not
        # look like a confident "0" reading
        c = rs.PRESSURE_TENDENCY_3H
        self.assertEqual(c(None), 0.0)

    def test_empty_curve_is_neutral(self):
        self.assertEqual(rs.Curve("empty", [])(3.0), 0.0)

    def test_out_of_range_outputs_stay_in_range(self):
        for curve in (
            rs.PRESSURE_ANOMALY, rs.PRESSURE_TENDENCY_3H, rs.WIND_SPEED, rs.CLOUD_COVER,
            rs.WATER_TEMP_SUITABILITY, rs.TIME_OF_DAY, rs.UV_INDEX, rs.RAIN_ON_DAYS,
        ):
            for x in (-100.0, -3.0, -0.7, 0.0, 0.7, 3.0, 100.0):
                self.assertTrue(-1.0 <= curve(x) <= 1.0, f"{curve.name}({x}) out of range")

    def test_pressure_tendency_rewards_a_falling_barometer_more_than_a_rising_one(self):
        fall = rs.PRESSURE_TENDENCY_3H(-2.5)
        rise = rs.PRESSURE_TENDENCY_3H(+2.5)
        self.assertGreater(fall, 0.6)
        self.assertLess(rise, 0.0)
        # and a crashing barometer is not better than a steady fall (front violence)
        self.assertLess(rs.PRESSURE_TENDENCY_3H(-6.0), rs.PRESSURE_TENDENCY_3H(-2.5))

    def test_pressure_anomaly_is_an_inverted_u(self):
        curve = rs.PRESSURE_ANOMALY
        best = max((-2, -1, -0.25, 0.25, 1.0, 2.2), key=curve)
        self.assertEqual(best, 0.25)
        self.assertLess(curve(-2.0), curve(0.25))
        self.assertLess(curve(2.2), curve(0.25))

    def test_wind_prefers_a_ripple_and_penalises_gales(self):
        self.assertGreater(rs.WIND_SPEED(3.5), rs.WIND_SPEED(0.0))
        self.assertGreater(rs.WIND_SPEED(3.5), rs.WIND_SPEED(6.5))
        self.assertLess(rs.WIND_SPEED(14.0), -0.5)

    def test_water_temperature_optimum_sits_at_the_species_optimum(self):
        c = rs.WATER_TEMP_SUITABILITY
        self.assertEqual(c(0.0), 1.0)
        self.assertLess(c(-6.0), 0.0)
        self.assertLess(c(+6.0), 0.0)
        self.assertAlmostEqual(c(-2.5), c(+2.5), delta=0.2)  # roughly symmetric penalty

    def test_time_of_day_penalises_midday_and_rewards_dusk(self):
        self.assertLess(rs.TIME_OF_DAY(13.0), 0.0)
        self.assertGreater(rs.TIME_OF_DAY(18.5), 0.7)
        self.assertLess(rs.TIME_OF_DAY(10.0), rs.TIME_OF_DAY(7.0))

    def test_shifted_and_scaled_variants_do_not_mutate_the_original(self):
        base = rs.WATER_TEMP_SUITABILITY
        warm = base.shifted(+2.0)
        self.assertEqual(base.points[0], (-12.0, -0.80))
        self.assertEqual(warm(2.0), base(0.0))
        weak = base.scaled(0.5)
        self.assertAlmostEqual(weak(0.0), 0.5)
        self.assertEqual(weak(-12.0), -0.4)

    def test_humidity_dislikes_both_desiccation_and_saturation(self):
        self.assertLess(rs.HUMIDITY_COMFORT(0.2), rs.HUMIDITY_COMFORT(0.7))
        self.assertLess(rs.HUMIDITY_COMFORT(1.0), rs.HUMIDITY_COMFORT(0.7))


class ShapeHelperTest(unittest.TestCase):
    def test_smoothstep_is_continuous_and_saturates(self):
        self.assertEqual(rs.smoothstep(1.0, 1.0, 3.0), 0.0)
        self.assertEqual(rs.smoothstep(3.0, 1.0, 3.0), 1.0)
        self.assertEqual(rs.smoothstep(2.0, 1.0, 3.0), 0.5)
        self.assertLess(rs.smoothstep(1.5, 1.0, 3.0), 0.25)  # S-curve, not linear
        self.assertEqual(rs.smoothstep(5.0, 2.0, 2.0), 1.0)  # degenerate width

    def test_gaussian_peaks_at_zero_offset(self):
        self.assertEqual(rs.gaussian(0.0, 0.0, 3.0), 1.0)
        self.assertLess(rs.gaussian(9.0, 0.0, 3.0), 0.02)
        self.assertEqual(rs.gaussian(1.0, 0.0, 0.0), 0.0)  # degenerate width -> no score

    def test_cosine_bump_is_zero_outside_and_one_midway(self):
        self.assertEqual(rs.cosine_bump(9.0, 10.0, 20.0), 0.0)
        self.assertEqual(rs.cosine_bump(15.0, 10.0, 20.0), 1.0)
        self.assertEqual(rs.cosine_bump(21.0, 10.0, 20.0), 0.0)
        self.assertEqual(rs.cosine_bump(5.0, 5.0, 5.0), 0.0)  # degenerate


class WeightingTest(unittest.TestCase):
    def test_missing_factors_are_dropped_and_the_rest_renormalised(self):
        w = rs.normalise_weights({"a": 0.4, "b": None, "c": 0.2, "d": -0.1})
        self.assertEqual(set(w), {"a", "c"})
        self.assertAlmostEqual(sum(w.values()), 1.0)
        self.assertAlmostEqual(w["a"], 2 / 3)
        self.assertNotIn("d", w)  # negative weights are meaningless and dropped

    def test_all_missing_gives_an_empty_weight_set_not_a_division_error(self):
        self.assertEqual(rs.normalise_weights({"a": None, "b": 0.0}), {})

    def test_weighted_score_reports_coverage_and_dropped_names(self):
        weights = {"a": 0.5, "b": 0.3, "c": 0.2}
        score, cover, dropped = rs.weighted_score({"a": 1.0, "b": None, "c": 0.5}, weights)
        self.assertAlmostEqual(score, (0.5 + 0.1) / 0.7, places=6)
        self.assertAlmostEqual(cover, 0.7)
        self.assertEqual(dropped, ["b"])

    def test_score_is_bounded_even_with_extremes(self):
        score, _, _ = rs.weighted_score({"a": 40.0, "b": -90.0}, {"a": 0.5, "b": 0.5})
        self.assertTrue(-1.0 <= score <= 1.0)

    def test_required_factor_missing_voids_the_evaluation(self):
        weights = {"solunar": 0.4, "wind": 0.6}
        score, cover, dropped = rs.weighted_score({"wind": 0.9}, weights, require=("solunar",))
        self.assertEqual(score, 0.0)
        self.assertEqual(cover, 0.0)
        self.assertIn("solunar", dropped)

    def test_no_usable_factors_reports_zero_coverage(self):
        score, cover, _ = rs.weighted_score({}, {"a": 1.0})
        self.assertEqual((score, cover), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
