"""Derive abridged lunar series coefficients by long-baseline frequency projection.

Reference: the full Meeus ch.33 series as implemented by astral 3.2 (scratch venv only).
Long baseline (1950-2050) makes the constituent frequencies separable, so amplitudes come
out clean instead of the ill-conditioned mush a short window gives.
"""
import datetime as dt
import math
import sys

import numpy as np

sys.path.insert(0, "custom_components/fishing")
from model import astronomy as astro
from astral.moon import moon_position as astral_moon

N = 30000
t0 = astro.to_jd(dt.datetime(1950, 1, 1, tzinfo=dt.timezone.utc))
jds = t0 + np.arange(N) * (36500.0 / N)

els = [astro.lunar_elements(float(jd)) for jd in jds]
Lp = np.array([e[0] for e in els])
D = np.array([e[1] for e in els])
M = np.array([e[2] for e in els])
Mp = np.array([e[3] for e in els])
F = np.array([e[4] for e in els])

ref_lon, ref_lat, ref_dist = [], [], []
for jd in jds:
    a = astral_moon(float(jd) - 2451545.0)
    ra, dec = math.degrees(a.right_ascension), math.degrees(a.declination)
    eps = astro.ecliptic_obliquity(float(jd))
    lon = astro.wrap360(
        astro.atan2_deg(
            math.sin(math.radians(ra)) * math.cos(math.radians(eps))
            + math.tan(math.radians(dec)) * math.sin(math.radians(eps)),
            math.cos(math.radians(ra)),
        )
    )
    lat = astro.asin_deg(
        math.sin(math.radians(dec)) * math.cos(math.radians(eps))
        - math.cos(math.radians(dec)) * math.sin(math.radians(eps)) * math.sin(math.radians(ra))
    )
    ref_lon.append(lon)
    ref_lat.append(lat)
    ref_dist.append(a.distance * 6378.137)

y_lon = np.unwrap(np.radians(np.array([astro.wrap180(fl - lp) for fl, lp in zip(ref_lon, Lp)])))
y_lat = np.radians(np.array(ref_lat))
y_rad = np.array(ref_dist) - astro.MEAN_MOON_DISTANCE


def terms(max_abs=6):
    out = []
    for a in range(-max_abs, max_abs + 1):
        for b in range(-4, 5):
            for c in range(-4, 5):
                for d in range(-4, 5):
                    s = abs(a) + abs(b) + abs(c) + abs(d)
                    if s == 0 or s > 6:
                        continue
                    out.append((a, b, c, d))
    return out


ALL = terms()
print(f"{len(ALL)} candidate arguments")


def project(y_rad_series, want_sin=True):
    scores = []
    fn = np.sin if want_sin else np.cos
    for (a, b, c, d) in ALL:
        arg = np.radians(a * D + b * M + c * Mp + d * F)
        basis = fn(arg)
        amp = 2.0 * np.dot(y_rad_series, basis) / len(y_rad_series)
        scores.append((abs(amp), amp, (a, b, c, d)))
    scores.sort(key=lambda s: -s[0])
    return scores


def refine(y_deg, picks):
    X = np.column_stack([np.sin(np.radians(a * D + b * M + c * Mp + d * F)) for (a, b, c, d) in picks])
    coef, *_ = np.linalg.lstsq(X, y_deg, rcond=None)
    res = y_deg - X @ coef
    return coef, np.std(res), np.max(np.abs(res))


print("\n=== LONGITUDE: top constituents (degrees) ===")
scores = project(y_lon)
for amp_abs, amp, t in scores[:26]:
    if amp_abs < 1e-5:
        break
    a, b, c, d = t
    print(f"   sin({a:+d}D {b:+d}M {c:+d}Mp {d:+d}F)  {math.degrees(amp):+10.5f}")

top = [t for _, _, t in scores[:22]]
coef, rms, mx = refine(np.degrees(y_lon), top)
print(f"\nrefit({len(top)} terms): rms {math.degrees(rms)*60:.2f}'  max {math.degrees(mx)*60:.1f}'")
for cc, t in sorted(zip(coef, top), key=lambda z: -abs(z[0])):
    a, b, c, d = t
    print(f"   {cc:+10.5f}  sin({a:+d}D {b:+d}M {c:+d}Mp {d:+d}F)")

for K in (6, 8, 10, 12, 14, 16, 18, 22):
    _, rms, mx = refine(np.degrees(y_lon), top[:K])
    print(f"   K={K:2d}: rms {math.degrees(rms)*60:6.2f}'  max {math.degrees(mx)*60:7.1f}'")

print("\n=== LATITUDE: top constituents (degrees) ===")
sb = project(y_lat)
for amp_abs, amp, t in sb[:14]:
    if amp_abs < 1e-6:
        break
    a, b, c, d = t
    print(f"   sin({a:+d}D {b:+d}M {c:+d}Mp {d:+d}F)  {math.degrees(amp):+10.5f}")
topb = [t for _, _, t in sb[:10]]
coefb, rmsb, mxb = refine(np.degrees(y_lat), topb)
print(f"refit({len(topb)}): rms {math.degrees(rmsb)*60:.3f}' max {math.degrees(mxb)*60:.2f}'")
for cc, t in sorted(zip(coefb, topb), key=lambda z: -abs(z[0])):
    a, b, c, d = t
    print(f"   {cc:+10.5f}  sin({a:+d}D {b:+d}M {c:+d}Mp {d:+d}F)")

print("\n=== RADIUS: top constituents (km, cos basis) ===")
scores_r = project(y_rad / 1000.0, want_sin=False)  # unit: 1000 km
for amp_abs, amp, t in scores_r[:14]:
    if amp_abs < 0.005:
        break
    a, b, c, d = t
    print(f"   cos({a:+d}D {b:+d}M {c:+d}Mp {d:+d}F)  {1000*math.degrees(amp):+12.3f} km")
topr = [t for _, _, t in scores_r[:10]]
Xr = np.column_stack([np.cos(np.radians(a * D + b * M + c * Mp + d * F)) for (a, b, c, d) in topr])
coefr, *_ = np.linalg.lstsq(Xr, y_rad, rcond=None)
resr = y_rad - Xr @ coefr
print(f"refit({len(topr)}): rms {np.std(resr):.1f} km  max {np.max(np.abs(resr)):.0f} km")
for cc, t in sorted(zip(coefr, topr), key=lambda z: -abs(z[0])):
    a, b, c, d = t
    print(f"   {cc:+12.3f}  cos({a:+d}D {b:+d}M {c:+d}Mp {d:+d}F)")
