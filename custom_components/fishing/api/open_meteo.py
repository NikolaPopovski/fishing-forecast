"""Open-Meteo: request shapes and tolerant parsing, with no HTTP of its own.

Everything here is stdlib-only and network-free. The client is handed a `fetch(url, params)`
coroutine by the integration (which uses Home Assistant's shared aiohttp session), so the parsing
and the request building can be tested against recorded real responses (tests/fixtures/).

What the real API does that a naive parser trips over (all of it seen in the recorded fixtures):

* With `timezone=auto` the hourly times are **naive local wall-clock strings** - exactly the
  convention every series in the model uses - and the response names the IANA zone and the
  offset. The model keeps the zone name (not the offset) so a DST change inside a 16-day window
  stays right.
* The marine endpoint answers for inland points too, but with `sea_surface_temperature` null in
  every hour (while `wave_height` is still filled). An all-null series is "no sea temperature",
  never zeros.
* The flood endpoint is daily (`YYYY-MM-DD`), only where a river-gauge model cell exists.
* The geocoder reports `elevation: -9999.0` for places with no known elevation.
* Errors arrive as HTTP 200 JSON: `{"error": true, "reason": "..."}`.

Missing data is dropped, never invented: a None, NaN or non-numeric value simply produces no
sample, and a missing variable produces no series.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Awaitable, Callable, Mapping

_LOGGER = logging.getLogger(__name__)

Sample = tuple[datetime, float]

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
FLOOD_URL = "https://flood-api.open-meteo.com/v1/flood"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

#: Open-Meteo hourly variable -> the SpotContext series name the model reads
HOURLY_TO_SERIES: dict[str, str] = {
    "temperature_2m": "air_temp",
    "apparent_temperature": "apparent_temp",
    "relative_humidity_2m": "humidity",
    "dew_point_2m": "dewpoint",
    "wind_speed_10m": "wind_speed",
    "wind_gusts_10m": "wind_gusts",
    "wind_direction_10m": "wind_dir",
    "cloud_cover": "cloud",
    "cloud_cover_low": "cloud_low",
    "cloud_cover_mid": "cloud_mid",
    "cloud_cover_high": "cloud_high",
    "precipitation": "precip",
    "precipitation_probability": "precip_prob",
    "rain": "rain",
    "showers": "showers",
    "uv_index": "uv_index",
    "shortwave_radiation": "shortwave",
    "cape": "cape",
    "visibility": "visibility",
    "soil_moisture_0_to_1cm": "soil_moisture",
}
#: which Open-Meteo variable feeds the single 'pressure' series (see SpotContext.pressure_source)
PRESSURE_VARIABLE = {"mslp": "pressure_msl", "station": "surface_pressure"}
HOURLY_VARIABLES: tuple[str, ...] = tuple(HOURLY_TO_SERIES) + tuple(PRESSURE_VARIABLE.values())

#: archive daily variable -> climatology variable (units converted in parse_archive_daily)
ARCHIVE_DAILY = (
    "temperature_2m_mean",
    "temperature_2m_max",
    "temperature_2m_min",
    "surface_pressure_mean",
    "pressure_msl_mean",
    "wind_speed_10m_mean",
    "cloud_cover_mean",
    "relative_humidity_2m_mean",
    "precipitation_sum",
)

#: Open-Meteo's "no elevation known" marker; anything this low is not a real elevation
_ELEVATION_MISSING_BELOW = -900.0


class OpenMeteoError(Exception):
    """The API could not be reached or answered with an error or an unusable payload."""


# --------------------------------------------------------------------------------------
# request building
# --------------------------------------------------------------------------------------


def _coord(value: float) -> float:
    return round(float(value), 4)


def forecast_params(latitude: float, longitude: float, *, past_days: int, forecast_days: int) -> dict[str, Any]:
    """Hourly weather for the model: local time, m/s, both pressure flavours."""
    return {
        "latitude": _coord(latitude),
        "longitude": _coord(longitude),
        "hourly": ",".join(HOURLY_VARIABLES),
        "past_days": int(past_days),
        "forecast_days": int(forecast_days),
        "wind_speed_unit": "ms",
        "timezone": "auto",
    }


def marine_params(latitude: float, longitude: float, *, past_days: int, forecast_days: int) -> dict[str, Any]:
    return {
        "latitude": _coord(latitude),
        "longitude": _coord(longitude),
        "hourly": "sea_surface_temperature,wave_height",
        "past_days": int(past_days),
        "forecast_days": int(forecast_days),
        "timezone": "auto",
    }


def flood_params(latitude: float, longitude: float, *, past_days: int, forecast_days: int) -> dict[str, Any]:
    return {
        "latitude": _coord(latitude),
        "longitude": _coord(longitude),
        "daily": "river_discharge",
        "past_days": int(past_days),
        "forecast_days": int(forecast_days),
    }


def archive_params(latitude: float, longitude: float, start: date, end: date) -> dict[str, Any]:
    return {
        "latitude": _coord(latitude),
        "longitude": _coord(longitude),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": ",".join(ARCHIVE_DAILY),
        "wind_speed_unit": "ms",
        "timezone": "auto",
    }


def geocoding_params(name: str, count: int = 5, language: str = "en") -> dict[str, Any]:
    return {"name": name.strip(), "count": int(count), "language": language, "format": "json"}


# --------------------------------------------------------------------------------------
# tolerant parsing
# --------------------------------------------------------------------------------------


def _check(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise OpenMeteoError("response is not a JSON object")
    if payload.get("error"):
        raise OpenMeteoError(str(payload.get("reason") or "the API reported an error"))
    return payload


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _time(text: Any) -> datetime | None:
    """Naive datetime from an ISO string; an offset, if any, is dropped (the times are local)."""
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def parse_block(payload: Mapping[str, Any], block: str) -> dict[str, list[Sample]]:
    """Every non-empty series of an hourly/daily block, keyed by the API's own variable names."""
    data = payload.get(block)
    if not isinstance(data, Mapping):
        return {}
    times_raw = data.get("time")
    if not isinstance(times_raw, list):
        return {}
    times = [_time(t) for t in times_raw]
    out: dict[str, list[Sample]] = {}
    for name, values in data.items():
        if name == "time" or not isinstance(values, list):
            continue
        series: list[Sample] = []
        for when, raw in zip(times, values):
            value = _number(raw)
            if when is not None and value is not None:
                series.append((when, value))
        if series:
            out[name] = series
    return out


@dataclass
class Forecast:
    """The hourly weather answer, already renamed for the model."""

    series: dict[str, list[Sample]] = field(default_factory=dict)     # SpotContext series names
    raw: dict[str, list[Sample]] = field(default_factory=dict)        # Open-Meteo names
    timezone: str | None = None
    utc_offset_seconds: int | None = None
    elevation_m: float | None = None
    latitude: float | None = None
    longitude: float | None = None


def _elevation(value: Any) -> float | None:
    out = _number(value)
    return None if out is None or out < _ELEVATION_MISSING_BELOW else out


def parse_forecast(payload: Any, pressure_source: str = "mslp") -> Forecast:
    data = _check(payload)
    raw = parse_block(data, "hourly")
    if not raw:
        raise OpenMeteoError("the forecast contained no usable hourly data")
    series = {HOURLY_TO_SERIES[k]: v for k, v in raw.items() if k in HOURLY_TO_SERIES}
    pressure = raw.get(PRESSURE_VARIABLE.get(pressure_source, PRESSURE_VARIABLE["mslp"]))
    if pressure:
        series["pressure"] = pressure
    offset = _number(data.get("utc_offset_seconds"))
    tz = data.get("timezone")
    return Forecast(
        series=series,
        raw=raw,
        timezone=tz if isinstance(tz, str) and tz else None,
        utc_offset_seconds=None if offset is None else int(offset),
        elevation_m=_elevation(data.get("elevation")),
        latitude=_number(data.get("latitude")),
        longitude=_number(data.get("longitude")),
    )


@dataclass
class Marine:
    sea_surface_temp: list[Sample] = field(default_factory=list)
    wave_height: list[Sample] = field(default_factory=list)

    @property
    def has_sea_surface_temp(self) -> bool:
        return bool(self.sea_surface_temp)


def parse_marine(payload: Any) -> Marine:
    """Marine data; an inland point yields an empty `sea_surface_temp` (all-null upstream)."""
    raw = parse_block(_check(payload), "hourly")
    return Marine(sea_surface_temp=raw.get("sea_surface_temperature", []), wave_height=raw.get("wave_height", []))


def parse_flood(payload: Any) -> list[Sample]:
    """Daily river discharge in m3/s; empty where no gauge-model cell covers the point."""
    return parse_block(_check(payload), "daily").get("river_discharge", [])


def parse_archive_daily(payload: Any) -> list[tuple[date, dict[str, float]]]:
    """Daily history as climatology rows (its variable names and its 0..1 cloud/humidity units)."""
    raw = parse_block(_check(payload), "daily")
    by_day: dict[date, dict[str, float]] = {}

    def put(var: str, source: str, scale: float = 1.0) -> None:
        for when, value in raw.get(source, []):
            by_day.setdefault(when.date(), {})[var] = value * scale

    put("air_temp", "temperature_2m_mean")
    put("air_temp_max", "temperature_2m_max")
    put("air_temp_min", "temperature_2m_min")
    put("pressure", "surface_pressure_mean")
    put("pressure_slp", "pressure_msl_mean")
    put("wind_speed", "wind_speed_10m_mean")
    put("cloud", "cloud_cover_mean", 0.01)                 # climatology documents 0..1
    put("humidity", "relative_humidity_2m_mean", 0.01)
    put("precip", "precipitation_sum")
    for row in by_day.values():
        if "air_temp_max" in row and "air_temp_min" in row:
            row["air_temp_range"] = row["air_temp_max"] - row["air_temp_min"]
    return sorted(by_day.items())


@dataclass
class Place:
    name: str
    latitude: float
    longitude: float
    elevation_m: float | None
    timezone: str | None
    country: str = ""
    admin1: str = ""

    @property
    def label(self) -> str:
        return ", ".join(part for part in (self.name, self.admin1, self.country) if part)


def parse_geocoding(payload: Any) -> list[Place]:
    """Search results; entries without coordinates are skipped. No results is an empty list."""
    data = _check(payload)
    results = data.get("results")
    if not isinstance(results, list):
        return []
    out: list[Place] = []
    for item in results:
        if not isinstance(item, Mapping):
            continue
        lat, lon = _number(item.get("latitude")), _number(item.get("longitude"))
        name = item.get("name")
        if lat is None or lon is None or not isinstance(name, str) or not name:
            continue
        tz = item.get("timezone")
        out.append(
            Place(
                name=name,
                latitude=lat,
                longitude=lon,
                elevation_m=_elevation(item.get("elevation")),
                timezone=tz if isinstance(tz, str) and tz else None,
                country=str(item.get("country") or ""),
                admin1=str(item.get("admin1") or ""),
            )
        )
    return out


def value_at(series: list[Sample], when: datetime, max_age_hours: float = 3.0) -> float | None:
    """Nearest sample to `when` within `max_age_hours` (None when the series is stale or empty)."""
    best: tuple[float, float] | None = None
    for t, v in series:
        age = abs((t - when).total_seconds()) / 3600.0
        if age <= max_age_hours and (best is None or age < best[0]):
            best = (age, v)
    return None if best is None else best[1]


# --------------------------------------------------------------------------------------
# the client: request + parse, transport injected
# --------------------------------------------------------------------------------------

Fetch = Callable[[str, Mapping[str, Any]], Awaitable[Any]]


class OpenMeteoClient:
    """Forecast is essential (raises); marine and flood are optional (None when unavailable)."""

    def __init__(self, fetch: Fetch):
        self._fetch = fetch

    async def _get(self, url: str, params: Mapping[str, Any]) -> Any:
        try:
            return await self._fetch(url, params)
        except OpenMeteoError:
            raise
        except Exception as err:                               # transport errors of any flavour
            raise OpenMeteoError(f"request failed: {err}") from err

    async def forecast(
        self, latitude: float, longitude: float, *, past_days: int, forecast_days: int, pressure_source: str = "mslp"
    ) -> Forecast:
        payload = await self._get(FORECAST_URL, forecast_params(latitude, longitude, past_days=past_days, forecast_days=forecast_days))
        return parse_forecast(payload, pressure_source)

    async def marine(self, latitude: float, longitude: float, *, past_days: int, forecast_days: int) -> Marine | None:
        try:
            payload = await self._get(MARINE_URL, marine_params(latitude, longitude, past_days=past_days, forecast_days=forecast_days))
            return parse_marine(payload)
        except OpenMeteoError as err:
            _LOGGER.debug("marine data unavailable: %s", err)
            return None

    async def flood(self, latitude: float, longitude: float, *, past_days: int, forecast_days: int) -> list[Sample] | None:
        try:
            payload = await self._get(FLOOD_URL, flood_params(latitude, longitude, past_days=past_days, forecast_days=forecast_days))
            return parse_flood(payload) or None
        except OpenMeteoError as err:
            _LOGGER.debug("river discharge unavailable: %s", err)
            return None

    async def archive(self, latitude: float, longitude: float, start: date, end: date) -> list[tuple[date, dict[str, float]]]:
        return parse_archive_daily(await self._get(ARCHIVE_URL, archive_params(latitude, longitude, start, end)))

    async def geocode(self, name: str, count: int = 5, language: str = "en") -> list[Place]:
        if not name or not name.strip():
            return []
        return parse_geocoding(await self._get(GEOCODING_URL, geocoding_params(name, count, language)))
