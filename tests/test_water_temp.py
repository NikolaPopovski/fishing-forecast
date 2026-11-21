"""Offline tests for model.water_temp: flux signs, inertia ordering, source fusion, anomaly wiring.

These assert physics-shaped properties (signs, orderings, bounds) rather than tuned numbers, so
they survive recalibration of the coefficients but catch a wrong sign or a unit slip.
"""
from __future__ import annotations

import math
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "custom_components" / "fishing"))   # appended, never prepended: select.py must not shadow the stdlib

from model import climatology as cl  # noqa: E402
from model import water_temp as wt  # noqa: E402

LAT, LON = 52.7, 5.3
YEAR0 = datetime(2025, 1, 1)
#: monthly mean air temperature of a temperate-maritime site (Jan..Dec), degC
MONTHLY_AIR = [3.5, 3.7, 6.0, 9.0, 13.0, 16.0, 18.5, 18.3, 15.0, 11.0, 7.0, 4.5]


def mean(values):
    values = list(values)
    return sum(values) / len(values)


def maritime_air(days=365, start=YEAR0):
    """Hourly air temperature: smooth seasonal cycle + a diurnal swing that is larger in summer."""
    out = []
    for i in range(24 * days):
        t = start + timedelta(hours=i)
        f = (t.month - 1) + (t.day - 1) / 30.0
        a, b, fr = int(f) % 12, (int(f) + 1) % 12, f - int(f)
        base = MONTHLY_AIR[a] * (1 - fr) + MONTHLY_AIR[b] * fr
        amp = 1.5 + 2.0 * max(0.0, math.sin(2 * math.pi * (t.timetuple().tm_yday - 80) / 365.0))
        out.append((t, base + amp * math.sin(2 * math.pi * (t.hour - 9) / 24.0)))
    return out


def const(air, value):
    return [(t, float(value)) for t, _ in air]


class FluxTest(unittest.TestCase):
    def test_shortwave_is_zero_at_night_and_cloud_reduces_it(self):
        midnight = datetime(2025, 6, 21, 0, 0)
        noon = datetime(2025, 6, 21, 11, 0)
        self.assertEqual(wt.shortwave_proxy(midnight, LAT, LON, 0.0), 0.0)
        clear = wt.shortwave_proxy(noon, LAT, LON, 0.0)
        overcast = wt.shortwave_proxy(noon, LAT, LON, 1.0)
        self.assertGreater(clear, 600.0)
        self.assertLess(clear, 1361.0)              # never more than the solar constant
        self.assertTrue(0.0 < overcast < 0.3 * clear)

    def test_longwave_loss_grows_with_dry_air_and_clear_sky(self):
        humid_clear = wt.net_longwave(10.0, 10.0, 9.0, 0.0)
        dry_clear = wt.net_longwave(10.0, 10.0, -5.0, 0.0)
        overcast = wt.net_longwave(10.0, 10.0, 5.0, 1.0)
        self.assertGreater(dry_clear, humid_clear)
        self.assertGreater(humid_clear, overcast)
        self.assertGreater(overcast, 0.0)           # water 10 degC is still losing heat to a 5 degC dewpoint sky

    def test_evaporation_grows_with_wind_and_dry_air(self):
        base = wt.evaporative_flux(15.0, 15.0, 0.6, 3.0)
        self.assertGreater(wt.evaporative_flux(15.0, 15.0, 0.6, 8.0), base)
        self.assertGreater(wt.evaporative_flux(15.0, 15.0, 0.3, 3.0), base)
        self.assertLess(wt.evaporative_flux(15.0, 15.0, 0.95, 3.0), base)

    def test_sensible_flux_direction_follows_the_temperature_difference(self):
        self.assertLess(wt.sensible_flux(15.0, 5.0, 3.0, 1.0), 0.0)     # warm water loses to cold air
        self.assertGreater(wt.sensible_flux(15.0, 25.0, 3.0, 1.0), 0.0)
        self.assertEqual(wt.sensible_flux(15.0, 15.0, 3.0, 1.0), 0.0)
        self.assertGreater(abs(wt.sensible_flux(15.0, 5.0, 8.0, 1.0)), abs(wt.sensible_flux(15.0, 5.0, 1.0, 1.0)))

    def test_missing_inputs_use_defaults_instead_of_raising(self):
        self.assertTrue(math.isfinite(wt.net_longwave(10.0, 10.0, None, 0.5)))
        self.assertTrue(math.isfinite(wt.evaporative_flux(10.0, 10.0, None, None)))
        self.assertTrue(math.isfinite(wt.sensible_flux(10.0, 10.0, None, 1.0)))


class BodyAndLayersTest(unittest.TestCase):
    def test_every_class_has_the_keys_model_series_reads(self):
        for kind, spec in wt.BODY_CLASSES.items():
            with self.subTest(kind=kind):
                self.assertTrue({"mix_m", "film_mult", "advection", "inertia", "shallow"} <= set(spec))
                self.assertTrue(0.0 <= wt.WaterBody(kind=kind).inertia <= 1.0)

    def test_unknown_kind_falls_back_to_a_small_lake(self):
        self.assertEqual(wt.WaterBody(kind="bathtub").spec, wt.BODY_CLASSES["lake_small"])

    def test_depth_caps_the_mixed_layer(self):
        self.assertAlmostEqual(wt.WaterBody(kind="lake_large", mean_depth_m=2.0).mixed_layer_m, 2.0)
        self.assertGreaterEqual(wt.WaterBody(kind="small_pond", mean_depth_m=0.01).mixed_layer_m, 0.15)

    def test_flow_raises_advection_but_never_lowers_it(self):
        self.assertGreater(wt.WaterBody(kind="lake_small", flow_index=0.8).advection, wt.WaterBody(kind="lake_small").advection)
        self.assertEqual(wt.WaterBody(kind="river", flow_index=0.1).advection, wt.BODY_CLASSES["river"]["advection"])
        self.assertLessEqual(wt.WaterBody(kind="river", flow_index=5.0).advection, 0.95)

    def test_slow_store_is_slower_for_bigger_water(self):
        self.assertGreater(wt.deep_reservoir_tau_days(0.9), wt.deep_reservoir_tau_days(0.1))
        self.assertLess(wt.deep_coupling_per_h(0.9), wt.deep_coupling_per_h(0.1))

    def test_summer_stratification_shrinks_the_layer_of_deep_water_only(self):
        self.assertEqual(wt.stratification_factor(5.0, False), 1.0)
        self.assertLess(wt.stratification_factor(22.0, False), wt.stratification_factor(12.0, False))
        self.assertGreaterEqual(wt.stratification_factor(40.0, False), 0.18)
        self.assertEqual(wt.stratification_factor(22.0, True), 1.0)

    def test_inverse_stratification_needs_depth_and_cold(self):
        self.assertEqual(wt.inverse_stratification_factor(1.0, 0.5), 1.0)           # shallow: it just freezes
        self.assertEqual(wt.inverse_stratification_factor(6.0, 6.0), 1.0)           # not cold enough
        deep_cold = wt.inverse_stratification_factor(0.5, 6.0)
        self.assertLess(deep_cold, wt.inverse_stratification_factor(3.0, 6.0))
        self.assertGreaterEqual(deep_cold, 0.12)


class ModelSeriesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.air = maritime_air()
        cls.cloud = const(cls.air, 70.0)
        cls.rh = const(cls.air, 80.0)
        cls.wind = const(cls.air, 4.5)
        cls.runs = {}
        for kind in ("ephemeral_ditch", "small_pond", "shallow_stream", "lake_small", "lake_large", "coastal_sea"):
            cls.runs[kind] = wt.model_series(
                cls.air, wt.WaterBody(kind=kind), LAT, LON, cloud=cls.cloud, humidity=cls.rh, wind_speed=cls.wind
            )

    def monthly(self, kind, month):
        s, _ = self.runs[kind]
        warm_up = YEAR0 + timedelta(days=30)
        return mean(v for t, v in s if t.month == month and t >= warm_up)

    def test_series_and_diagnostics_line_up(self):
        s, d = self.runs["lake_small"]
        self.assertEqual(len(s), len(self.air))
        self.assertEqual(len(d), len(s))
        self.assertEqual([t for t, _ in s], [x.when for x in d])

    def test_water_stays_inside_its_liquid_window(self):
        for kind, (s, _) in self.runs.items():
            with self.subTest(kind=kind):
                self.assertTrue(all(0.0 <= v <= 42.0 for _, v in s))

    def test_annual_cycle_is_plausible_for_every_body(self):
        # generous bands around real temperate-maritime water; tight enough to catch a runaway or a sign flip
        bands = {1: (0.5, 6.5), 4: (6.5, 12.5), 6: (12.5, 19.0), 8: (13.0, 19.0), 10: (6.5, 12.5), 12: (0.5, 6.5)}
        for kind in self.runs:
            for month, (lo, hi) in bands.items():
                with self.subTest(kind=kind, month=month):
                    self.assertTrue(lo <= self.monthly(kind, month) <= hi, f"{kind} month {month}: {self.monthly(kind, month):.2f}")

    def test_big_water_is_warmer_in_winter_and_cooler_in_early_summer(self):
        for month, sign in ((1, 1), (12, 1), (6, -1)):
            ordered = [self.monthly(k, month) for k in ("small_pond", "lake_small", "lake_large")]
            with self.subTest(month=month):
                self.assertTrue(
                    ordered[0] * sign < ordered[1] * sign < ordered[2] * sign, f"month {month}: {ordered}"
                )

    def test_diurnal_amplitude_ranks_by_inertia(self):
        def amplitude(kind):
            s, _ = self.runs[kind]
            by_hour = {}
            for t, v in s:
                if t.month == 7:
                    by_hour.setdefault(t.hour, []).append(v)
            means = [mean(x) for x in by_hour.values()]
            return max(means) - min(means)

        amps = [amplitude(k) for k in ("ephemeral_ditch", "small_pond", "lake_small", "lake_large")]
        self.assertTrue(amps[0] > amps[1] > amps[2] > amps[3] > 0.0, amps)

    def test_a_cold_snap_cools_everything_and_hits_small_water_first(self):
        snap_start = YEAR0 + timedelta(days=365)
        snap = [(snap_start + timedelta(hours=i), -3.0 + 1.5 * math.sin(2 * math.pi * ((i % 24) - 9) / 24.0)) for i in range(24 * 35)]
        air = self.air + snap
        end = {}
        for kind in ("ephemeral_ditch", "lake_large"):
            s, _ = wt.model_series(
                air, wt.WaterBody(kind=kind), LAT, LON, cloud=const(air, 70.0), humidity=const(air, 85.0), wind_speed=const(air, 3.5)
            )
            end[kind] = mean(v for t, v in s if t >= snap_start + timedelta(days=30))
        self.assertLess(end["ephemeral_ditch"], 1.0)
        self.assertGreaterEqual(end["lake_large"], end["ephemeral_ditch"])

    def test_cloud_is_read_as_percent_not_a_fraction(self):
        # regression: 20 % and 80 % cloud both used to clamp to "fully overcast"
        air = maritime_air(days=60, start=datetime(2025, 5, 1))
        body = wt.WaterBody(kind="lake_small")
        _, clear = wt.model_series(air, body, LAT, LON, cloud=const(air, 20.0))
        _, dull = wt.model_series(air, body, LAT, LON, cloud=const(air, 80.0))
        self.assertGreater(mean(x.shortwave_wm2 for x in clear), 1.5 * mean(x.shortwave_wm2 for x in dull))

    def test_humidity_is_read_as_percent(self):
        air = maritime_air(days=60, start=datetime(2025, 5, 1))
        body = wt.WaterBody(kind="lake_small")
        _, dry = wt.model_series(air, body, LAT, LON, humidity=const(air, 30.0))
        _, damp = wt.model_series(air, body, LAT, LON, humidity=const(air, 90.0))
        self.assertGreater(mean(x.evaporative_wm2 for x in damp), mean(x.evaporative_wm2 for x in dry))   # less negative = less loss

    def test_shade_keeps_summer_water_cooler(self):
        air = maritime_air(days=120, start=datetime(2025, 4, 1))
        open_s, _ = wt.model_series(air, wt.WaterBody(kind="small_pond"), LAT, LON, cloud=const(air, 30.0))
        shady_s, _ = wt.model_series(air, wt.WaterBody(kind="small_pond", shading_index=1.0), LAT, LON, cloud=const(air, 30.0))
        self.assertLess(mean(v for _, v in shady_s[-24 * 20:]), mean(v for _, v in open_s[-24 * 20:]))

    def test_start_water_temp_and_start_at_are_honoured(self):
        air = maritime_air(days=40, start=datetime(2025, 5, 1))
        begin = datetime(2025, 5, 20)
        s, _ = wt.model_series(air, wt.WaterBody(kind="lake_large"), LAT, LON, start_water_temp=4.0, start_at=begin)
        self.assertEqual(s[0][0], begin)
        self.assertLess(abs(s[0][1] - 4.0), 1.5)         # one step from the seed

    def test_too_little_air_data_returns_nothing(self):
        self.assertEqual(wt.model_series([], wt.WaterBody(), LAT, LON), ([], []))
        self.assertEqual(wt.model_series(maritime_air(days=1)[:20], wt.WaterBody(), LAT, LON), ([], []))


class FusionTest(unittest.TestCase):
    def test_precedence_is_sensor_then_sst_then_model(self):
        self.assertEqual(wt.fuse_water_temp(12.0, 10.0, 9.0), (12.0, "sensor"))
        self.assertEqual(wt.fuse_water_temp(None, 10.0, 9.0), (10.0, "sst"))
        self.assertEqual(wt.fuse_water_temp(None, None, 9.0), (9.0, "model"))
        self.assertEqual(wt.fuse_water_temp(None, None, None), (None, "none"))

    def test_a_stale_sensor_loses_to_everything_else_but_beats_nothing(self):
        self.assertEqual(wt.fuse_water_temp(12.0, 10.0, 9.0, sensor_age_hours=40.0), (10.0, "sst"))
        self.assertEqual(wt.fuse_water_temp(12.0, None, 9.0, sensor_age_hours=40.0), (9.0, "model"))
        self.assertEqual(wt.fuse_water_temp(12.0, None, None, sensor_age_hours=40.0), (12.0, "sensor_stale"))

    def test_age_limit_is_inclusive_and_configurable(self):
        self.assertEqual(wt.fuse_water_temp(12.0, 10.0, None, sensor_age_hours=30.0)[1], "sensor")
        self.assertEqual(wt.fuse_water_temp(12.0, 10.0, None, sensor_age_hours=5.0, sensor_max_age_hours=4.0)[1], "sst")


class AnalyseTest(unittest.TestCase):
    NOW = datetime(2025, 6, 20, 12, 0)

    @staticmethod
    def ramp(per_day, days=9, end_value=15.0):
        n = 24 * days
        return [(AnalyseTest.NOW - timedelta(hours=n - i), end_value - per_day * (n - i) / 24.0) for i in range(n + 1)]

    @staticmethod
    def june_normals():
        clim = cl.Climatology(LAT, 0.0, "lake")
        for i in range(90):
            d = date(2025, 5, 1) + timedelta(days=i)
            clim.ingest_daily(d, {"water_temp": 15.0 + 0.8 * math.sin(i * 1.7)})
        return clim

    def test_no_data_means_no_score_and_says_so(self):
        st = wt.analyse([], self.NOW)
        self.assertIsNone(st.temp)
        self.assertEqual(st.source, "none")
        self.assertEqual(st.score, 0.0)
        self.assertTrue(st.notes)

    def test_steady_series_reads_steady(self):
        st = wt.analyse(self.ramp(0.0), self.NOW)
        self.assertEqual(st.trend, "steady")
        self.assertAlmostEqual(st.change_7d, 0.0, places=6)
        self.assertIsNone(st.shock)

    def test_trend_words_follow_the_seven_day_change(self):
        self.assertEqual(wt.analyse(self.ramp(0.3), self.NOW).trend, "warming fast")   # +2.1 over 7 days
        self.assertEqual(wt.analyse(self.ramp(-0.3), self.NOW).trend, "cooling fast")
        self.assertEqual(wt.analyse(self.ramp(0.1), self.NOW).trend, "warming")
        self.assertEqual(wt.analyse(self.ramp(-0.1), self.NOW).trend, "cooling")

    def test_thermal_shock_is_flagged(self):
        st = wt.analyse(self.ramp(3.0, days=3), self.NOW)
        self.assertAlmostEqual(st.change_24h, 3.0, places=6)
        self.assertEqual(st.shock, st.change_24h)
        self.assertTrue(any("thermal shock" in n for n in st.notes))

    def test_level_can_come_from_a_sensor_while_trend_comes_from_the_model(self):
        st = wt.analyse(self.ramp(0.0), self.NOW, fused_value=19.0, source="sensor")
        self.assertEqual(st.temp, 19.0)
        self.assertEqual(st.source, "sensor")
        self.assertAlmostEqual(st.change_24h, 4.0, places=6)         # 19 vs the modelled 15 a day ago
        self.assertTrue(any("external source" in n for n in st.notes))

    def test_anomaly_in_sigma_against_the_spots_own_normal(self):
        clim = self.june_normals()
        warm = wt.analyse(self.ramp(0.0), self.NOW, clim=clim, fused_value=19.0, source="sensor")
        normal = wt.analyse(self.ramp(0.0), self.NOW, clim=clim, fused_value=15.0, source="sensor")
        self.assertGreater(warm.anomaly_sigma, 2.0)
        self.assertAlmostEqual(warm.anomaly_raw, 4.0, delta=0.8)
        self.assertAlmostEqual(normal.anomaly_sigma, 0.0, delta=1.0)
        self.assertIsNotNone(warm.normal)

    def test_empty_climatology_is_reported_not_faked(self):
        st = wt.analyse(self.ramp(0.0), self.NOW, clim=cl.Climatology(LAT, 0.0))
        self.assertIsNone(st.anomaly_sigma)
        self.assertTrue(any("no water climatology" in n for n in st.notes))

    def test_score_is_best_at_the_species_optimum(self):
        series = self.ramp(0.0)
        at_opt = wt.analyse(series, self.NOW, species_optimum_c=15.0)
        far = wt.analyse(series, self.NOW, species_optimum_c=26.0)
        self.assertAlmostEqual(at_opt.species_offset_c, 0.0)
        self.assertAlmostEqual(far.species_offset_c, -11.0)
        self.assertGreater(at_opt.score, far.score)

    def test_without_a_species_only_the_trend_scores(self):
        st = wt.analyse(self.ramp(0.0), self.NOW)
        self.assertIsNone(st.species_offset_c)
        self.assertTrue(-1.0 <= st.score <= 1.0)

    def test_as_dict_is_json_friendly(self):
        import json

        json.dumps(wt.analyse(self.ramp(0.2), self.NOW, species_optimum_c=15.0).as_dict())


if __name__ == "__main__":
    unittest.main()
