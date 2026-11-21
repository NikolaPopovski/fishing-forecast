#!/usr/bin/env python3
"""Regenerate tests/fixtures/events_reference.json from the reference astronomy stack.

Home Assistant core computes its own sun/moon entities with `astral`, so astral is the most
relevant cross-check for this integration.  Run this from the dev venv (pip install astral)
whenever model/astronomy.py changes in a way that could move events, then commit the fixture;
tests/test_astronomy.py is offline and asserts against it forever after.

    python -m venv .venv && .venv/Scripts/pip install astral
    .venv/Scripts/python scripts/regen_event_fixtures.py

Notes on the reference:
* astral raises ValueError when it believes a body never rises/sets on that date; those
  dates are stored as null.  Around polar grazing days our implementation is *more* exact
  (it finds the horizon graze, astral gives up), and on such days a second rise/set pair
  exists in the same local day - we report the first one, so the test only demands agreement
  at mid-latitude sites.
* astral's rise/set for the Moon omits the augmentation of the semi-diameter, which we also
  ignore (both use a mean lunar horizon altitude), so the comparison is like-for-like.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astral import Observer
from astral.moon import moonrise, moonset
from astral.sun import noon, sunrise, sunset

sys.path.insert(0, "custom_components/fishing")

SITES = [
    # label, lat, lon, elevation, IANA tz, first day, days
    ("enkhuizen_nl", 52.3702, 5.2722, 2, "Europe/Amsterdam", (2026, 3, 1), 75),
    ("new_york_us", 40.7128, -74.0060, 10, "America/New_York", (2026, 3, 1), 75),
    ("auckland_nz", -36.8485, 174.7633, 6, "Pacific/Auckland", (2026, 6, 1), 75),
    ("singapore_sg", 1.3521, 103.8198, 15, "Asia/Singapore", (2026, 9, 1), 75),
    ("reykjavik_is", 64.1466, -21.9426, 60, "Atlantic/Reykjavik", (2026, 6, 1), 75),
    # True polar sites: astral gives up on these ("never rises"), so they are stored only to
    # exercise our polar code paths; the tests check them by altitude identity, not agreement.
    ("longyearbyen_winter", 78.2231, 15.6489, 20, "Arctic/Longyearbyen", (2026, 1, 1), 40),
    ("longyearbyen_summer", 78.2231, 15.6489, 20, "Arctic/Longyearbyen", (2026, 6, 1), 40),
]


def main() -> int:
    out: dict[str, list[dict]] = {}
    for label, lat, lon, elev, tzname, first, days in SITES:
        tz = ZoneInfo(tzname)
        obs = Observer(lat, lon, elev)
        start = datetime(first[0], first[1], first[2], tzinfo=tz)
        rows = []
        for i in range(days):
            d0 = start + timedelta(days=i)
            row: dict[str, object] = {
                "date": d0.date().isoformat(),
                "tz": tzname,
                # the offset that applied on *this* day; tests rebuild the observer's zone
                # from it so they need no tz database of their own
                "utc_offset_min": int(d0.utcoffset().total_seconds() // 60),
            }
            for key, fn in (("sunrise", sunrise), ("sunset", sunset), ("solar_noon", noon)):
                try:
                    val = fn(obs, d0.date(), tz)
                except ValueError:
                    val = None
                row[key] = val.isoformat() if val else None
            for key, fn in (("moonrise", moonrise), ("moonset", moonset)):
                try:
                    val = fn(obs, d0.date(), tz)
                except ValueError:
                    val = None
                row[key] = val.isoformat() if val else None
            rows.append(row)
        out[label] = rows
        print(f"{label}: {len(rows)} days")
    path = "tests/fixtures/events_reference.json"
    with open(path, "w") as fh:
        json.dump({"sites": [s[:5] for s in SITES], "events": out}, fh, indent=1)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
