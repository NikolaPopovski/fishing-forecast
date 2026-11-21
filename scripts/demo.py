"""Offline demo: a 48 h forecast table, so a human can eyeball whether the weights feel right.

    python scripts/demo.py                    # 3-hourly table, 3 species per spot, front on day 2
    python scripts/demo.py --step 1           # hourly
    python scripts/demo.py --at 26            # advice for the hour the front arrives (default)
    python scripts/demo.py --species pike carp perch --kinds lake

Everything is synthetic and stdlib-only: no network, no tz database, no HA. The same weather (a
calm, mild June day, then a front crossing on day two) is run over a lake, a river and a tidal
coast, so differences between the columns come from the species and the water, not the sky.

The point is to read it like an angler: does pike peak at dusk, does the front lift the predators
just before it arrives and hammer the carp, does the river go off after the rain? If a number looks
absurd, fix the species profile or the response curve - never the physics.
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "custom_components" / "fishing"))   # appended, never prepended: select.py must not shadow the stdlib
sys.path.insert(0, str(ROOT / "tests"))

from model import advice, scoring, species  # noqa: E402
from support import SPOT_START, spot_context  # noqa: E402

DAYS_OF_HISTORY = 22
START = SPOT_START + timedelta(days=DAYS_OF_HISTORY)          # 2025-06-23 00:00, spot-local
I0 = 24 * DAYS_OF_HISTORY

DEFAULT_SPECIES = {
    "lake": ("pike", "carp", "zander"),
    "river": ("barbel", "brown_trout_sea_trout", "pike"),
    "coast": ("European_bass", "flounder_plaice", "coastal_cod"),
}


# --- the weather: calm, then a front crossing around hour 30 ---------------------------------


def _ramp(h, h0, h1, v0, v1):
    return v0 + (v1 - v0) * min(1.0, max(0.0, (h - h0) / (h1 - h0)))


def pressure(i, t):
    h = i - I0
    if h < 18:
        return 1020.0
    if h < 32:
        return _ramp(h, 18, 32, 1020.0, 1008.0)
    return _ramp(h, 32, 44, 1008.0, 1016.0)


def wind(i, t):
    h = i - I0
    return _ramp(h, 24, 32, 3.0, 9.0) if h < 36 else _ramp(h, 36, 46, 9.0, 4.0)


def wind_dir(i, t):
    return _ramp(i - I0, 22, 32, 200.0, 262.0)


def cloud(i, t):
    h = i - I0
    return _ramp(h, 14, 28, 40.0, 95.0) if h < 30 else _ramp(h, 30, 42, 95.0, 45.0)


def rain(i, t):
    return 3.0 if 27 <= i - I0 <= 31 else 0.0


def air(i, t):
    base = 16.0 + 4.0 * math.sin(2 * math.pi * (t.hour - 9) / 24.0)
    return base - (4.0 if i - I0 >= 32 else 0.0)


def river_discharge(day):
    """Daily gauge: the river only rises the day *after* the rain (the front's rain falls on day 2)."""
    lag_day = DAYS_OF_HISTORY + 1
    return 100.0 if day <= lag_day else 100.0 * (1.0 + 0.7 * (day - lag_day))


def build(kind):
    common = dict(pressure=pressure, wind=wind, wind_dir=wind_dir, cloud=cloud, precip=rain, air=air, days=DAYS_OF_HISTORY, ahead=3)
    if kind == "river":
        return spot_context(kind="river", body="river", discharge=river_discharge, **common)
    if kind == "coast":
        return spot_context(kind="coast", body="coastal_sea", tidal=True, tide_range_m=2.0, lon=4.4, **common)
    return spot_context(kind="lake", body="lake_small", **common)


# --- printing ---------------------------------------------------------------------------------


def short(sid, width=9):
    return species.profile(sid).name.split()[0][:width]


def table(ctx, kind, sids, step):
    profs = [species.profile(s) for s in sids]
    print(f"\n=== {kind.upper()}  ({ctx.spot_kind}, {ctx.water_body.kind}{', tidal' if ctx.tidal else ''}) ===")
    head = f"{'time':<11}{'sun':>5}{'hPa/3h':>8}{'wind':>6}{'cloud':>6}{'rain':>5}   " + "".join(f"{short(s):>10}" for s in sids)
    print(head)
    print("-" * len(head))
    curves = {p.id: scoring.hourly_curve(ctx, START, 48, p) for p in profs}
    for idx in range(0, 48, step):
        t, ev0 = curves[profs[0].id][idx]
        pa = ev0.pressure
        tend = "" if pa is None or pa.tendency_3h is None else f"{pa.tendency_3h:+.1f}"
        o = ev0.obs
        cells = "".join(f"{curves[p.id][idx][1].score:>6.1f}{curves[p.id][idx][1].band[:3]:>4}" for p in profs)
        print(
            f"{t:%a %H:%M}  {ev0.sun_altitude:>5.0f}{tend:>8}"
            f"{(o['wind_speed'] if o['wind_speed'] is not None else float('nan')):>6.1f}"
            f"{(o['cloud'] if o['cloud'] is not None else float('nan')):>6.0f}"
            f"{(o['precip'] if o['precip'] is not None else float('nan')):>5.1f}   {cells}"
        )
    first = profs[0]
    spans = scoring.spans_from_curve(curves[first.id], 6.0)
    print(f"\nbest windows for {first.name} (score >= 6.0):")
    if not spans:
        print("  none")
    for s in spans:
        print(f"  {s.start:%a %H:%M} - {s.end:%a %H:%M}   peak {s.peak_score:.1f} at {s.peak_at:%H:%M}")


def advice_block(ctx, sids, hours_in):
    when = START + timedelta(hours=hours_in)
    prepared = scoring.prepare(ctx, when, now=START)
    print(f"\nadvice at {when:%a %H:%M}:")
    for sid in sids:
        prof = species.profile(sid)
        ev = scoring.evaluate(ctx, when, prof, prepared)
        out = advice.as_dict(ctx, ev, prof)
        print(f"  {out['headline']}")
        for line in out["reasons"]:
            print(f"      {line}")
        for line in out["tactics"]:
            print(f"      > {line}")
        print(f"      when: {out['window']}")
        print(f"      conditions: {', '.join(out['conditions']) or '-'}  | confidence {ev.confidence:.2f}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--step", type=int, default=3, help="hours between table rows (default 3)")
    ap.add_argument("--at", type=int, default=26, help="hour offset for the advice block (default 26, the front)")
    ap.add_argument("--kinds", nargs="+", default=list(DEFAULT_SPECIES), choices=list(DEFAULT_SPECIES))
    ap.add_argument("--species", nargs="+", help="species ids (default: three per spot kind)")
    args = ap.parse_args(argv)

    print(f"Synthetic spot, {START:%Y-%m-%d} 00:00 local. Front crosses around hour 30 ({START + timedelta(hours=30):%a %H:%M}).")
    for kind in args.kinds:
        ctx = build(kind)
        sids = tuple(args.species) if args.species else DEFAULT_SPECIES[kind]
        table(ctx, kind, sids, args.step)
        advice_block(ctx, sids, args.at)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
