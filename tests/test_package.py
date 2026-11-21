"""The installable package: what ships, what does not, and that integration and card agree on a version."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from glue import FISHING_DIR, ROOT  # noqa: E402

from fishing import const  # noqa: E402


def load_packager():
    spec = importlib.util.spec_from_file_location("fishing_package", ROOT / "scripts" / "package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PackageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        cls.pkg = load_packager()
        cls.zip_path = cls.pkg.build(cls.out)
        with zipfile.ZipFile(cls.zip_path) as z:
            cls.names = z.namelist()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_everything_unzips_into_one_fishing_folder(self):
        self.assertTrue(all(n.startswith("fishing/") for n in self.names))
        self.assertIn("fishing/manifest.json", self.names)
        self.assertIn("fishing/__init__.py", self.names)

    def test_the_card_ships_inside_the_integration_and_is_the_committed_copy(self):
        with zipfile.ZipFile(self.zip_path) as z:
            packed = z.read("fishing/www/fishing-card.js")
        self.assertEqual(packed, (FISHING_DIR / "www" / "fishing-card.js").read_bytes())
        self.assertEqual((self.out / "fishing-card.js").read_bytes(), packed)       # standalone copy for a manual install

    def test_the_model_ships_and_dev_files_do_not(self):
        self.assertTrue({"fishing/model/scoring.py", "fishing/model/advice.py", "fishing/api/open_meteo.py", "fishing/translations/en.json"} <= set(self.names))
        for name in self.names:
            self.assertNotIn("__pycache__", name)
            self.assertFalse(name.endswith(".pyc"), name)
            self.assertNotRegex(name, r"(^|/)(tests|scratch|scripts|\.claude)/")

    def test_every_source_file_is_included(self):
        expected = {f"fishing/{p.relative_to(FISHING_DIR).as_posix()}" for p in FISHING_DIR.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
        self.assertTrue(expected <= set(self.names), expected - set(self.names))

    def test_no_duplicate_entries(self):
        self.assertEqual(len(self.names), len(set(self.names)))


class VersionTest(unittest.TestCase):
    def test_manifest_const_and_card_agree(self):
        manifest = json.loads((FISHING_DIR / "manifest.json").read_text(encoding="utf-8"))
        card = (FISHING_DIR / "www" / "fishing-card.js").read_text(encoding="utf-8")
        in_card = re.search(r'const VERSION = "([^"]+)"', card).group(1)
        self.assertEqual(manifest["version"], const.VERSION)
        self.assertEqual(in_card, const.VERSION)


class HacsReadinessTest(unittest.TestCase):
    """What HACS installs is exactly the repository's `custom_components/fishing`, so it must be complete."""

    README = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_the_card_is_inside_the_integration_so_a_hacs_install_carries_it(self):
        self.assertTrue((FISHING_DIR / "www" / "fishing-card.js").is_file())
        self.assertFalse((ROOT / "www" / "fishing-card.js").exists(), "a second copy of the card at the repo root would drift")

    def test_hacs_json(self):
        hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
        self.assertEqual(hacs["name"], "Fishing Forecast")
        self.assertTrue(hacs.get("render_readme"))
        self.assertRegex(hacs["homeassistant"], r"^\d{4}\.\d+\.\d+$")

    def test_exactly_one_integration_in_the_repository(self):
        folders = [p.name for p in (ROOT / "custom_components").iterdir() if p.is_dir() and p.name != "__pycache__"]
        self.assertEqual(folders, ["fishing"])

    def test_the_readme_covers_every_species_entity_and_card_option(self):
        from fishing.model import species as sp

        for name in sp.species_names().values():
            first = name.split(" (")[0].split(" / ")[0]
            self.assertIn(first.lower(), self.README.lower(), name)
        for needed in ("Score", "Confidence", "Water temperature", "Pressure tendency", "Forecast peak", "Next best window", "Feeding time", "Stale data", "Target species", "fishing.refresh"):
            self.assertIn(needed, self.README)
        card = (FISHING_DIR / "www" / "fishing-card.js").read_text(encoding="utf-8")
        for option in re.findall(r"^ \*   (show_\w+|view|hours|name):", card, re.M):
            self.assertIn(option, self.README, option)
        for word in ("HACS", "custom:fishing-card", "Open-Meteo", "Limits"):
            self.assertIn(word, self.README)

    def test_the_workflows_run_the_suites(self):
        tests = (ROOT / ".github" / "workflows" / "tests.yaml").read_text(encoding="utf-8")
        self.assertIn("python -m unittest discover -s tests", tests)
        self.assertIn('node --test "tests/card/*.test.mjs"', tests)
        validate = (ROOT / ".github" / "workflows" / "validate.yaml").read_text(encoding="utf-8")
        self.assertIn("hacs/action", validate)
        self.assertIn("category: integration", validate)

    def test_manifest_has_what_hacs_and_hassfest_require(self):
        manifest = json.loads((FISHING_DIR / "manifest.json").read_text(encoding="utf-8"))
        for key in ("domain", "name", "version", "documentation", "issue_tracker", "codeowners"):
            self.assertTrue(manifest.get(key), key)
        self.assertTrue(manifest["documentation"].startswith("https://github.com/"))
        self.assertTrue(manifest["issue_tracker"].startswith(manifest["documentation"]))
        self.assertTrue(all(c.startswith("@") for c in manifest["codeowners"]))
        keys = list(manifest)
        self.assertEqual(keys[:2], ["domain", "name"])
        self.assertEqual(keys[2:], sorted(keys[2:]), "hassfest wants the other manifest keys alphabetical")

    def test_there_is_a_licence_and_the_readme_links_it(self):
        licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(licence.startswith("MIT License"))
        self.assertIn("Copyright (c)", licence)
        self.assertIn("(LICENSE)", self.README)

    def test_no_personal_details_in_any_tracked_file(self):
        skip_dirs = {"scratch", ".claude", "dist", ".git", "node_modules", "__pycache__"}
        suffixes = {".py", ".md", ".json", ".js", ".mjs", ".yaml", ".yml", ".txt", ".ini", ".toml"}
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in suffixes or skip_dirs & set(path.relative_to(ROOT).parts) or path == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertNotIn("gebruiker", text.lower(), "a local user name")
                self.assertNotRegex(text, r"[\w.+-]+@(gmail|googlemail|hotmail|outlook|yahoo)\.", "a personal email address")
                self.assertNotRegex(text, r"[A-Za-z]:[\\/]Users[\\/]", "a local machine path")

    def test_nothing_private_is_tracked_by_the_package_inputs(self):
        for path in FISHING_DIR.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix in (".py", ".json", ".js", ".yaml"):
                text = path.read_text(encoding="utf-8")
                self.assertNotRegex(text, r"[\w.+-]+@[\w-]+\.[\w.]+", f"an email address in {path.name}")
                self.assertNotIn("C:\\Users", text, path.name)


if __name__ == "__main__":
    unittest.main()
