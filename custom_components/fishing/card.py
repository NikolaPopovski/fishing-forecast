"""Serve the Lovelace card from the integration and load it on every dashboard.

The card is one file, `www/fishing-card.js` inside this integration, so it arrives with any install
(HACS, a zip, a copied folder). Serving it from here means nobody has to copy a file into
`/config/www` or add a dashboard resource by hand.

It is added with `add_extra_js_url`, which loads the module on every dashboard whatever mode it is in
(storage or YAML), so there is no resource collection to edit and nothing to clean up. Calling this
twice is harmless: the second call does nothing.
"""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from . import const

_LOGGER = logging.getLogger(__name__)

URL_PATH = f"/{const.DOMAIN}/fishing-card.js"
DEFAULT_PATH = Path(__file__).parent / "www" / "fishing-card.js"
DATA_REGISTERED = f"{const.DOMAIN}_card_registered"


async def async_register_card(hass: HomeAssistant, path: Path | None = None) -> bool:
    """Make the card available; returns whether it is now being served."""
    if hass.data.get(DATA_REGISTERED):
        return True
    path = path or DEFAULT_PATH
    if not path.is_file():
        _LOGGER.info("The Lovelace card file is not packaged with this install (%s); add /local/fishing-card.js yourself if you want the card", path)
        return False
    http = getattr(hass, "http", None)
    if http is None:                                       # no web server (tests, or a headless install)
        return False

    await http.async_register_static_paths([StaticPathConfig(URL_PATH, str(path), cache_headers=False)])
    try:
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, f"{URL_PATH}?v={const.VERSION}")
    except ImportError:                                    # no frontend: the file is served, nothing loads it
        _LOGGER.debug("The frontend is not available, so the card is served but not auto-loaded")
        return False
    hass.data[DATA_REGISTERED] = True
    return True
