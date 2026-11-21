"""Shared offline fixtures for the model tests (stdlib only, deterministic, no network).

`synthetic_store` is the two-year temperate-maritime weather history the climatology tests use;
the pressure, water and hydro tests reuse it so they get realistic anomalies for free.
"""
from __future__ import annotations

import math
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "custom_components" / "fishing"))   # appended, never prepended: select.py must not shadow the stdlib

from model import climatology as cl  # noqa: E402


def synthetic_store(latitude=52.37, elevation=2.0, years=2, seed=7) -> cl.Climatology:
    """Two years of plausible temperate-maritime daily weather, no network needed."""
    rnd = _Rng(seed)
    store = cl.Climatology(latitude, elevation, "synthetic")
    day = date(2024, 1, 1)
    for i in range(366 * years):
        d = day + timedelta(days=i)
        doy = d.timetuple().tm_yday
        season = math.sin(2 * math.pi * (doy - 110) / 365.0)
        t = 10.5 + 9.5 * season + rnd.gauss(0, 1.9)
        p = 1013.0 - 5.0 * math.sin(2 * math.pi * (doy + 30) / 365.0) + rnd.gauss(0, 4.2)
        w = 5.4 + 2.2 * abs(math.cos(2 * math.pi * (doy - 90) / 365.0)) + rnd.gauss(0, 1.5)
        store.ingest_daily(
            d,
            {
                "air_temp": round(t, 2),
                "air_temp_max": round(t + 3.5, 2),
                "air_temp_min": round(t - 3.5, 2),
                "pressure": round(p, 2),
                "wind_speed": round(max(0.2, w), 2),
            },
        )
    return store


class _Rng:
    """Tiny deterministic gaussian source (avoid depending on global random state)."""

    def __init__(self, seed: int):
        self.state = seed & 0x7FFFFFFF or 1

    def uniform(self) -> float:
        self.state = (1103515245 * self.state + 12345) & 0x7FFFFFFF
        return self.state / 0x7FFFFFFF

    def gauss(self, mu: float, sigma: float) -> float:
        u = max(1e-9, self.uniform())
        v = self.uniform()
        return mu + sigma * math.sqrt(-2 * math.log(u)) * math.cos(2 * math.pi * v)


# --------------------------------------------------------------------------------------
# a whole spot: the SpotContext the scoring tests (and scripts/demo.py) run against
# --------------------------------------------------------------------------------------

from datetime import datetime  # noqa: E402

from model import water_temp as _wt  # noqa: E402
from model.context import SpotContext  # noqa: E402

SPOT_START = datetime(2025, 6, 1)


def spot_context(
    *,
    name="lake",
    lat=52.7,
    lon=5.3,
    utc_offset_min=120,
    kind="lake",
    body="lake_small",
    start=SPOT_START,
    days=22,
    ahead=3,
    pressure=lambda i, t: 1018.0,
    air=lambda i, t: 16.0 + 4.0 * math.sin(2 * math.pi * (t.hour - 9) / 24.0),
    cloud=50.0,
    wind=3.5,
    wind_dir=200.0,
    precip=0.0,
    discharge=None,
    with_clim=True,
    tidal=False,
    tide_range_m=0.0,
    water_sensor=None,
) -> SpotContext:
    """Hourly history of `days` days followed by `ahead` days of "forecast", all deterministic.

    The defaults are a calm, mild, partly cloudy June on a small Dutch lake with a steady
    barometer: a neutral baseline that each test then perturbs in exactly one way.
    """
    ctx = SpotContext(
        name=name,
        lat=lat,
        lon=lon,
        tz_offset_min=utc_offset_min,
        spot_kind=kind,
        water_body=_wt.WaterBody(kind=body),
        pressure_source="station",
        tidal=tidal,
        tide_range_m=tide_range_m,
        clim=synthetic_store(lat) if with_clim else None,
        water_sensor=water_sensor,
    )
    n = 24 * (days + ahead)

    def hourly(fn):
        return [(start + timedelta(hours=i), fn(i, start + timedelta(hours=i))) for i in range(n)]

    def uv(i, t):
        return 6.0 * math.sin(math.pi * (t.hour - 4.5) / 17.0) if 4.5 < t.hour < 21.5 else 0.0

    ctx.series["air_temp"] = hourly(air)
    ctx.series["pressure"] = hourly(pressure)
    def varying(x):
        """A constant, or a function of (hour index, datetime) for weather that changes."""
        return x if callable(x) else (lambda i, t: x)

    ctx.series["wind_speed"] = hourly(varying(wind))
    ctx.series["wind_dir"] = hourly(varying(wind_dir))
    ctx.series["cloud"] = hourly(varying(cloud))
    ctx.series["humidity"] = hourly(lambda i, t: 70.0)
    ctx.series["precip"] = hourly(varying(precip))
    ctx.series["uv_index"] = hourly(uv)
    if discharge is not None:
        ctx.series["discharge"] = [(start + timedelta(days=d), discharge(d)) for d in range(days + ahead)]
    return ctx
