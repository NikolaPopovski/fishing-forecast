"""The one place Home Assistant's HTTP session meets the (transport-free) Open-Meteo client."""
from __future__ import annotations

from typing import Any, Mapping

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api.open_meteo import OpenMeteoClient, OpenMeteoError

TIMEOUT = aiohttp.ClientTimeout(total=30)


async def fetch_json(hass: HomeAssistant, url: str, params: Mapping[str, Any]) -> Any:
    """GET `url` with `params` and return the decoded JSON.

    Open-Meteo reports its own errors as JSON with a 4xx status (`{"error": true, "reason": ...}`),
    so a 4xx whose body is such an object is returned for the parser to turn into a readable error;
    any other failure status is an OpenMeteoError.
    """
    session = async_get_clientsession(hass)
    async with session.get(url, params=params, timeout=TIMEOUT) as resp:
        try:
            data = await resp.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError):
            data = None
        if resp.status >= 400 and not (isinstance(data, dict) and data.get("error")):
            raise OpenMeteoError(f"HTTP {resp.status}")
        return data


def client_for(hass: HomeAssistant) -> OpenMeteoClient:
    return OpenMeteoClient(lambda url, params: fetch_json(hass, url, params))
