"""The integration serves the Lovelace card and loads it on every dashboard (once)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.fishing import card, const


def fake_http(hass: HomeAssistant) -> MagicMock:
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    return hass.http


async def test_the_card_is_served_and_added_to_every_dashboard(hass: HomeAssistant, tmp_path):
    js = tmp_path / "fishing-card.js"
    js.write_text("// card")
    http = fake_http(hass)
    with patch("homeassistant.components.frontend.add_extra_js_url") as add:
        assert await card.async_register_card(hass, js) is True

    (configs,), _ = http.async_register_static_paths.call_args
    assert len(configs) == 1
    assert configs[0].url_path == "/fishing/fishing-card.js"
    assert configs[0].path == str(js)
    assert configs[0].cache_headers is False                  # a new version must reach the browser at once
    add.assert_called_once_with(hass, f"/fishing/fishing-card.js?v={const.VERSION}")


async def test_registering_twice_does_nothing_the_second_time(hass: HomeAssistant, tmp_path):
    js = tmp_path / "fishing-card.js"
    js.write_text("// card")
    http = fake_http(hass)
    with patch("homeassistant.components.frontend.add_extra_js_url") as add:
        assert await card.async_register_card(hass, js) is True
        assert await card.async_register_card(hass, js) is True
    assert http.async_register_static_paths.await_count == 1
    assert add.call_count == 1


async def test_a_missing_card_file_is_not_an_error(hass: HomeAssistant, tmp_path):
    http = fake_http(hass)
    assert await card.async_register_card(hass, tmp_path / "nope.js") is False
    http.async_register_static_paths.assert_not_awaited()


async def test_no_web_server_means_no_registration(hass: HomeAssistant, tmp_path):
    js = tmp_path / "fishing-card.js"
    js.write_text("// card")
    hass.http = None
    assert await card.async_register_card(hass, js) is False


async def test_setting_up_the_integration_registers_the_card(hass: HomeAssistant, tmp_path):
    js = tmp_path / "fishing-card.js"
    js.write_text("// card")
    fake_http(hass)
    with patch.object(card, "DEFAULT_PATH", js), patch("homeassistant.components.frontend.add_extra_js_url") as add:
        assert await async_setup_component(hass, const.DOMAIN, {})
    add.assert_called_once()
    assert hass.data[card.DATA_REGISTERED] is True
