"""Offline tests for model.scoring: the acceptance cases for the factor engine plus its honesty rules.

Each scenario perturbs the neutral baseline in support.spot_context() in exactly one way, so a
difference in the score can only come from that one thing.
"""
from __future__ import annotations

import dataclasses
import json
import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "custom_components" / "fishing"))   # appended, never prepended: select.py must not shadow the stdlib
sys.path.insert(0, str(Path(__file__).resolve().parent))

from model import astronomy as astro  # noqa: E402
from model import scoring as sc  # noqa: E402
from model import species as sp  # noqa: E402
from model.context import SpotContext  # noqa: E402
from support import SPOT_START, spot_context  # noqa: E402

NOW = SPOT_START + timedelta(days=22)           # 2025-06-23 00:00 local, first "forecast" day
UTC = timezone.utc
SPOT_KINDS = ("stream", "river", "lake", "reservoir", "estuary", "coast")


def pike():
    return sp.profile("pike")


def winter_air(i, t):
    return 5.0 + 2.0 * math.sin(2 * math.pi * (t.hour - 9) / 24.0)


def moon_night_context(kind="full"):
    """A December spot and the instant ~1 h after local midnight nearest to a full/new moon."""
    crossing = astro.find_phase_crossing(kind, datetime(2025, 11, 25, tzinfo=UTC))
    day = (crossing + timedelta(hours=14)).date()          # the night *after* the phase instant
    when = datetime(day.year, day.month, day.day, 1, 0) + timedelta(days=1) * 0
    start = datetime(day.year, day.month, day.day) - timedelta(days=22)
    return spot_context(start=start, days=22, ahead=4, air=winter_air), when


class BandTest(unittest.TestCase):
    def test_band_words(self):
        self.assertEqual(
            [sc.band(x) for x in (-0.9, -0.3, 0.0, 0.3, 0.8)],
            ["terrible", "poor", "fair", "good", "excellent"],
        )

    def test_band_is_monotone(self):
        order = ["terrible", "poor", "fair", "good", "excellent"]
        words = [sc.band(-1.0 + i / 50.0) for i in range(101)]
        self.assertEqual(sorted(words, key=order.index), words)


class TimeContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = spot_context()

    def test_naive_local_aware_local_and_aware_utc_agree(self):
        local = datetime(2025, 6, 23, 5, 0)
        a = sc.evaluate(self.ctx, local, pike())
        b = sc.evaluate(self.ctx, local.replace(tzinfo=self.ctx.tz), pike())
        c = sc.evaluate(self.ctx, datetime(2025, 6, 23, 3, 0, tzinfo=UTC), pike())      # 05:00 at UTC+2
        self.assertEqual(a.when, local)
        self.assertEqual(b.when, local)
        self.assertEqual(c.when, local)
        self.assertAlmostEqual(a.score, b.score, places=9)
        self.assertAlmostEqual(a.score, c.score, places=9)

    def test_helpers(self):
        self.assertEqual(sc.to_local(self.ctx, datetime(2025, 6, 23, 3, 0, tzinfo=UTC)), datetime(2025, 6, 23, 5, 0))
        self.assertEqual(sc.to_aware(self.ctx, datetime(2025, 6, 23, 5, 0)).utcoffset(), timedelta(hours=2))
        self.assertIsNone(sc.local_event(self.ctx, None))
        ev = sc.local_event(self.ctx, self.ctx.astronomy_for(datetime(2025, 6, 23, 12)).light.sunrise)
        self.assertIsNone(ev.tzinfo)
        self.assertTrue(datetime(2025, 6, 23, 4) < ev < datetime(2025, 6, 23, 6))


class StructureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = spot_context()
        cls.ev = sc.evaluate(cls.ctx, datetime(2025, 6, 23, 12, 0), pike())

    def test_ranges_and_types(self):
        ev = self.ev
        self.assertTrue(0.0 <= ev.score <= 10.0)
        self.assertTrue(-1.0 <= ev.raw <= 1.0)
        self.assertTrue(0.0 <= ev.confidence <= 1.0)
        self.assertTrue(0.0 <= ev.coverage <= 1.0)
        self.assertIn(ev.band, ("terrible", "poor", "fair", "good", "excellent"))
        self.assertEqual(ev.species_id, "pike")
        self.assertIsNone(ev.when.tzinfo)

    def test_every_factor_is_a_known_factor_name_with_a_sentence(self):
        for f in self.ev.factors:
            with self.subTest(factor=f.name):
                self.assertIn(f.name, sp.FACTORS)
                self.assertTrue(f.label)
                self.assertTrue(f.why)
                self.assertTrue(-1.0 <= f.score <= 1.0)

    def test_used_weights_sum_to_one_and_contributions_are_weight_times_score(self):
        used = [f for f in self.ev.factors if f.source != "unavailable"]
        self.assertAlmostEqual(sum(f.weight for f in used), 1.0, places=9)
        for f in used:
            self.assertAlmostEqual(f.contribution, f.weight * f.score, places=9)
        raw_before_season = sum(f.contribution for f in used)
        season = self.ev.season_factor
        expected = raw_before_season * season if raw_before_season > 0 else raw_before_season * (2.0 - season)
        self.assertAlmostEqual(self.ev.raw, max(-1.0, min(1.0, expected)), places=9)

    def test_shown_score_is_shrunk_toward_five_by_confidence(self):
        ev = self.ev
        unshrunk = (ev.raw + 1.0) * 5.0
        factor = 0.55 + 0.45 * ev.confidence
        self.assertAlmostEqual(ev.score, 5.0 + (unshrunk - 5.0) * factor, places=9)
        self.assertLessEqual(abs(ev.score - 5.0), abs(unshrunk - 5.0) + 1e-9)

    def test_serialises(self):
        json.dumps(self.ev.as_dict())
        json.dumps([s.as_dict() for s in sc.best_spans(self.ctx, NOW, 24, pike(), min_score=0.0)])

    def test_factor_lookup(self):
        self.assertEqual(self.ev.factor("wind").name, "wind")
        self.assertIsNone(self.ev.factor("tide"))


class NotApplicableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = spot_context()

    def test_landlocked_water_has_no_tide_factor_and_no_penalty_for_it(self):
        ev = sc.evaluate(self.ctx, datetime(2025, 6, 23, 12, 0), pike())
        self.assertIn("tide", ev.not_applicable)
        self.assertIsNone(ev.factor("tide"))
        self.assertAlmostEqual(ev.coverage, 1.0, places=9)

    def test_moonlight_is_not_a_daytime_question_and_uv_is_not_a_night_one(self):
        noon = sc.evaluate(self.ctx, datetime(2025, 6, 23, 13, 0), pike())
        night = sc.evaluate(self.ctx, datetime(2025, 6, 23, 1, 0), pike())
        self.assertIn("moonlight", noon.not_applicable)
        self.assertIsNone(noon.factor("moonlight"))
        self.assertIn("uv", night.not_applicable)
        self.assertIsNone(night.factor("uv"))
        self.assertIsNotNone(night.factor("moonlight"))
        self.assertAlmostEqual(night.coverage, 1.0, places=9)

    def test_tidal_water_gets_a_tide_factor(self):
        ctx = spot_context(kind="coast", tidal=True, tide_range_m=2.5, lon=4.4)
        ev = sc.evaluate(ctx, datetime(2025, 6, 23, 12, 0), sp.profile("European_bass"))
        tide = ev.factor("tide")
        self.assertIsNotNone(tide)
        self.assertTrue(0.0 <= tide.value <= 1.0)
        self.assertNotIn("tide", ev.not_applicable)


class AcceptanceTest(unittest.TestCase):
    """The behavioural cases from the handover (section 8.2)."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = spot_context()

    def test_pike_peaks_at_dusk_or_dawn_not_noon(self):
        curve = sc.hourly_curve(self.ctx, NOW, 24, pike())
        light = self.ctx.astronomy_for(NOW).light
        rise, sset = sc.local_event(self.ctx, light.sunrise), sc.local_event(self.ctx, light.sunset)

        def near(t, event, hours):
            return abs((t - event).total_seconds()) <= hours * 3600

        twilight = [ev.score for t, ev in curve if near(t, rise, 1.5) or near(t, sset, 1.5)]
        noon = [ev.score for t, ev in curve if 11 <= t.hour <= 14]
        peak_t, peak_ev = max(curve, key=lambda item: item[1].score)
        self.assertGreater(max(twilight), max(noon) + 0.3)
        self.assertFalse(10 <= peak_t.hour <= 16, peak_t)
        self.assertTrue(-10.0 <= peak_ev.sun_altitude <= 15.0, peak_ev.sun_altitude)   # sun on the horizon

    def test_a_falling_barometer_lifts_pike_more_than_a_steady_high(self):
        i0 = 24 * 22 + 12                                           # 12:00 on the evaluation day
        steady = spot_context(pressure=lambda i, t: 1025.0)
        falling = spot_context(pressure=lambda i, t: 1025.0 - (2.5 / 3.0) * min(3, max(0, i - (i0 - 3))) - 0.0)
        when = NOW + timedelta(hours=12)
        a, b = sc.evaluate(steady, when, pike()), sc.evaluate(falling, when, pike())
        self.assertAlmostEqual(b.pressure.tendency_3h, -2.5, delta=0.3)
        self.assertGreater(b.factor("pressure").score, a.factor("pressure").score)
        self.assertGreater(b.score, a.score)

    def test_zander_gains_more_than_pike_when_night_falls(self):
        ctx, when = moon_night_context("new")
        noon = when.replace(hour=12)
        z_gain = sc.evaluate(ctx, when, sp.profile("zander")).raw - sc.evaluate(ctx, noon, sp.profile("zander")).raw
        p_gain = sc.evaluate(ctx, when, pike()).raw - sc.evaluate(ctx, noon, pike()).raw
        self.assertGreater(z_gain, p_gain)

    def test_a_bright_full_moon_hurts_zander_and_helps_pike(self):
        ctx, when = moon_night_context("full")
        prepared = sc.prepare(ctx, when)
        self.assertGreater(prepared.moonshine, 0.4)                 # the moon really is up and bright
        dark = dataclasses.replace(prepared, moonshine=0.0)
        for species, direction in (("zander", -1), ("pike", +1)):
            lit = sc.evaluate(ctx, when, sp.profile(species), prepared)
            unlit = sc.evaluate(ctx, when, sp.profile(species), dark)
            with self.subTest(species=species):
                self.assertGreater((lit.raw - unlit.raw) * direction, 0.0)

    def test_moonlight_contribution_flips_sign_between_wels_and_flounder(self):
        ctx, when = moon_night_context("full")
        prepared = sc.prepare(ctx, when)
        wels = sc.evaluate(ctx, when, sp.profile("wels_catfish"), prepared).factor("moonlight")
        flounder = sc.evaluate(ctx, when, sp.profile("flounder_plaice"), prepared).factor("moonlight")
        self.assertLess(wels.contribution, 0.0)
        self.assertGreater(flounder.contribution, 0.0)

    def test_losing_the_river_gauge_lowers_confidence_without_moving_the_score_much(self):
        def gauge(d):
            return 100.0 * (1.0 + 0.3 * math.sin(d / 4.0))

        with_gauge = spot_context(kind="river", body="river", discharge=gauge)
        without = spot_context(kind="river", body="river")
        when = NOW + timedelta(hours=12)
        a, b = sc.evaluate(with_gauge, when, sp.profile("barbel")), sc.evaluate(without, when, sp.profile("barbel"))
        self.assertLess(b.confidence, a.confidence)
        self.assertLess(b.coverage, a.coverage)
        self.assertLessEqual(abs(a.score - b.score), 0.6)
        self.assertTrue(any("river-level" in n or "river level" in n or "level" in n for n in b.notes))

    def test_hourly_curve_is_deterministic_and_has_no_midnight_sawtooth(self):
        one = sc.hourly_curve(self.ctx, NOW - timedelta(hours=12), 48, pike())
        two = sc.hourly_curve(self.ctx, NOW - timedelta(hours=12), 48, pike())
        self.assertEqual([(t, e.score) for t, e in one], [(t, e.score) for t, e in two])
        scores = [e.score for _, e in one]
        self.assertEqual(len(scores), 48)
        steps = [abs(b - a) for a, b in zip(scores, scores[1:])]
        self.assertLess(max(steps), 1.2)
        midnight = next(i for i, (t, _) in enumerate(one) if t.hour == 0 and i > 0)
        self.assertLess(abs(scores[midnight] - scores[midnight - 1]), 1.2)

    def test_every_species_on_every_spot_kind_evaluates_at_a_temperate_and_a_polar_site(self):
        sites = (
            dict(),
            dict(lat=78.2, lon=15.6, utc_offset_min=60),                                          # Longyearbyen, midnight sun
            dict(lat=78.2, lon=15.6, utc_offset_min=60, start=datetime(2025, 11, 20)),           # ... and polar night
        )
        checked = 0
        for site in sites:
            for kind in SPOT_KINDS:
                ctx = spot_context(kind=kind, tidal=kind in ("estuary", "coast"), tide_range_m=1.5, **site)
                day0 = ctx.series["air_temp"][0][0] + timedelta(days=22)
                for hour in (0, 6, 12, 18):
                    when = day0 + timedelta(hours=hour)
                    prepared = sc.prepare(ctx, when)
                    for prof in sp.PROFILE_LIST:
                        with self.subTest(site=site.get("lat", 52.7), start=day0.month, kind=kind, hour=hour, species=prof.id):
                            ev = sc.evaluate(ctx, when, prof, prepared)
                            self.assertTrue(0.0 <= ev.score <= 10.0)
                            self.assertTrue(math.isfinite(ev.raw))
                            self.assertTrue(0.0 <= ev.confidence <= 1.0)
                            checked += 1
        self.assertEqual(checked, 3 * 6 * 4 * 22)


class DegradationTest(unittest.TestCase):
    def test_a_context_with_no_data_at_all_still_evaluates_and_says_so(self):
        ctx = SpotContext(name="bare", lat=52.7, lon=5.3, tz_offset_min=120)
        ev = sc.evaluate(ctx, datetime(2025, 6, 23, 12, 0), pike())
        self.assertLess(ev.confidence, 0.45)
        self.assertLess(abs(ev.score - 5.0), 2.0)                   # shrunk hard toward "average"
        self.assertTrue(ev.notes)
        unavailable = {f.name for f in ev.factors if f.source == "unavailable"}
        self.assertTrue({"water_temp", "pressure", "wind", "cloud"} <= unavailable)
        for f in ev.factors:
            if f.source == "unavailable":
                self.assertEqual((f.weight, f.contribution), (0.0, 0.0))
                self.assertIsNone(f.value)
        # astronomy-only factors still work with no weather at all
        self.assertIsNotNone(ev.factor("time_of_day"))
        self.assertNotEqual(ev.factor("time_of_day").source, "unavailable")

    def test_missing_factors_are_named_in_the_notes(self):
        ctx = spot_context()
        del ctx.series["wind_speed"]
        ctx.invalidate()
        ev = sc.evaluate(ctx, datetime(2025, 6, 23, 12, 0), pike())
        self.assertTrue(any("wind" in n.lower() for n in ev.notes))
        self.assertEqual(ev.factor("wind").source, "unavailable")
        self.assertLess(ev.coverage, 1.0)

    def test_less_data_never_means_more_confidence(self):
        full = spot_context()
        sparse = spot_context(with_clim=False)
        for name in ("wind_speed", "cloud", "uv_index", "precip"):
            del sparse.series[name]
        sparse.invalidate()
        when = datetime(2025, 6, 23, 12, 0)
        self.assertLess(sc.evaluate(sparse, when, pike()).confidence, sc.evaluate(full, when, pike()).confidence)

    def test_off_season_lowers_a_good_score_and_deepens_a_bad_one(self):
        ctx = spot_context()
        when = datetime(2025, 6, 23, 5, 0)
        prof = pike()
        in_season = sc.evaluate(ctx, when, prof.adjusted(season=(1.0,) * 12))
        off = sc.evaluate(ctx, when, prof.adjusted(season=(0.5,) * 12))
        self.assertGreater(in_season.raw, 0.0)
        self.assertLess(off.raw, in_season.raw)
        self.assertTrue(any("season" in n for n in off.notes))


class WaterSourceTest(unittest.TestCase):
    def test_a_probe_sets_the_level_but_not_a_fake_thermal_shock(self):
        ctx = spot_context(water_sensor=21.0)
        ev = sc.evaluate(ctx, datetime(2025, 6, 23, 12, 0), pike())
        self.assertEqual(ev.water.source, "sensor")
        self.assertAlmostEqual(ev.water.temp, 21.0, delta=0.05)
        self.assertLess(abs(ev.water.change_24h), 2.0)              # the model's shape, not sensor-minus-model
        self.assertIsNone(ev.water.shock)

    def test_the_probe_offset_carries_into_the_forecast(self):
        probe = spot_context(water_sensor=21.0)
        plain = spot_context()
        later = datetime(2025, 6, 24, 12, 0)
        now = datetime(2025, 6, 23, 12, 0)
        a = sc.prepare(probe, later, now=now)
        b = sc.prepare(plain, later, now=now)
        self.assertGreater(a.water_level, b.water_level + 2.0)

    def test_without_any_probe_the_model_is_used_and_labelled(self):
        ev = sc.evaluate(spot_context(), datetime(2025, 6, 23, 12, 0), pike())
        self.assertEqual(ev.water.source, "model")
        self.assertEqual(ev.factor("water_temp").source, "model")

    def test_a_measured_series_beats_the_model(self):
        ctx = spot_context()
        ctx.series["water_temp"] = [(datetime(2025, 6, 23, 0, 0) + timedelta(hours=h), 17.5) for h in range(0, 14)]
        ctx.invalidate()
        ev = sc.evaluate(ctx, datetime(2025, 6, 23, 12, 0), pike())
        self.assertEqual(ev.water.source, "sensor")
        self.assertAlmostEqual(ev.water.temp, 17.5, places=6)

    def test_water_model_is_built_once_per_refresh(self):
        ctx = spot_context()
        sc.evaluate(ctx, datetime(2025, 6, 23, 5, 0), pike())
        first = ctx.memo[sc._WATER_MODEL_KEY]
        sc.evaluate(ctx, datetime(2025, 6, 23, 9, 0), pike())
        self.assertIs(ctx.memo[sc._WATER_MODEL_KEY], first)
        ctx.invalidate()
        self.assertNotIn(sc._WATER_MODEL_KEY, ctx.memo)


class PreparedTest(unittest.TestCase):
    def test_reusing_a_prepared_instant_gives_the_same_answer(self):
        ctx = spot_context()
        when = datetime(2025, 6, 23, 20, 0)
        prepared = sc.prepare(ctx, when)
        for prof in (pike(), sp.profile("carp"), sp.profile("zander")):
            self.assertAlmostEqual(
                sc.evaluate(ctx, when, prof, prepared).score,
                sc.evaluate(ctx, when, prof).score,
                places=9,
            )

    def test_a_prepared_for_another_instant_is_not_silently_reused(self):
        ctx = spot_context()
        wrong = sc.prepare(ctx, datetime(2025, 6, 23, 5, 0))
        ev = sc.evaluate(ctx, datetime(2025, 6, 23, 13, 0), pike(), wrong)
        self.assertEqual(ev.when, datetime(2025, 6, 23, 13, 0))
        self.assertGreater(ev.sun_altitude, 30.0)


class SpanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = spot_context()
        cls.curve = sc.hourly_curve(cls.ctx, NOW, 48, pike())

    def test_spans_are_ordered_disjoint_and_above_the_threshold(self):
        cut = sorted(ev.score for _, ev in self.curve)[len(self.curve) // 2]
        spans = sc.spans_from_curve(self.curve, min_score=cut)
        self.assertTrue(spans)
        for a, b in zip(spans, spans[1:]):
            self.assertLess(a.end, b.start + timedelta(seconds=1))
        for s in spans:
            self.assertGreaterEqual(s.mean_score, cut)
            self.assertGreaterEqual(s.peak_score, s.mean_score)
            self.assertTrue(s.start <= s.peak_at < s.end)
            self.assertGreater(s.hours, 0.0)

    def test_thresholds_at_the_extremes(self):
        self.assertEqual(sc.spans_from_curve(self.curve, min_score=99.0), [])
        everything = sc.spans_from_curve(self.curve, min_score=0.0)
        self.assertEqual(len(everything), 1)
        self.assertEqual(everything[0].hours, 48.0)

    def test_best_spans_matches_the_curve_it_is_built_from(self):
        direct = sc.best_spans(self.ctx, NOW, 48, pike(), min_score=5.9)
        again = sc.spans_from_curve(self.curve, 5.9)
        self.assertEqual([(s.start, s.end) for s in direct], [(s.start, s.end) for s in again])


if __name__ == "__main__":
    unittest.main()
