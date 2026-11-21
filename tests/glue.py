"""Load the integration's pure modules without importing Home Assistant.

`custom_components/fishing/__init__.py` imports Home Assistant, which the test environment does
not have (stdlib only, offline). The pure modules (`const`, `api.open_meteo`, `spot`, `snapshot`)
use relative imports, so they need a parent package: this registers a bare stand-in named
`fishing` that points at the real directory but never executes its `__init__.py`.

Everything the glue tests import comes through this `fishing` package (including `fishing.model`),
which is a separate import of the model from the top-level `model` the science tests use; they
never mix objects.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FISHING_DIR = ROOT / "custom_components" / "fishing"
FIXTURES = ROOT / "tests" / "fixtures"


def load_package() -> types.ModuleType:
    existing = sys.modules.get("fishing")
    if existing is not None and getattr(existing, "__glue_stub__", False):
        return existing
    pkg = types.ModuleType("fishing")
    pkg.__path__ = [str(FISHING_DIR)]
    pkg.__glue_stub__ = True
    sys.modules["fishing"] = pkg
    return pkg


def fixture(name: str):
    """A recorded real API response (tests/fixtures/<name>.json)."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


load_package()
