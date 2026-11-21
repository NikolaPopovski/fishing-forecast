"""Static checks on the Home Assistant half of the integration, which cannot be imported offline.

These cannot prove the glue works (only a real Home Assistant can) but they catch the mistakes that
would otherwise surface as a broken setup form or a nameless entity: a typo in a translation key,
a selector option with no label, a module that does not even compile, a pure module that quietly
started importing Home Assistant.
"""
from __future__ import annotations

import ast
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from glue import FISHING_DIR  # noqa: E402

from fishing import const  # noqa: E402
from fishing.model import hydro  # noqa: E402
from fishing.model import species as sp  # noqa: E402
from fishing.model.context import SECTOR_ORDER  # noqa: E402
from fishing.model.water_temp import BODY_CLASSES  # noqa: E402

PURE_MODULES = ("const.py", "refresh.py", "spot.py", "snapshot.py", "api/__init__.py", "api/open_meteo.py")
HA_MODULES = ("__init__.py", "card.py", "coordinator.py", "config_flow.py", "entity.py", "sensor.py", "binary_sensor.py", "select.py", "diagnostics.py", "transport.py")
FORBIDDEN_IN_PURE = ("homeassistant", "aiohttp", "voluptuous")


def source(name: str) -> str:
    return (FISHING_DIR / name).read_text(encoding="utf-8")


def imports_of(name: str) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(source(name))):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return out


STRINGS = json.loads(source("strings.json"))


class CompileTest(unittest.TestCase):
    def test_every_module_is_valid_python(self):
        for name in PURE_MODULES + HA_MODULES:
            with self.subTest(module=name):
                compile(source(name), name, "exec")

    def test_json_and_manifest(self):
        manifest = json.loads(source("manifest.json"))
        self.assertEqual(manifest["domain"], const.DOMAIN)
        self.assertTrue(manifest["config_flow"])
        self.assertEqual(manifest["requirements"], [])           # Home Assistant already ships everything used
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(manifest["iot_class"], "cloud_polling")

    def test_english_translation_is_the_source_strings(self):
        self.assertEqual(json.loads(source("translations/en.json")), STRINGS)


class PurityTest(unittest.TestCase):
    def test_pure_modules_never_import_home_assistant(self):
        for name in PURE_MODULES:
            with self.subTest(module=name):
                self.assertFalse(imports_of(name) & set(FORBIDDEN_IN_PURE), imports_of(name))

    def test_the_model_stays_stdlib_only(self):
        allowed = set(sys.stdlib_module_names) | {"__future__"}
        for path in sorted((FISHING_DIR / "model").glob("*.py")):
            with self.subTest(module=path.name):
                self.assertTrue(imports_of(f"model/{path.name}") <= allowed, imports_of(f"model/{path.name}") - allowed)

    def test_no_module_name_can_shadow_the_standard_library_when_the_package_dir_is_on_sys_path(self):
        # tests append the package directory to sys.path; select.py is unavoidable (it is a Home
        # Assistant platform), so the guard is that it is appended, never prepended
        own = {p.stem for p in FISHING_DIR.glob("*.py")}
        shadows = own & set(sys.stdlib_module_names)
        self.assertEqual(shadows, {"select"})
        for path in (Path(__file__).resolve().parent).glob("*.py"):
            if path.name == Path(__file__).name:
                continue                                    # this file quotes the pattern it searches for
            self.assertNotIn('sys.path.insert(0, str(ROOT / "custom_components" / "fishing"))', path.read_text(encoding="utf-8"), path.name)


class TranslationTest(unittest.TestCase):
    def test_entity_translation_keys_used_in_code_have_names(self):
        for module, platform in (("sensor.py", "sensor"), ("binary_sensor.py", "binary_sensor"), ("select.py", "select")):
            keys = set(re.findall(r'translation_key="([a-z_]+)"', source(module))) | set(re.findall(r'_attr_translation_key = "([a-z_]+)"', source(module)))
            with self.subTest(platform=platform):
                self.assertTrue(keys)
                names = STRINGS["entity"][platform]
                self.assertEqual(keys, set(names))
                self.assertTrue(all(v["name"] for v in names.values()))

    def test_every_selector_option_has_a_label(self):
        wanted = {
            "spot_kind": const.SPOT_KINDS,
            "water_body": tuple(BODY_CLASSES),
            "catchment": tuple(hydro.CATCHMENT_RESPONSES),
            "pressure_source": const.PRESSURE_SOURCES,
        }
        for key, values in wanted.items():
            with self.subTest(selector=key):
                labels = STRINGS["selector"][key]["options"]
                self.assertEqual(set(labels), set(values))
                self.assertTrue(all(labels.values()))

    def test_selector_keys_used_by_the_flow_exist(self):
        used = set(re.findall(r'_dropdown\([^,]+,\s*"([a-z_]+)"\)', source("config_flow.py")))
        self.assertEqual(used, set(STRINGS["selector"]))

    def test_every_flow_step_has_a_title_and_labels_for_its_fields(self):
        flow = source("config_flow.py")
        for step in re.findall(r'step_id="([a-z_]+)"', flow):
            with self.subTest(step=step):
                scope = STRINGS["options"]["step"] if step in ("init", "shelter") else STRINGS["config"]["step"]
                self.assertIn(step, scope)
                self.assertTrue(scope[step]["title"])

    def test_form_fields_have_labels(self):
        config_fields = {f for step in STRINGS["config"]["step"].values() for f in step.get("data", {})}
        for key in (const.CONF_NAME, const.CONF_LATITUDE, const.CONF_LONGITUDE, const.CONF_ELEVATION, const.CONF_SPOT_KIND, const.CONF_WATER_BODY,
                    const.CONF_TIDAL, const.CONF_TIDE_RANGE, const.CONF_CATCHMENT, const.CONF_PRESSURE_SOURCE, const.CONF_WATER_TEMP_ENTITY, const.CONF_SPECIES):
            with self.subTest(field=key):
                self.assertIn(key, config_fields)
        shelter = STRINGS["options"]["step"]["shelter"]["data"]
        self.assertEqual(set(SECTOR_ORDER) | {const.CONF_BANK_ASPECT}, set(shelter))
        self.assertTrue(set(STRINGS["config"]["step"]["spot"]["data"]) >= {const.CONF_NAME, const.CONF_WATER_BODY})

    def test_errors_raised_by_the_flow_are_translated(self):
        raised = set(re.findall(r'errors\["base"\] = "([a-z_]+)"', source("config_flow.py")))
        self.assertEqual(raised, set(STRINGS["config"]["error"]))
        self.assertIn("already_configured", STRINGS["config"]["abort"])

    def test_the_service_is_described_and_registered(self):
        self.assertIn("refresh", STRINGS["services"])
        self.assertIn('SERVICE_REFRESH = "refresh"', source("__init__.py"))
        self.assertTrue(source("services.yaml").startswith("refresh:"))


class WiringTest(unittest.TestCase):
    def test_platforms_listed_in_init_exist_as_modules(self):
        listed = set(re.findall(r"Platform\.([A-Z_]+)", source("__init__.py")))
        self.assertEqual({p.lower() for p in listed}, {"sensor", "binary_sensor", "select"})
        for name in listed:
            self.assertTrue((FISHING_DIR / f"{name.lower()}.py").exists())

    def test_every_config_key_the_flow_writes_is_read_by_spotconfig(self):
        reader = source("spot.py")
        for key in re.findall(r'^(CONF_[A-Z_]+) = "', source("const.py"), re.M):
            with self.subTest(key=key):
                self.assertIn(f"const.{key}", reader)

    def test_species_names_are_unique_because_the_select_uses_them_as_options(self):
        names = list(sp.species_names().values())
        self.assertEqual(len(names), len(set(names)))

    def test_no_update_listener_is_used_with_the_auto_reloading_options_flow(self):
        # OptionsFlowWithReload forbids integrations that also register update listeners
        self.assertNotIn("add_update_listener", source("__init__.py") + source("coordinator.py"))
        self.assertIn("OptionsFlowWithReload", source("config_flow.py"))


if __name__ == "__main__":
    unittest.main()
