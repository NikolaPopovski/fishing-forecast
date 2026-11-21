"""Derive the abridged lunar series used by model/astronomy.py.

Reference theory: full Meeus ch.33 (ELP2000-82b style) as implemented by astral 3.2, which
is what Home Assistant core itself uses for its sun/moon entities.

Method: orthogonal matching pursuit over a curated candidate set of arguments
(D, M, M', F) with both sin and cos components, over a 1950-2100 baseline so that all
constituent frequencies are resolvable.  Re-fitting the active set at each step keeps the
system well-conditioned (greedy amplitude projection alone is not, because a few
constituents are quasi-commensurable over short windows).

The resulting coefficients are copied into model/astronomy.py, and tests/test_astronomy.py
locks the achieved accuracy against tests/fixtures/lunar_reference.json.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys

import numpy as np

sys.path.insert(0, "custom_components/fishing")
from astral.moon import moon_position as astral_moon  # dev-only dependency
from model import astronomy as astro

EARTH_RADIUS_KM = 6378.137
N = 20000
SPAN_YEARS = 150.0
t0 = astro.to_jd(dt.datetime(1950, 1, 1, tzinfo=dt.timezone.utc))
jds = t0 + np.arange(N) * (SPAN_YEARS * 365.25 / N)

els = [astro.lunar_elements(float(jd)) for jd in jds]
Lp = np.array([e[0] for e in els])
D = np.array([e[1] for e in els])
M = np.array([e[2] for e in els])
Mp = np.array([e[3] for e in els])
F = np.array([e[4] for e in els])

ref_l, ref_b, ref_d = [], [], []
for jd in jds:
    a = astral_moon(float(jd) - 2451545.0)
    ra, dec = math.degrees(a.right_ascension), math.degrees(a.declination)
    eps = astro.ecliptic_obliquity(float(jd))
    ce, se = math.cos(math.radians(eps)), math.sin(math.radians(eps))
    lon = astro.wrap360(astro.atan2_deg(
        math.sin(math.radians(ra)) * ce + math.tan(math.radians(dec)) * se, math.cos(math.radians(ra))))
    lat = astro.asin_deg(math.sin(math.radians(dec)) * ce - math.cos(math.radians(dec)) * se * math.sin(math.radians(ra)))
    ref_l.append(lon)
    ref_b.append(lat)
    ref_d.append(a.distance * EARTH_RADIUS_KM)

y_lon = np.array([astro.wrap180(v - lp) for v, lp in zip(ref_l, Lp)])
y_lat = np.array(ref_b)
y_rad = np.array(ref_d) - astro.MEAN_MOON_DISTANCE

# selection-rule-flavoured candidate set (the published tables never need wild arguments)
CAND = []
for d_ in range(-6, 7):
    for m_ in range(-3, 4):
        for mp_ in range(-3, 4):
            for f_ in range(-3, 4):
                s = abs(d_) + abs(m_) + abs(mp_) + abs(f_)
                if s == 0 or s > 5:
                    continue
                t = (d_, m_, mp_, f_)
                if abs(t[2]) > 3 or abs(t[3]) > 2:
                    continue
                if t >= tuple(-v for v in t):
                    CAND.append(t)
print(f"{len(CAND)} candidate arguments (canonical half-space)")


_BCACHE = {}


def basis(t, trig):
    key = (t, trig)
    b = _BCACHE.get(key)
    if b is None:
        arg = np.radians(t[0] * D + t[1] * M + t[2] * Mp + t[3] * F)
        b = (np.sin if trig == "sin" else np.cos)(arg)
        _BCACHE[key] = b
    return b


def omp(y, K, trigs=("sin",)):
    active = []
    resid = y.copy()
    for _ in range(K):
        best = (0.0, None)
        for t in CAND:
            for trig in trigs:
                b = basis(t, trig)
                a = float(np.dot(resid, b) / np.dot(b, b))
                gain = abs(a) * math.sqrt(float(np.dot(b, b)))
                if gain > best[0]:
                    best = (gain, (t, trig))
        if best[1] is None or best[0] < 1e-4:
            break
        active.append(best[1])
        X = np.column_stack([basis(t, tg) for (t, tg) in active])
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ coef
    X = np.column_stack([basis(t, tg) for (t, tg) in active])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    return active, coef, float(np.std(resid)), float(np.max(np.abs(resid)))


def argstr(t):
    d_, m_, mp_, f_ = t
    parts = []
    for coef, name in ((d_, "D"), (m_, "M"), (mp_, "Mp"), (f_, "F")):
        if coef == 0:
            continue
        parts.append(f"{coef:+d}*{name}")
    return " ".join(parts).lstrip("+")


def show(name, active, coef, rms, mx):
    print(f"\n### {name}:  rms {rms*60:.2f}'  max {mx*60:.1f}'   ({len(active)} terms)")
    rows = []
    for (t, tg), c in zip(active, coef):
        if tg == "sin" and c < 0:  # sin(-x) = -sin(x): safe to fold the sign into the argument
            t = tuple(-v for v in t)
            c = -c
        rows.append((abs(c), tg, t, c))
    rows.sort(key=lambda r: -r[0])
    for a_, tg, t, c in rows:
        print(f"   {c:+12.6f}  {tg}({argstr(t)})")
    return [(t, tg, c) for a_, tg, t, c in rows]


for K in (6, 8, 10, 12, 14, 16, 18, 20, 24):
    _a, _c, r_, m_ = omp(y_lon, K, trigs=("sin",))
    _a2, _c2, r2, m2 = omp(y_lon, K, trigs=("sin", "cos"))
    print(f"longitude K={K:2d}: sin-only rms {r_*60:6.2f}' max {m_*60:6.1f}'   sin+cos rms {r2*60:6.2f}' max {m2*60:6.1f}'")
for K in (4, 6, 8, 10, 12):
    _a, _c, r_, m_ = omp(y_lat, K, trigs=("sin",))
    _a2, _c2, r2, m2 = omp(y_rad, K, trigs=("cos",))
    print(f"lat/rad K={K:2d}: lat rms {r_*60:5.2f}' max {m_*60:5.1f}'   rad rms {r2:6.1f} km max {m2:6.0f} km")
best_l = show("LONGITUDE (deg, added to L')", *omp(y_lon, 20, trigs=("sin",)))
best_b = show("LATITUDE (deg)", *omp(y_lat, 10, trigs=("sin",)))
a_r, c_r, r_r, m_r = omp(y_rad, 12, trigs=("cos",))
print(f"\nradius: rms {r_r:.1f} km max {m_r:.1f} km")
for (t, tg), c in sorted(zip(a_r, c_r), key=lambda z: -abs(z[1])):
    print(f"   {c:+12.3f}  {tg}({argstr(t)})")

print("\n=== paste-ready literals ===")
print("LUNAR_LON_TERMS = [")
for t, tg, c in best_l:
    print(f"    ({t[0]}, {t[1]}, {t[2]}, {t[3]}, {c:+.6f}),  # {tg}({argstr(t)})")
print("]")
print("LUNAR_LAT_TERMS = [")
for t, tg, c in best_b:
    print(f"    ({t[0]}, {t[1]}, {t[2]}, {t[3]}, {c:+.6f}),  # {tg}({argstr(t)})")
print("]")
print("LUNAR_RAD_TERMS = [")
for t, tg, c in sorted([(t, tg, c) for (t, tg), c in zip(a_r, c_r)], key=lambda z: -abs(z[2])):
    print(f"    ({t[0]}, {t[1]}, {t[2]}, {t[3]}, {c:+.3f}),  # {tg}({argstr(t)})")
print("]")

# golden reference fixture for the shipped unit test (offline regression)
sample_idx = np.linspace(0, N - 1, 60).astype(int)
fixture = []
for i in sample_idx:
    jd = float(jds[i])
    m = astro.moon_position(jd)
    a = astral_moon(jd - 2451545.0)
    fixture.append({
        "jd": round(jd, 4),
        "ref_ra": round(math.degrees(a.right_ascension), 5),
        "ref_dec": round(math.degrees(a.declination), 5),
        "ref_dist_km": round(a.distance * EARTH_RADIUS_KM, 1),
        "mine_ra": round(m.ra, 5),
        "mine_dec": round(m.dec, 5),
        "mine_dist_km": round(m.dist_km, 1),
    })
with open("scratch/lunar_reference.json", "w") as fh:
    json.dump(fixture, fh, indent=1)
print(f"\nwrote scratch/lunar_reference.json ({len(fixture)} epochs)")
errs = [math.hypot(astro.wrap180(x["mine_ra"] - x["ref_ra"]) * math.cos(math.radians(x["ref_dec"])), x["mine_dec"] - x["ref_dec"]) for x in fixture]
print(f"current shipped ephemeris angular error: rms {math.sqrt(sum(e*e for e in errs)/len(errs))*60:.2f}' max {max(errs)*60:.2f}'")
