"""Fixtures for the Home Assistant level tests.

These need the real `homeassistant` package, so they run in the dedicated Python 3.14 environment
(`scratch/ha314`), NOT in the stdlib-only gate:

    scratch/ha314/Scripts/python.exe -m pytest tests/ha -q

Open-Meteo is replaced by the recorded real responses in tests/fixtures, so nothing touches the network.
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))                    # so `custom_components.fishing` imports

from custom_components.fishing import const  # noqa: E402
from custom_components.fishing.api import open_meteo as om  # noqa: E402

pytest_plugins = "pytest_homeassistant_custom_component"

# --- Windows only ------------------------------------------------------------------------
# The Home Assistant test plugin blocks every socket so that no test can reach the network. On
# Windows asyncio's own wake-up pipe is a loopback socketpair, which that guard also blocks before
# any test runs. Give *only* socket.socketpair a real implementation (built from the genuine socket
# class, captured now, before the guard replaces it). Every other socket stays blocked.
if sys.platform == "win32":
    _REAL_SOCKET = socket.socket

    def _loopback_socketpair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
        listener = _REAL_SOCKET(family, type, proto)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            client = _REAL_SOCKET(family, type, proto)
            client.connect(listener.getsockname())
            handle, _ = listener._accept()                      # accept() would build the guarded class
            return _REAL_SOCKET(family, type, proto, fileno=handle), client
        finally:
            listener.close()

    socket.socketpair = _loopback_socketpair

FIXTURES = ROOT / "tests" / "fixtures"
#: the recorded forecast covers 2026-11-19 .. 2026-11-22 local; "now" sits in the middle of it
NOW = "2026-11-21 12:00:00+00:00"


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class FakeOpenMeteo:
    """Serves the recorded responses by URL; individual routes can be replaced or made to fail."""

    def __init__(self) -> None:
        self.routes = {
            om.FORECAST_URL: fixture("open_meteo_forecast"),
            om.MARINE_URL: fixture("open_meteo_marine_inland"),
            om.FLOOD_URL: fixture("open_meteo_flood"),
            om.ARCHIVE_URL: fixture("open_meteo_archive"),
            om.GEOCODING_URL: fixture("open_meteo_geocoding"),
        }
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, hass, url, params):
        self.calls.append((url, dict(params)))
        result = self.routes[url]
        if isinstance(result, Exception):
            raise result
        return result

    def count(self, url: str) -> int:
        return sum(1 for u, _ in self.calls if u == url)


@pytest.fixture
def open_meteo():
    fake = FakeOpenMeteo()

    async def fetch_json(hass, url, params):                 # a real coroutine function, so it is awaited
        return await fake(hass, url, params)

    with patch("custom_components.fishing.transport.fetch_json", new=fetch_json):
        yield fake


@pytest.fixture
def now(freezer):
    freezer.move_to(NOW)
    return freezer


def make_entry(**overrides) -> MockConfigEntry:
    data = {
        const.CONF_NAME: "Test mark",
        const.CONF_LATITUDE: 52.7,
        const.CONF_LONGITUDE: 5.3,
        const.CONF_SPOT_KIND: "lake",
        const.CONF_WATER_BODY: "lake_small",
        const.CONF_TIDAL: False,
        const.CONF_CATCHMENT: "mixed_farmland",
        const.CONF_PRESSURE_SOURCE: "mslp",
    }
    data.update(overrides.pop("data", {}))
    return MockConfigEntry(
        domain=const.DOMAIN,
        title=data[const.CONF_NAME],
        data=data,
        options=overrides.pop("options", {const.CONF_SPECIES: "pike"}),
        unique_id=f"{data[const.CONF_LATITUDE]:.4f}_{data[const.CONF_LONGITUDE]:.4f}",
        **overrides,
    )
