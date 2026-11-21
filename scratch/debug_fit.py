import datetime as dt
import math
import sys

import numpy as np

sys.path.insert(0, "custom_components/fishing")
from model import astronomy as astro
from astral.moon import moon_position as am

N = 2000
t0 = astro.to_jd(dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc))
jds = t0 + np.arange(N) * (365.25 * 20 / N)   # 20-year baseline


def ref_lon_ra(jd):
    a = am(float(jd) - 2451545.0)
    ra, dec = math.degrees(a.right_ascension), math.degrees(a.declination)
    eps = astro.ecliptic_obliquity(float(jd))
    lon = astro.wrap360(astro.atan2_deg(
        math.sin(math.radians(ra)) * math.cos(math.radians(eps)) + math.tan(math.radians(dec)) * math.sin(math.radians(eps)),
        math.cos(math.radians(ra))))
    return lon, ra, dec


els = [astro.lunar_elements(float(jd)) for jd in jds]
Lp = np.array([e[0] for e in els]); D = np.array([e[1] for e in els]); M = np.array([e[2] for e in els]); Mp = np.array([e[3] for e in els]); F = np.array([e[4] for e in els])
rl = [ref_lon_ra(jd) for jd in jds]
ref_l = np.array([x[0] for x in rl])
ref_ra = np.array([x[1] for x in rl]); ref_dec = np.array([x[2] for x in rl])

y = np.array([astro.wrap180(fl - lp) for fl, lp in zip(ref_l, Lp)])
print(f"y = lambda_ref - L'_mine : min {y.min():+.4f} max {y.max():+.4f} std {y.std():.4f} mean {y.mean():+.4f}")
xs = (jds - t0) / 36525.0
sl = np.polyfit(xs, y, 1)
print(f"linear drift in y: slope {sl[0]:+.6f} deg/century, resid after detrend {np.std(y - np.polyval(sl, xs)):.5f}")

# how close is my own full dl (as coded) to y?
def my_dl(jd):
    lp, d, m, mp, f, t = astro.lunar_elements(jd)
    e = 1.0 - 0.002495 * t - 0.00000527 * t * t
    sd, cd = astro.sin_deg, astro.cos_deg
    dl = (6.288774 * sd(mp) + 1.273966 * sd(2*d) + 0.658433 * sd(m) + 0.213618 * sd(2*mp)
          - 0.185116 * sd(2*d - mp) - 0.114332 * sd(2*f) + 0.058793 * sd(2*d - 2*mp)
          - 0.057058 * sd(2*d - mp - m) - 0.055815 * sd(2*d + mp) - 0.046074 * sd(2*d + m)
          + 0.032130 * sd(2*mp - m) + 0.026224 * sd(2*d - 2*f))
    return dl

mine = np.array([my_dl(float(jd)) for jd in jds])
print(f"my coded dl: min {mine.min():.3f} max {mine.max():.3f};  error vs reference: rms {np.std(mine-y)*60:.1f}' max {np.max(np.abs(mine-y))*60:.1f}'")

# candidate canonical set (dedup pairs) from projection, then least squares
cands = []
for a in range(-6,7):
    for b in range(-4,5):
        for c in range(-4,5):
            for d in range(-4,5):
                s = abs(a)+abs(b)+abs(c)+abs(d)
                if s == 0 or s > 6: continue
                t = (a,b,c,d); neg = tuple(-v for v in t)
                if t < neg: cands.append(t)     # canonical: keep lexicographically smaller
print(f"{len(cands)} canonical candidates")
proj = []
for (a,b,c,d) in cands:
    arg = np.radians(a*D + b*M + c*Mp + d*F)
    amp = 2.0*np.dot(y, np.sin(arg))/len(y)
    proj.append((abs(amp), amp, (a,b,c,d)))
proj.sort(key=lambda p: -p[0])
print("top 18 constituents of (lambda_ref - L'):")
for a_, amp, t in proj[:18]:
    if a_ < 1e-5: break
    print(f"   {amp:+10.5f}  sin({t[0]:+d}D {t[1]:+d}M {t[2]:+d}Mp {t[3]:+d}F)")
for K in (6, 8, 10, 12, 14, 16, 18, 20, 24, 30):
    picks = [t for _,_,t in proj[:K]]
    X = np.column_stack([np.sin(np.radians(a*D + b*M + c*Mp + d*F)) for (a,b,c,d) in picks])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    r = y - X@coef
    print(f"   K={K:2d}: rms {np.std(r)*60:7.2f}'  max {np.max(np.abs(r))*60:7.1f}'   coefs: " + ", ".join(f"{c:+.4f}" for c in coef[:4]))
