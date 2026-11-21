"""Offline tests for model.pressure: tide-corrected tendency, anomaly, fronts, graceful degradation."""
from __future__ import annotations

import math
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "custom_components" / "fishing"))   # appended, never prepended: select.py must not shadow the stdlib
sys.path.insert(0, str(Path(__file__).resolve().parent))

from model import pressure as pr  # noqa: E402
from support import synthetic_store  # noqa: E402

T0 = datetime(2025, 6, 1)


def hourly(fn, hours, start=T0):
    return [(start + timedelta(hours=i), fn(i, start + timedelta(hours=i))) for i in range(hours)]


def ramp_air():
    """Flat 15 degC for a day and a half, then 0.5 degC/h cooling from T0 on (36 h of lead-in so the
    daily-cycle correction has the previous day to compare against)."""
    return hourly(lambda i, t: 15.0 - 0.5 * max(0.0, (t - T0).total_seconds() / 3600.0), 48, start=T0 - timedelta(hours=36))


def tide(i, t, amp=0.6):
    """Semi-diurnal solar tide: maxima at 10:00 and 22:00 local, nothing synoptic in it."""
    return 1015.0 + amp * math.cos(2 * math.pi * (t.hour - 10) / 12.0)


class DiurnalTideTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.series = hourly(tide, 24 * 12)
        cls.cycle = pr.build_diurnal_cycle(cls.series)

    def test_cycle_is_zero_mean_and_has_the_tide_amplitude(self):
        self.assertEqual(len(self.cycle), 24)
        self.assertAlmostEqual(sum(self.cycle) / 24, 0.0, places=9)
        self.assertAlmostEqual(max(self.cycle) - min(self.cycle), 1.2, places=6)

    def test_corrected_tendency_of_a_pure_tide_is_about_zero(self):
        # the whole point: a tide with no weather in it must not look like a front at ANY hour
        for hour in range(24):
            when = T0 + timedelta(days=11, hours=hour)
            with self.subTest(hour=hour):
                a = pr.analyse(self.series, when, diurnal_cycle=self.cycle)
                self.assertLess(abs(a.tendency_3h), 0.05)
                self.assertEqual(a.trend_label, "steady")
                self.assertTrue(a.diurnal_corrected)

    def test_without_the_correction_the_tide_manufactures_a_falling_barometer(self):
        a = pr.analyse(self.series, T0 + timedelta(days=11, hours=3))
        self.assertLess(a.tendency_3h_raw, -0.5)
        self.assertEqual(a.trend_label, "falling")
        self.assertFalse(a.diurnal_corrected)
        self.assertTrue(any("tide not subtracted" in n for n in a.notes))

    def test_real_fall_survives_the_correction(self):
        # tide plus a genuine 1 hPa/h fall: correction must remove the wobble but keep the fall
        s = hourly(lambda i, t: tide(i, t) - (1.0 * max(0, i - 24 * 11 - 6)), 24 * 11 + 12)
        a = pr.analyse(s, s[-1][0], diurnal_cycle=self.cycle)
        self.assertAlmostEqual(a.tendency_3h, -3.0, delta=0.4)
        self.assertEqual(a.trend_label, "falling fast")

    def test_short_history_gives_no_cycle(self):
        self.assertEqual(pr.build_diurnal_cycle(hourly(tide, 8)), [])
        self.assertEqual(pr.build_diurnal_cycle([]), [])


class StateTest(unittest.TestCase):
    def setUp(self):
        self.now = T0 + timedelta(hours=47)
        self.steady = hourly(lambda i, t: 1025.0, 48)
        self.falling = hourly(lambda i, t: 1025.0 - 1.0 * max(0, i - 30), 48)

    def test_steady_high(self):
        a = pr.analyse(self.steady, self.now)
        self.assertEqual(a.trend_label, "steady")
        self.assertAlmostEqual(a.tendency_3h, 0.0, places=9)
        self.assertAlmostEqual(a.stability_24h, 0.0, places=9)
        self.assertEqual(a.value, 1025.0)

    def test_falling_front_scores_better_for_the_tendency_than_steady(self):
        steady = pr.analyse(self.steady, self.now)
        falling = pr.analyse(self.falling, self.now)
        self.assertEqual(falling.trend_label, "falling fast")
        self.assertAlmostEqual(falling.tendency_3h, -3.0, places=6)
        self.assertGreater(falling.parts["tendency"], steady.parts["tendency"])
        self.assertGreater(falling.front_index, 0.2)   # a deep ongoing fall is a live front

    def test_rising_barometer_is_labelled(self):
        rising = hourly(lambda i, t: 1000.0 + 1.0 * i, 12)
        self.assertEqual(pr.analyse(rising, rising[-1][0]).trend_label, "rising fast")

    def test_stability_is_the_24h_range(self):
        wobbly = hourly(lambda i, t: 1015.0 + (6.0 if i % 2 else 0.0), 30)
        self.assertAlmostEqual(pr.analyse(wobbly, wobbly[-1][0]).stability_24h, 6.0)

    def test_as_dict_is_json_friendly(self):
        import json

        json.dumps(pr.analyse(self.falling, self.now).as_dict())


class AnomalyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clim = synthetic_store()          # ingests station pressure, so use source='station'
        cls.now = T0 + timedelta(hours=47)

    def anomaly(self, hpa):
        s = hourly(lambda i, t: float(hpa), 48)
        return pr.analyse(s, self.now, clim=self.clim, source=pr.STATION)

    def test_low_is_negative_sigma_and_high_is_positive(self):
        low, mid, high = self.anomaly(985), self.anomaly(1012), self.anomaly(1040)
        self.assertLess(low.anomaly_sigma, -2.0)
        self.assertAlmostEqual(mid.anomaly_sigma, 0.0, delta=0.4)
        self.assertGreater(high.anomaly_sigma, 2.0)
        self.assertIn("anomaly", low.parts)
        self.assertIsNotNone(mid.normal)

    def test_no_climatology_drops_the_anomaly_factor_without_inventing_one(self):
        a = pr.analyse(hourly(lambda i, t: 1000.0, 48), self.now)
        self.assertIsNone(a.anomaly_sigma)
        self.assertNotIn("anomaly", a.parts)
        self.assertIn("tendency", a.parts)    # the rest of the score still renormalises over what exists


class FrontTest(unittest.TestCase):
    def setUp(self):
        self.past = hourly(lambda i, t: 1020.0 - 0.8 * i, 12)
        low = 1020.0 - 0.8 * 12
        self.forecast = [(T0 + timedelta(hours=12 + i), low + (-1.0 * i if i < 6 else 1.2 * (i - 6))) for i in range(40)]
        self.now = T0 + timedelta(hours=11)

    def go(self, **kw):
        return pr.analyse(self.past, self.now, forecast=self.forecast, **kw)

    def test_trough_in_the_forecast_is_located(self):
        a = self.go()
        self.assertIsNotNone(a.front_eta_hours)
        self.assertTrue(0.0 < a.front_eta_hours < 24.0)
        self.assertGreater(a.front_index, 0.5)

    def test_corroboration_measures_veer_cloud_and_airmass_change(self):
        a = self.go(
            wind_dir=hourly(lambda i, t: 200.0 + 8.0 * i, 12),
            cloud=hourly(lambda i, t: 20.0 + 8.0 * i, 12),
            air_temp=ramp_air(),
        )
        self.assertGreater(a.wind_shift_deg, 30)
        self.assertGreater(a.cloud_rise, 30.0)      # cloud is in percent
        self.assertLess(a.temp_jump_c, -2.0)

    def test_the_daily_temperature_cycle_alone_is_not_an_airmass_change(self):
        # regression: raw "now minus 6 h ago" is about -5 degC every evening and read as a cold front
        days = hourly(lambda i, t: 15.0 + 4.0 * math.sin(2 * math.pi * (t.hour - 9) / 24.0), 24 * 5)
        for hour in range(24):
            when = T0 + timedelta(days=4, hours=hour)
            with self.subTest(hour=hour):
                a = pr.analyse(self.past, when, air_temp=days)
                self.assertAlmostEqual(a.temp_jump_c, 0.0, places=6)

    def test_a_real_airmass_change_shows_through_the_daily_cycle(self):
        def cycle(i, t):
            drop = -4.0 if (t - T0) >= timedelta(days=4, hours=8) else 0.0     # starts inside the 06:00-12:00 window
            return 15.0 + 4.0 * math.sin(2 * math.pi * (t.hour - 9) / 24.0) + drop

        days = hourly(cycle, 24 * 5)
        a = pr.analyse(self.past, T0 + timedelta(days=4, hours=12), air_temp=days)
        self.assertAlmostEqual(a.temp_jump_c, -4.0, places=6)

    def test_without_the_previous_day_the_jump_cannot_be_separated_so_it_is_unknown(self):
        short = hourly(lambda i, t: 15.0 - 0.5 * i, 12)
        self.assertIsNone(pr.analyse(self.past, self.now, air_temp=short).temp_jump_c)

    def test_wind_shift_wraps_across_north(self):
        a = self.go(wind_dir=hourly(lambda i, t: (350.0 + 5.0 * i) % 360.0, 12))
        self.assertGreater(a.wind_shift_deg, 0)      # 350 -> 40 is a +50 veer, not -310
        self.assertLess(a.wind_shift_deg, 90)

    def test_uncorroborated_swing_is_discounted(self):
        bare = self.go()
        corroborated = self.go(
            wind_dir=hourly(lambda i, t: 200.0 + 8.0 * i, 12),
            cloud=hourly(lambda i, t: 20.0 + 8.0 * i, 12),
            air_temp=ramp_air(),
        )
        flat = self.go(
            wind_dir=hourly(lambda i, t: 200.0, 12),
            cloud=hourly(lambda i, t: 20.0, 12),
            air_temp=hourly(lambda i, t: 15.0, 48, start=T0 - timedelta(hours=36)),
        )
        self.assertLess(flat.front_index, bare.front_index)
        self.assertLess(flat.front_index, corroborated.front_index)
        self.assertAlmostEqual(corroborated.front_index, bare.front_index, places=6)   # full evidence, no discount
        self.assertTrue(any("not backed" in n for n in flat.notes))

    def test_no_forecast_caps_the_front_instead_of_locating_it(self):
        a = pr.analyse(self.past, self.now)
        self.assertIsNone(a.front_eta_hours)
        self.assertLessEqual(a.front_index, 0.55)


class DegradationTest(unittest.TestCase):
    def test_empty_history(self):
        a = pr.analyse([], T0)
        self.assertEqual(a.parts, {})
        self.assertIsNone(a.value)
        self.assertIsNone(a.tendency_3h)
        self.assertIsNone(a.front_index)
        self.assertEqual(a.trend_label, "unknown")
        self.assertFalse(a.diurnal_corrected)
        self.assertEqual(a.score, 0.0)
        self.assertTrue(any("not enough barometric history" in n for n in a.notes))

    def test_single_sample_has_a_value_but_no_tendency(self):
        a = pr.analyse([(T0, 1010.0)], T0)
        self.assertEqual(a.value, 1010.0)
        self.assertIsNone(a.tendency_3h)


class StationAndMslpTest(unittest.TestCase):
    def test_round_trip(self):
        for elev, temp in ((0.0, 10.0), (150.0, 5.0), (800.0, -2.0)):
            station = pr.sea_level_to_station_local(1018.0, elev, temp)
            self.assertAlmostEqual(pr.mslp_from_station(station, elev, temp), 1018.0, delta=0.05)
            self.assertAlmostEqual(pr.expected_station_from_mslp(1018.0, elev, temp), station, places=9)

    def test_higher_ground_reads_lower(self):
        self.assertLess(pr.sea_level_to_station_local(1015.0, 500.0, 8.0), pr.sea_level_to_station_local(1015.0, 100.0, 8.0))

    def test_sensor_offset_finds_a_constant_bias(self):
        mslp = hourly(lambda i, t: 1015.0 + 3.0 * math.sin(i / 7.0), 60)
        station = [(t, pr.sea_level_to_station_local(v, 300.0, 8.0) + 1.5) for t, v in mslp]
        offset, spread = pr.sensor_offset(station, mslp, 300.0, 8.0)
        self.assertAlmostEqual(offset, 1.5, places=6)
        self.assertAlmostEqual(spread, 0.0, places=6)

    def test_sensor_offset_needs_enough_overlap(self):
        short = hourly(lambda i, t: 1015.0, 4)
        self.assertEqual(pr.sensor_offset(short, short, 0.0, 10.0), (None, None))


if __name__ == "__main__":
    unittest.main()
