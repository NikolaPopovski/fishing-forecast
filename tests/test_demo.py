"""Smoke test for scripts/demo.py: it must keep running offline and keep printing sane output."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "custom_components" / "fishing"))   # appended, never prepended: select.py must not shadow the stdlib
sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_demo():
    spec = importlib.util.spec_from_file_location("fishing_demo", ROOT / "scripts" / "demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DemoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.demo = load_demo()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cls.code = cls.demo.main(["--step", "6"])
        cls.out = buf.getvalue()

    def test_runs_cleanly_for_every_spot_kind(self):
        self.assertEqual(self.code, 0)
        for kind in ("LAKE", "RIVER", "COAST"):
            self.assertIn(f"=== {kind}", self.out)

    def test_output_is_plain_ascii_so_any_console_can_show_it(self):
        self.assertTrue(self.out.isascii())

    def test_table_has_eight_rows_per_spot_and_scores_in_range(self):
        scores = [float(m) for m in re.findall(r"^\w{3} \d\d:\d\d\s+-?\d+.*?\s(\d+\.\d)\s[a-z]{3}", self.out, re.M)]
        self.assertGreaterEqual(len(scores), 8 * 3)
        self.assertTrue(all(0.0 <= s <= 10.0 for s in scores))

    def test_the_front_is_visible_in_the_barometer_column(self):
        self.assertIn("-2.6", self.out)              # the fall ahead of the front
        self.assertIn("+2.0", self.out)              # the recovery behind it

    def test_advice_block_has_reasons_tactics_and_timing(self):
        self.assertIn("advice at", self.out)
        self.assertIn("Helps:", self.out)
        self.assertIn("when:", self.out)
        self.assertIn("conditions:", self.out)

    def test_species_and_kinds_can_be_chosen(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.demo.main(["--kinds", "lake", "--species", "perch", "--step", "12"])
        out = buf.getvalue()
        self.assertIn("=== LAKE", out)
        self.assertNotIn("=== RIVER", out)
        self.assertIn("European perch", out)


if __name__ == "__main__":
    unittest.main()
