"""Offline tests for model.advice: every condition rule, the text constraints, the timing advice."""
from __future__ import annotations

import dataclasses
import json
import re
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "custom_components" / "fishing"))   # appended, never prepended: select.py must not shadow the stdlib
sys.path.insert(0, str(Path(__file__).resolve().parent))

from model import advice as ad  # noqa: E402
from model import scoring as sc  # noqa: E402
from model import species as sp  # noqa: E402
from model.context import ShelterGeometry, SpotContext  # noqa: E402
from support import SPOT_START, spot_context  # noqa: E402

NOW = SPOT_START + timedelta(days=22)
S = ad.Situation


def neutral() -> ad.Situation:
    """A situation in which no named condition holds: each case below perturbs it minimally."""
    return S(
        sun_alt=10.0, moonshine=0.0, cloud=50.0, wind=3.0, wind_dir=90.0, wind_max_24h=5.0,
        humidity=50.0, cape=0.0, air_temp=12.0, water_offset=0.0, water_change_24h=0.0,
        water_change_7d=0.0, tend3=0.0, tend24=0.0, stability24=5.0, front_index=0.1,
        front_eta=None, temp_jump=0.0, turbidity=0.25, discharge_z=None, level_trend_z=None,
        plume=0.0, rain_24=0.0, rain_72=0.0,
    )


#: key -> (overrides that make it hold, overrides that are a near miss and must not trigger it)
CASES: dict[str, tuple[dict, dict]] = {
    "front": (dict(front_index=0.6, front_eta=4.0), dict(front_index=0.6, front_eta=10.0)),
    "post_front": (dict(tend3=1.0, stability24=6.0, wind=3.0), dict(tend3=1.0, stability24=6.0, wind=8.0)),
    "falling_barometer": (dict(tend3=-2.0, tend24=-6.0), dict(tend3=-2.0, tend24=-1.0)),
    "winter_storm_edge": (dict(front_index=0.5, tend3=-2.0, air_temp=5.0, wind=9.0), dict(front_index=0.5, tend3=-2.0, air_temp=15.0, wind=9.0)),
    "night_flood": (dict(sun_alt=-20.0, tidal=True, tide_rising=True), dict(sun_alt=-20.0, tidal=True, tide_rising=False)),
    "falling_into_flood": (dict(tidal=True, tide_rising=True, hours_to_slack=1.0), dict(tidal=True, tide_rising=True, hours_to_slack=4.0)),
    "gale_aftermath": (dict(wind_max_24h=14.0, wind=4.0, turbidity=0.4), dict(wind_max_24h=8.0, wind=4.0, turbidity=0.4)),
    "pressure_falling": (dict(tend3=-1.5), dict(tend3=-0.5)),
    "rising_falling": (dict(tend3=-1.0, sun_alt=30.0, air_temp=24.0), dict(tend3=-1.0, sun_alt=-20.0, air_temp=24.0)),
    "cold_front": (dict(temp_jump=-3.0, tend3=0.2), dict(temp_jump=-3.0, tend3=-1.0)),
    "cold_snap": (dict(water_change_7d=-3.0), dict(water_change_7d=-1.0)),
    "warm_spike": (dict(water_change_24h=3.0), dict(water_change_24h=1.0)),
    "very_high": (dict(running_water=True, discharge_z=2.2), dict(running_water=False, discharge_z=2.2)),
    "high_coloured": (dict(running_water=True, discharge_z=1.3, turbidity=0.5), dict(running_water=True, discharge_z=1.3, turbidity=0.2)),
    "rising_warm": (dict(running_water=True, level_trend_z=0.6, water_offset=1.0), dict(running_water=True, level_trend_z=0.6, water_offset=-2.0)),
    "slight_rise_clean": (dict(level_trend_z=0.8, turbidity=0.1, tend3=-0.6), dict(level_trend_z=0.8, turbidity=0.1, tend3=0.2)),
    "colour_rising": (dict(level_trend_z=0.6, turbidity=0.3), dict(level_trend_z=0.6, turbidity=0.7)),
    "fast_clean": (dict(running_water=True, turbidity=0.1, discharge_z=0.5), dict(running_water=True, turbidity=0.1, discharge_z=1.4)),
    "low_clear_warm": (dict(running_water=True, discharge_z=-1.2, turbidity=0.1, water_offset=2.0), dict(running_water=True, discharge_z=-1.2, turbidity=0.1, water_offset=0.0)),
    "warm_dry": (dict(running_water=True, discharge_z=-0.8, water_offset=3.0, turbidity=0.1), dict(running_water=False, discharge_z=-0.8, water_offset=3.0, turbidity=0.1)),
    "clear_cool_river": (dict(running_water=True, turbidity=0.1, water_offset=-2.0), dict(running_water=True, turbidity=0.1, water_offset=1.0)),
    "fresh_runoff": (dict(coastal=True, plume=0.5), dict(coastal=False, plume=0.5)),
    "turbid": (dict(turbidity=0.6), dict(turbidity=0.5)),
    "coloured": (dict(turbidity=0.5), dict(turbidity=0.8)),
    "clear_cold": (dict(turbidity=0.1, water_offset=-6.0), dict(turbidity=0.1, water_offset=-2.0)),
    "clear_spring_tide": (dict(tidal=True, tide_range=0.9, turbidity=0.2), dict(tidal=True, tide_range=0.3, turbidity=0.2)),
    "warm_night": (dict(sun_alt=-20.0, air_temp=19.0), dict(sun_alt=-20.0, air_temp=10.0)),
    "cloudy_night": (dict(sun_alt=-20.0, cloud=90.0, air_temp=14.0), dict(sun_alt=-20.0, cloud=40.0, air_temp=14.0)),
    "warm_stable": (dict(tend3=0.1, stability24=1.0, water_offset=1.0), dict(tend3=0.1, stability24=1.0, water_offset=-3.0)),
    "stable_warm": (dict(tend3=0.1, stability24=1.0, water_offset=-1.0), dict(tend3=0.1, stability24=6.0, water_offset=-1.0)),
    "warm": (dict(water_offset=4.0), dict(water_offset=2.0)),
    "cold": (dict(water_offset=-6.0), dict(water_offset=-3.0)),
    "moonlit": (dict(moonshine=0.8, sun_alt=-10.0), dict(moonshine=0.2, sun_alt=-10.0)),
    "bright_clear_warm": (dict(sun_alt=40.0, cloud=10.0, turbidity=0.1, water_offset=3.0), dict(sun_alt=40.0, cloud=80.0, turbidity=0.1, water_offset=3.0)),
    "warm_clear_bright": (dict(sun_alt=40.0, cloud=10.0, turbidity=0.1, water_offset=1.0), dict(sun_alt=40.0, cloud=10.0, turbidity=0.5, water_offset=1.0)),
    "bright_warm": (dict(sun_alt=40.0, cloud=10.0, water_offset=3.0), dict(sun_alt=40.0, cloud=10.0, water_offset=0.0)),
    "bright_clear": (dict(sun_alt=40.0, cloud=10.0, turbidity=0.1), dict(sun_alt=40.0, cloud=10.0, turbidity=0.5)),
    "bright_choppy": (dict(sun_alt=40.0, cloud=10.0, wind=6.0), dict(sun_alt=40.0, cloud=10.0, wind=2.0)),
    "bright_calm": (dict(sun_alt=10.0, cloud=20.0, wind=1.0), dict(sun_alt=10.0, cloud=20.0, wind=3.0)),
    "bright": (dict(sun_alt=40.0, cloud=10.0), dict(sun_alt=40.0, cloud=50.0)),
    "muggy_overcast": (dict(cloud=90.0, humidity=80.0, wind=2.0, air_temp=20.0, cape=500.0), dict(cloud=90.0, humidity=40.0, wind=2.0, air_temp=20.0, cape=500.0)),
    "overcast": (dict(sun_alt=20.0, cloud=90.0, air_temp=12.0), dict(sun_alt=-20.0, cloud=90.0, air_temp=12.0)),
    "strong_breeze_southwesterly": (dict(wind=11.0, wind_dir=225.0), dict(wind=11.0, wind_dir=45.0)),
    "onshore_breeze": (dict(onshore=True, wind=5.0), dict(onshore=False, wind=5.0)),
    "chop_on_the_bank": (dict(coastal=True, wind=6.0), dict(coastal=False, wind=6.0)),
    "choppy_wind": (dict(wind=5.0), dict(wind=3.0)),
    "windy": (dict(wind=7.0), dict(wind=5.0)),
    "flat_calm_clear": (dict(wind=1.0, turbidity=0.1), dict(wind=3.0, turbidity=0.1)),
}


class VocabularyTest(unittest.TestCase):
    def test_rules_cover_exactly_the_keys_the_species_profiles_use(self):
        used = {k for p in sp.PROFILE_LIST for k in p.tactics}
        self.assertEqual(set(ad.RULES) | {"default"}, used)

    def test_every_rule_has_a_case_here(self):
        self.assertEqual(set(CASES), set(ad.RULES))

    def test_rules_are_documented_and_prioritised(self):
        for key, rule in ad.RULES.items():
            self.assertEqual(rule.key, key)
            self.assertTrue(rule.meaning)
            self.assertGreater(rule.priority, 0)

    def test_priorities_put_timing_before_colour_before_wind(self):
        p = {k: r.priority for k, r in ad.RULES.items()}
        self.assertGreater(p["front"], p["coloured"])
        self.assertGreater(p["coloured"], p["windy"])
        self.assertGreater(p["night_flood"], p["cold"])


class RuleTest(unittest.TestCase):
    def test_the_neutral_situation_triggers_nothing(self):
        self.assertEqual(ad.matching_keys(neutral()), set())

    def test_a_situation_with_no_data_triggers_nothing(self):
        self.assertEqual(ad.matching_keys(S()), set())

    def test_each_rule_fires_on_its_case_and_not_on_its_near_miss(self):
        for key, (yes, no) in CASES.items():
            with self.subTest(rule=key):
                self.assertIn(key, ad.matching_keys(dataclasses.replace(neutral(), **yes)))
                self.assertNotIn(key, ad.matching_keys(dataclasses.replace(neutral(), **no)))

    def test_a_missing_number_never_satisfies_a_rule(self):
        # blank out each field the positive case set, one at a time: the rule must stop matching
        for key, (yes, _) in CASES.items():
            base = dataclasses.replace(neutral(), **yes)
            for field_name, value in yes.items():
                if value is None or isinstance(value, bool) and field_name in ("running_water", "coastal", "tidal"):
                    continue
                if (key, field_name) in {("warm_dry", "discharge_z"), ("muggy_overcast", "cape")}:
                    continue          # documented fallbacks: no gauge -> judge dryness from rain; CAPE is optional corroboration
                with self.subTest(rule=key, missing=field_name):
                    blanked = dataclasses.replace(base, **{field_name: None})
                    self.assertNotIn(key, ad.matching_keys(blanked))


class SituationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = spot_context()
        cls.ev = sc.evaluate(cls.ctx, datetime(2025, 6, 23, 5, 0), sp.profile("pike"))

    def test_numbers_come_from_the_evaluation(self):
        s = ad.situation(self.ctx, self.ev)
        self.assertEqual(s.wind, 3.5)
        self.assertEqual(s.cloud, 50.0)
        self.assertAlmostEqual(s.sun_alt, self.ev.sun_altitude)
        self.assertAlmostEqual(s.water_offset, self.ev.water.species_offset_c)
        self.assertFalse(s.running_water or s.coastal or s.tidal)
        self.assertIsNone(s.tide_rising)
        self.assertIsNone(s.onshore)                       # no bank aspect configured

    def test_kinds_set_the_water_flags(self):
        for kind, running, coastal in (("river", True, False), ("stream", True, False), ("coast", False, True), ("estuary", False, True), ("lake", False, False)):
            ctx = spot_context(kind=kind)
            ev = sc.evaluate(ctx, datetime(2025, 6, 23, 12, 0), sp.profile("pike"))
            s = ad.situation(ctx, ev)
            self.assertEqual((s.running_water, s.coastal), (running, coastal), kind)

    def test_tide_turning_is_found_on_tidal_water(self):
        ctx = spot_context(kind="coast", tidal=True, tide_range_m=2.5, lon=4.4)
        seen = set()
        for hour in range(24):
            ev = sc.evaluate(ctx, datetime(2025, 6, 23, hour, 0), sp.profile("European_bass"))
            s = ad.situation(ctx, ev)
            self.assertIsInstance(s.tide_rising, bool)
            self.assertTrue(s.hours_to_slack is None or 0.0 < s.hours_to_slack <= 8.0)
            self.assertTrue(0.0 <= s.tide_range <= 1.0)
            seen.add(s.tide_rising)
        self.assertEqual(seen, {True, False})              # the tide both rises and falls in a day

    def test_onshore_needs_a_known_bank_aspect(self):
        ctx = spot_context(wind_dir=200.0)
        ctx.shelter = ShelterGeometry(bank_aspect_deg=190.0)
        ev = sc.evaluate(ctx, datetime(2025, 6, 23, 12, 0), sp.profile("pike"))
        self.assertTrue(ad.situation(ctx, ev).onshore)
        ctx.shelter = ShelterGeometry(bank_aspect_deg=20.0)
        self.assertFalse(ad.situation(ctx, ev).onshore)
        self.assertIsNone(ad._onshore(ctx, None))

    def test_a_recent_gale_is_remembered(self):
        ctx = spot_context()
        ctx.series["wind_speed"] = [(t, 14.0 if t >= NOW - timedelta(hours=10) and t < NOW - timedelta(hours=6) else 3.0) for t, _ in ctx.series["wind_speed"]]
        ctx.invalidate()
        ev = sc.evaluate(ctx, NOW, sp.profile("pike"))
        self.assertEqual(ad.situation(ctx, ev).wind_max_24h, 14.0)

    def test_real_weather_triggers_the_matching_conditions(self):
        i0 = 24 * 22 + 12
        falling = spot_context(pressure=lambda i, t: 1025.0 - (2.0 / 3.0) * min(3, max(0, i - (i0 - 3))))
        ev = sc.evaluate(falling, NOW + timedelta(hours=12), sp.profile("pike"))
        self.assertIn("pressure_falling", ad.state_keys(falling, ev))

        stained = spot_context()
        stained.series["precip"] = [(t, 30.0 if t == NOW - timedelta(hours=6) else 0.0) for t, _ in stained.series["precip"]]
        stained.invalidate()
        ev = sc.evaluate(stained, NOW, sp.profile("pike"))
        self.assertTrue({"coloured", "turbid"} & ad.state_keys(stained, ev))


class TextHelpersTest(unittest.TestCase):
    def test_clip_limits_length_at_a_word_and_stays_ascii(self):
        long = "word " * 60
        out = ad.clip(long)
        self.assertLessEqual(len(out), ad.MAX_LEN)
        self.assertTrue(out.endswith("..."))
        self.assertNotIn("  ", out)
        self.assertEqual(ad.clip("short and sweet"), "short and sweet")
        self.assertEqual(ad.clip("a   b\n c"), "a b c")
        self.assertEqual(ad.clip("café – ok"), "caf? ? ok")
        self.assertLessEqual(len(ad.clip("x" * 500)), ad.MAX_LEN)


class ReasonsAndTacticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = spot_context()
        cls.prof = sp.profile("pike")
        cls.ev = sc.evaluate(cls.ctx, datetime(2025, 6, 23, 5, 0), cls.prof)

    def test_reasons_are_the_top_helping_and_hurting_factors(self):
        out = ad.reasons(self.ev)
        helping = sorted((f for f in self.ev.factors if f.contribution > 0.01), key=lambda f: -f.contribution)[:2]
        hurting = sorted((f for f in self.ev.factors if f.contribution < -0.01), key=lambda f: f.contribution)[:2]
        self.assertEqual(out, [ad.clip(f"Helps: {f.why}") for f in helping] + [ad.clip(f"Hurts: {f.why}") for f in hurting])
        self.assertTrue(out[0].startswith("Helps:"))

    def test_reasons_are_never_empty_even_with_no_data(self):
        bare = SpotContext(name="bare", lat=52.7, lon=5.3, tz_offset_min=120)
        ev = sc.evaluate(bare, datetime(2025, 6, 23, 12, 0), self.prof)
        self.assertTrue(ad.reasons(ev))
        empty = dataclasses.replace(self.ev, factors=[])
        self.assertEqual(ad.reasons(empty), ["Not enough data to say what is driving the score."])
        flat = dataclasses.replace(self.ev, factors=[dataclasses.replace(f, contribution=0.0) for f in self.ev.factors])
        self.assertIn("average", ad.reasons(flat)[0])

    def test_tactics_use_the_profiles_own_words_most_specific_first(self):
        prof = sp.profile("pike")
        with mock.patch.object(ad, "state_keys", return_value={"cold", "coloured", "front"}):
            out = ad.tactics(self.ctx, self.ev, prof)
        self.assertEqual(out, [ad.clip(prof.tactics["front"]), ad.clip(prof.tactics["coloured"])])
        with mock.patch.object(ad, "state_keys", return_value={"cold", "coloured", "front"}):
            self.assertEqual(ad.tactics(self.ctx, self.ev, prof, limit=1), [ad.clip(prof.tactics["front"])])

    def test_conditions_the_species_has_no_advice_for_are_ignored(self):
        prof = sp.profile("pike")
        with mock.patch.object(ad, "state_keys", return_value={"night_flood", "gale_aftermath"}):
            out = ad.tactics(self.ctx, self.ev, prof)
        self.assertEqual(len(out), 1)
        self.assertNotIn(out[0], prof.tactics.values())      # fell back to the generic line

    def test_fallback_is_the_profiles_default_when_it_has_one(self):
        prof = sp.profile("mixed_general")
        with mock.patch.object(ad, "state_keys", return_value=set()):
            self.assertEqual(ad.tactics(self.ctx, self.ev, prof), [ad.clip(prof.tactics["default"])])

    def test_every_tactic_text_in_the_registry_fits(self):
        for p in sp.PROFILE_LIST:
            for key, text in p.tactics.items():
                with self.subTest(species=p.id, key=key):
                    self.assertEqual(ad.clip(text), " ".join(text.split()))      # nothing is ever truncated


class HeadlineAndWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = spot_context()
        cls.prof = sp.profile("pike")

    def test_headline_names_band_species_and_score(self):
        ev = sc.evaluate(self.ctx, datetime(2025, 6, 23, 5, 0), self.prof)
        text = ad.headline(ev, self.prof)
        self.assertTrue(text.startswith(ev.band.capitalize()))
        self.assertIn(self.prof.name, text)
        self.assertIn(f"{ev.score:.1f}/10", text)
        self.assertNotIn("low confidence", text)

    def test_headline_admits_low_confidence(self):
        bare = SpotContext(name="bare", lat=52.7, lon=5.3, tz_offset_min=120)
        ev = sc.evaluate(bare, datetime(2025, 6, 23, 12, 0), self.prof)
        self.assertIn("low confidence", ad.headline(ev, self.prof))

    def test_headline_mentions_a_major_solunar_window_when_inside_one(self):
        probe = sc.evaluate(self.ctx, datetime(2025, 6, 23, 12, 0), self.prof)
        major = next(w for w in probe.solunar.windows if w.kind == "major" and sc.local_event(self.ctx, w.peak).date() == NOW.date())
        peak = sc.local_event(self.ctx, major.peak)
        ev = sc.evaluate(self.ctx, peak, self.prof)
        self.assertEqual(ev.solunar.in_window, "major")
        self.assertIn("major solunar window", ad.headline(ev, self.prof))

    def test_window_advice_names_the_next_major_window_in_local_time(self):
        ev = sc.evaluate(self.ctx, datetime(2025, 6, 23, 5, 0), self.prof)
        nxt = min((w for w in ev.solunar.windows if w.kind == "major" and sc.local_event(self.ctx, w.end) > ev.when), key=lambda w: w.start)
        a, b = sc.local_event(self.ctx, nxt.start), sc.local_event(self.ctx, nxt.end)
        text = ad.window_advice(self.ctx, ev)
        self.assertIn(f"Next major solunar window {a:%H:%M}-{b:%H:%M}", text)

    def test_window_advice_while_inside_a_window_and_a_golden_hour(self):
        probe = sc.evaluate(self.ctx, datetime(2025, 6, 23, 12, 0), self.prof)
        major = next(w for w in probe.solunar.windows if w.kind == "major" and sc.local_event(self.ctx, w.peak).date() == NOW.date())
        ev = sc.evaluate(self.ctx, sc.local_event(self.ctx, major.peak), self.prof)
        self.assertIn("now until", ad.window_advice(self.ctx, ev))

        start, end, _ = self.ctx.astronomy_for(NOW).golden[0]
        mid = sc.local_event(self.ctx, start) + (sc.local_event(self.ctx, end) - sc.local_event(self.ctx, start)) / 2
        ev = sc.evaluate(self.ctx, mid, self.prof)
        self.assertIn(f"Golden hour ends {sc.local_event(self.ctx, end):%H:%M}", ad.window_advice(self.ctx, ev))

    def test_window_advice_is_never_empty_even_at_the_pole(self):
        polar = spot_context(lat=78.2, lon=15.6, utc_offset_min=60)
        for hour in (0, 6, 12, 18):
            ev = sc.evaluate(polar, datetime(2025, 6, 23, hour, 0), self.prof)
            self.assertTrue(ad.window_advice(polar, ev))


class OutputContractTest(unittest.TestCase):
    """The card renders these strings verbatim: length, charset and precision are a contract."""

    @staticmethod
    def contexts():
        i0 = 24 * 22 + 12
        yield "baseline", spot_context(), datetime(2025, 6, 23, 5, 0)
        yield "falling", spot_context(pressure=lambda i, t: 1025.0 - (2.5 / 3.0) * min(3, max(0, i - (i0 - 3)))), datetime(2025, 6, 23, 12, 0)
        yield "rainy river", spot_context(kind="river", body="river", precip=2.0, discharge=lambda d: 100.0 * (1.0 + 0.4 * (d > 20))), datetime(2025, 6, 23, 18, 0)
        yield "windy night", spot_context(wind=11.0, wind_dir=225.0), datetime(2025, 6, 23, 1, 0)
        yield "coast", spot_context(kind="coast", tidal=True, tide_range_m=2.5, lon=4.4), datetime(2025, 6, 23, 22, 0)
        yield "polar", spot_context(lat=78.2, lon=15.6, utc_offset_min=60), datetime(2025, 6, 23, 0, 0)
        yield "bare", SpotContext(name="bare", lat=52.7, lon=5.3, tz_offset_min=120), datetime(2025, 6, 23, 12, 0)

    def test_every_string_for_every_species_fits_the_contract(self):
        too_precise = re.compile(r"\d+\.\d{2,}")
        checked = 0
        for label, ctx, when in self.contexts():
            prepared = sc.prepare(ctx, when)
            for prof in sp.PROFILE_LIST:
                ev = sc.evaluate(ctx, when, prof, prepared)
                out = ad.as_dict(ctx, ev, prof)
                strings = [out["headline"], out["window"], *out["reasons"], *out["tactics"], *out["notes"]]
                with self.subTest(context=label, species=prof.id):
                    self.assertTrue(out["reasons"])
                    self.assertTrue(out["tactics"])
                    self.assertTrue(out["headline"])
                    self.assertTrue(out["window"])
                    for text in strings:
                        self.assertTrue(text)
                        self.assertLessEqual(len(text), ad.MAX_LEN, text)
                        self.assertTrue(text.isascii(), text)
                        self.assertIsNone(too_precise.search(text), text)
                    self.assertTrue(set(out["conditions"]) <= set(ad.RULES))
                checked += 1
        self.assertEqual(checked, 7 * 22)

    def test_as_dict_is_json_friendly_and_complete(self):
        ctx = spot_context()
        prof = sp.profile("zander")
        out = ad.as_dict(ctx, sc.evaluate(ctx, datetime(2025, 6, 23, 1, 0), prof), prof)
        json.dumps(out)
        self.assertEqual(set(out), {"headline", "reasons", "tactics", "window", "conditions", "notes"})
        priorities = [ad.RULES[k].priority for k in out["conditions"]]
        self.assertEqual(priorities, sorted(priorities, reverse=True))


if __name__ == "__main__":
    unittest.main()
