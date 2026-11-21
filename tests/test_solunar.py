"""Offline tests for model.solunar: window geometry, blending, the polar lunar-clock fallback.

All datetimes are timezone-aware UTC, because the astronomy layer returns aware UTC.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "custom_components" / "fishing"))   # appended, never prepended: select.py must not shadow the stdlib

from model import astronomy as astro  # noqa: E402
from model import solunar as sol  # noqa: E402

UTC = timezone.utc
LAT, LON = 52.7, 5.3
DAY = datetime(2025, 6, 10, tzinfo=UTC)
POLAR = (78.2, 15.6)       # Longyearbyen


def window(peak, kind="major", centre="transit", strength=1.0):
    half = sol.MAJOR_HALF_H if kind == "major" else sol.MINOR_HALF_H
    return sol.SolunarWindow(peak - timedelta(hours=half), peak + timedelta(hours=half), peak, kind, centre, strength)


class WindowsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wins = sol.windows(DAY, LAT, LON)
        cls.events = astro.body_events(DAY, LAT, LON, moon=True)

    def test_four_windows_two_major_two_minor_in_time_order(self):
        self.assertEqual(sorted(w.kind for w in self.wins), ["major", "major", "minor", "minor"])
        self.assertEqual(sorted(w.centre for w in self.wins), ["anti_transit", "rise", "set", "transit"])
        self.assertEqual([w.start for w in self.wins], sorted(w.start for w in self.wins))

    def test_majors_bracket_transit_and_antitransit_minors_bracket_rise_and_set(self):
        by = {w.centre: w for w in self.wins}
        self.assertEqual(by["transit"].peak, self.events.transit)
        self.assertEqual(by["anti_transit"].peak, self.events.lower_transit)
        self.assertEqual(by["rise"].peak, self.events.rise)
        self.assertEqual(by["set"].peak, self.events.set)
        for w in self.wins:
            self.assertTrue(w.contains(w.peak))
            self.assertAlmostEqual((w.end - w.start).total_seconds() / 3600.0, 4.0 if w.kind == "major" else 2.0)
            self.assertEqual(w.notes, [])           # real events, nothing synthesised

    def test_labels_and_dict(self):
        labels = {w.centre: w.label for w in self.wins}
        self.assertEqual(labels["transit"], "moon overhead")
        self.assertEqual(labels["anti_transit"], "moon underfoot")
        self.assertEqual(labels["rise"], "moonrise")
        self.assertEqual(labels["set"], "moonset")
        import json

        json.dumps([w.as_dict() for w in self.wins])

    def test_strength_override_is_applied(self):
        self.assertTrue(all(w.strength == 0.4 for w in sol.windows(DAY, LAT, LON, strength=0.4)))

    def test_multi_day_windows_are_sorted_and_not_duplicated(self):
        wins = sol.multi_day_windows(DAY, LAT, LON, days=2)
        self.assertGreaterEqual(len(wins), 7)
        self.assertEqual([w.start for w in wins], sorted(w.start for w in wins))
        for i, a in enumerate(wins):
            for b in wins[i + 1:]:
                self.assertGreater(abs((a.peak - b.peak).total_seconds()), 900)
        self.assertTrue(any(w.peak - DAY > timedelta(hours=24) for w in wins))     # day two is really there


class PolarFallbackTest(unittest.TestCase):
    def test_no_rise_or_set_day_still_gets_four_windows_on_the_lunar_clock(self):
        # 78 N, June: the Moon stays below the horizon for days around this date
        day = datetime(2025, 6, 10, tzinfo=UTC)
        ev = astro.body_events(day, *POLAR, moon=True)
        self.assertTrue(ev.always_down)
        wins = sol.windows(day, *POLAR)
        self.assertEqual(sorted(w.centre for w in wins), ["anti_transit", "rise", "set", "transit"])
        minors = [w for w in wins if w.kind == "minor"]
        majors = [w for w in wins if w.kind == "major"]
        self.assertEqual(len(minors), 2)
        for m in minors:
            self.assertTrue(any("lunar clock" in n for n in m.notes))
            # a quarter lunar day (~6.2 h) from the nearest major - never stacked on one
            gap = min(abs((m.peak - j.peak).total_seconds()) / 3600.0 for j in majors)
            self.assertTrue(4.5 < gap < 8.0, gap)

    def test_the_clock_order_is_rise_then_transit_then_set(self):
        day = datetime(2025, 12, 17, tzinfo=UTC)
        by = {w.centre: w for w in sol.windows(day, *POLAR)}
        self.assertLess(by["rise"].peak, by["transit"].peak)
        self.assertLess(by["transit"].peak, by["set"].peak)

    def test_a_missing_single_event_only_fills_that_one(self):
        # 2025-12-10 at 78 N has real rise and set: nothing should be synthesised
        wins = sol.windows(datetime(2025, 12, 10, tzinfo=UTC), *POLAR)
        self.assertEqual(sorted(w.centre for w in wins), ["anti_transit", "rise", "set", "transit"])
        self.assertTrue(all(not w.notes for w in wins))


class IntensityTest(unittest.TestCase):
    peak = datetime(2025, 6, 10, 12, tzinfo=UTC)

    def at(self, hours, wins):
        return sol.intensity_at(self.peak + timedelta(hours=hours), wins)

    def test_peak_edge_tail_and_beyond_decay_without_snapping(self):
        wins = [window(self.peak)]
        peak, edge, tail, beyond = (self.at(h, wins)[0] for h in (0.0, 2.0, 3.5, 4.6))
        self.assertEqual(peak, 1.0)
        self.assertEqual(edge, 1.0)
        self.assertTrue(0.0 < tail < 1.0)
        self.assertEqual(beyond, 0.0)

    def test_only_inside_a_window_is_a_kind_and_label_reported(self):
        wins = [window(self.peak)]
        _, kind, label, peak = self.at(1.0, wins)
        self.assertEqual((kind, label, peak), ("major", "moon overhead", self.peak))
        self.assertEqual(self.at(3.0, wins)[1:], (None, None, None))      # in the tail, not inside

    def test_overlapping_windows_add_up_beyond_either_alone(self):
        a = window(self.peak, "major", "transit", strength=0.5)
        b = window(self.peak + timedelta(minutes=30), "minor", "rise", strength=0.5)
        self.assertEqual(self.at(0.0, [a])[0], 0.5)
        self.assertGreater(self.at(0.0, [a, b])[0], 0.5)
        self.assertLessEqual(self.at(0.0, [a, b])[0], 1.0)

    def test_a_major_beats_an_overlapping_minor_for_the_label(self):
        wins = [window(self.peak, "minor", "rise"), window(self.peak, "major", "transit")]
        self.assertEqual(self.at(0.0, wins)[1], "major")

    def test_weak_strength_lowers_intensity(self):
        self.assertLess(self.at(0.0, [window(self.peak, strength=0.3)])[0], self.at(0.0, [window(self.peak, strength=1.0)])[0])

    def test_no_windows(self):
        self.assertEqual(self.at(0.0, []), (0.0, None, None, None))


class AnalyseTest(unittest.TestCase):
    peak = datetime(2025, 6, 10, 12, tzinfo=UTC)

    def setUp(self):
        self.wins = [window(self.peak - timedelta(hours=12), "minor", "set"), window(self.peak), window(self.peak + timedelta(hours=9), "minor", "rise")]

    def test_inside_a_window(self):
        st = sol.analyse(self.peak, self.wins, strength=1.0)
        self.assertEqual((st.in_window, st.label, st.peak), ("major", "moon overhead", self.peak))
        self.assertEqual(st.next_window.centre, "rise")
        self.assertAlmostEqual(st.hours_to_next, 9.0 - sol.MINOR_HALF_H)
        self.assertEqual(st.prev_window.centre, "set")

    def test_score_is_higher_in_a_window_than_between_windows(self):
        inside = sol.analyse(self.peak, self.wins, strength=1.0)
        between = sol.analyse(self.peak + timedelta(hours=5), self.wins, strength=1.0)
        self.assertGreater(inside.score, between.score)
        self.assertGreater(inside.score, 0.5)
        self.assertLess(between.score, 0.0)

    def test_a_quarter_moon_day_flattens_the_contrast(self):
        weak = sol.analyse(self.peak, [window(self.peak, strength=0.3)], strength=0.3)
        strong = sol.analyse(self.peak, [window(self.peak, strength=1.0)], strength=1.0)
        self.assertLess(weak.score, strong.score)

    def test_nothing_ahead_or_behind(self):
        st = sol.analyse(self.peak + timedelta(days=3), self.wins, strength=1.0)
        self.assertIsNone(st.next_window)
        self.assertIsNone(st.hours_to_next)
        empty = sol.analyse(self.peak, [], strength=1.0)
        self.assertIsNone(empty.next_window)
        self.assertIsNone(empty.prev_window)

    def test_as_dict_is_json_friendly(self):
        import json

        json.dumps(sol.analyse(self.peak, self.wins, strength=1.0).as_dict())


class StrengthAndTideTest(unittest.TestCase):
    def test_day_strength_peaks_near_new_and_full_moon(self):
        full = astro.find_phase_crossing("full", datetime(2025, 6, 1, tzinfo=UTC))
        self.assertIsNotNone(full)
        quarter = full + timedelta(days=7.4)
        self.assertGreater(sol.day_strength(full), sol.day_strength(quarter))
        for when in (full, quarter):
            self.assertTrue(0.0 <= sol.day_strength(when) <= 1.35)

    def test_tide_bonus_is_zero_when_the_water_is_not_tidal(self):
        self.assertEqual(sol.tide_bonus(None, None), 0.0)
        self.assertEqual(sol.tide_bonus(None, 1.4), 0.0)
        self.assertEqual(sol.tide_bonus(2.0, None), 0.0)
        self.assertEqual(sol.tide_bonus(0.0, 0.8), 0.0)

    def test_tide_bonus_grows_with_range_and_springs_and_is_capped(self):
        self.assertGreater(sol.tide_bonus(2.0, 1.0), sol.tide_bonus(1.0, 1.0))
        self.assertGreater(sol.tide_bonus(2.0, 1.4), sol.tide_bonus(2.0, 1.0))
        self.assertEqual(sol.tide_bonus(10.0, 2.0), 0.35)


if __name__ == "__main__":
    unittest.main()
