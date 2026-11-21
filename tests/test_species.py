"""Offline tests for the species registry: structural invariants, not tuning opinions."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "custom_components" / "fishing"))   # appended, never prepended: select.py must not shadow the stdlib

from model import species as sp  # noqa: E402

LIGHT_WINDOWS = {"diurnal", "crepuscular", "nocturnal", "opportunistic"}


class RegistryTest(unittest.TestCase):
    def test_there_are_22_unique_profiles(self):
        ids = sp.species_ids()
        self.assertEqual(len(ids), 22)
        self.assertEqual(len(set(ids)), 22)
        self.assertEqual(set(ids), set(sp.PROFILES))
        self.assertEqual(set(ids), set(sp.MOONLIGHT_EFFECT))

    def test_every_profile_is_structurally_valid(self):
        for p in sp.PROFILE_LIST:
            with self.subTest(species=p.id):
                self.assertTrue(set(p.weights) <= set(sp.FACTORS), "profile uses a factor scoring does not know")
                self.assertTrue(all(v > 0 for v in p.weights.values()))
                self.assertEqual(len(p.season), 12)
                self.assertTrue(all(0.0 < m <= 1.6 for m in p.season))
                self.assertIn(p.light_window, LIGHT_WINDOWS)
                self.assertGreater(p.water_temp_width_c, 0)
                self.assertTrue(-5 <= p.water_temp_optimum_c <= 35)
                self.assertTrue(-1.0 <= p.moonlight_effect <= 1.0)
                self.assertTrue(0.0 <= p.turbidity_pref <= 1.0)
                self.assertGreater(p.pressure_sensitivity, 0)

    def test_weight_map_is_normalised(self):
        for p in sp.PROFILE_LIST:
            with self.subTest(species=p.id):
                self.assertAlmostEqual(sum(p.weight_map().values()), 1.0, places=9)

    def test_generic_weights_only_use_known_factors(self):
        self.assertEqual(set(sp.GENERIC_WEIGHTS), set(sp.FACTORS))

    def test_moonlight_table_is_applied_to_profiles(self):
        for p in sp.PROFILE_LIST:
            with self.subTest(species=p.id):
                self.assertEqual(p.moonlight_effect, sp.MOONLIGHT_EFFECT[p.id])


class LookupTest(unittest.TestCase):
    def test_unknown_or_missing_falls_back_to_mixed(self):
        self.assertEqual(sp.profile("nope").id, "mixed_general")
        self.assertEqual(sp.profile(None).id, "mixed_general")
        self.assertEqual(sp.profile("").id, "mixed_general")

    def test_lookup_is_tolerant_of_case_and_spaces(self):
        self.assertEqual(sp.profile(" Pike ").id, "pike")
        self.assertEqual(sp.profile("Wels Catfish").id, "wels_catfish")

    def test_every_listed_id_resolves_to_itself(self):
        # regression: ids with capitals (European_bass) must survive the lowercase normalisation,
        # otherwise a user who picks sea bass silently gets the mixed profile.
        for sid in sp.species_ids():
            with self.subTest(species=sid):
                self.assertEqual(sp.profile(sid).id, sid)

    def test_names_match_ids(self):
        names = sp.species_names()
        self.assertEqual(set(names), set(sp.species_ids()))
        self.assertTrue(all(names.values()))


class ProfileBehaviourTest(unittest.TestCase):
    def test_moonlight_sign_divides_night_hunters(self):
        self.assertLess(sp.profile("wels_catfish").moonlight_effect, 0)
        self.assertLess(sp.profile("zander").moonlight_effect, 0)
        self.assertGreater(sp.profile("flounder_plaice").moonlight_effect, 0)
        self.assertGreater(sp.profile("pike").moonlight_effect, 0)

    def test_season_factor_is_by_month(self):
        p = sp.profile("pike")
        for month in range(1, 13):
            self.assertEqual(p.season_factor(date(2026, month, 15)), p.season[month - 1])

    def test_adjusted_is_a_non_mutating_copy(self):
        p = sp.profile("carp")
        q = p.adjusted(water_temp_optimum_c=p.water_temp_optimum_c + 2.0)
        self.assertEqual(q.water_temp_optimum_c, p.water_temp_optimum_c + 2.0)
        self.assertNotEqual(p.water_temp_optimum_c, q.water_temp_optimum_c)
        self.assertEqual(q.id, p.id)

    def test_tactic_lookup_with_default(self):
        p = sp.profile("pike")
        self.assertEqual(p.tactic_for("definitely_not_a_state", "fallback"), "fallback")
        for state, text in p.tactics.items():
            self.assertEqual(p.tactic_for(state), text)
            self.assertTrue(text)


if __name__ == "__main__":
    unittest.main()
