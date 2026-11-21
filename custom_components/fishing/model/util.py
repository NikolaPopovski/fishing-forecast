"""Small maths helpers shared by the model.

Nothing in here knows about Home Assistant, weather APIs or fishing.  Kept separate
because these primitives define the shape of the scoring curves and it is worth being
able to unit test them in isolation.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

TAU = math.tau


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Clamp *value* into [low, high]."""
    return low if value < low else high if value > high else value


def norm(value: float, low: float, high: float) -> float:
    """Linear 0..1 mapping of *value* between *low* and *high* (reversible if low>high)."""
    if high == low:
        return 0.0
    return clamp((value - low) / (high - low))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def gauss(x: float, mu: float, sigma: float, floor: float = 0.0) -> float:
    """Gaussian bump centred on *mu*.

    *sigma* is the standard deviation; values below it are considered "close".
    *floor* is the asymptotic value far from the bump, which matters because most
    fishing factors are "not terrible" rather than "terrible" when they are off-target.
    """
    if sigma <= 0:
        return 1.0 if abs(x - mu) < 1e-9 else floor
    t = (x - mu) / sigma
    return clamp(floor + (1.0 - floor) * math.exp(-0.5 * t * t))


def band(
    x: float,
    hard_lo: float,
    soft_lo: float,
    soft_hi: float,
    hard_hi: float,
    floor: float = 0.0,
) -> float:
    """Trapezoidal membership: 1 inside [soft_lo, soft_hi], 0 outside [hard_lo, hard_hi].

    This is the workhorse for "species comfort ranges" — a fish is fine inside a range,
    dies outside another range, and in between it degrades linearly.
    """
    if x <= hard_lo or x >= hard_hi:
        return floor
    if x < soft_lo:
        return lerp(floor, 1.0, norm(x, hard_lo, soft_lo))
    if x > soft_hi:
        return lerp(1.0, floor, norm(x, soft_hi, hard_hi))
    return 1.0


def sigmoid(x: float, centre: float, width: float) -> float:
    """Smooth step.  >0 rises, and *width* sets how gradual the transition is."""
    if width == 0:
        return 1.0 if x > centre else 0.0
    return clamp(1.0 / (1.0 + math.exp(-(x - centre) / width)))


def wrap180(angle: float) -> float:
    """Wrap degrees to (-180, 180]."""
    a = math.fmod(angle, 360.0)
    if a <= -180.0:
        a += 360.0
    elif a > 180.0:
        a -= 360.0
    return a


def wrap360(angle: float) -> float:
    a = math.fmod(angle, 360.0)
    return a + 360.0 if a < 0 else a


def circular_mean(angles_deg: Sequence[float]) -> float | None:
    """Vector mean direction.  A plain arithmetic mean of 350 and 10 gives 180 - wrong."""
    sx = sy = 0.0
    n = 0
    for a in angles_deg:
        if a is None:
            continue
        r = math.radians(a)
        sx += math.cos(r)
        sy += math.sin(r)
        n += 1
    if n == 0:
        return None
    return wrap360(math.degrees(math.atan2(sy, sx)))


def circular_std(angles_deg: Sequence[float]) -> float | None:
    """Spread of a set of directions (0 = perfectly steady wind, 180 = chaotic)."""
    sx = sy = 0.0
    n = 0
    for a in angles_deg:
        if a is None:
            continue
        r = math.radians(a)
        sx += math.cos(r)
        sy += math.sin(r)
        n += 1
    if n < 2:
        return None
    r = math.hypot(sx, sy) / n
    if r >= 1.0:
        return 0.0
    return math.degrees(math.acos(clamp(r, -1.0, 1.0)))


def mean(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def median(values: Sequence[float | None]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def stdev(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    return math.sqrt(sum((v - mu) ** 2 for v in vals) / (len(vals) - 1))


def slope_per_hour(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Least-squares slope (units of y per unit of x).  Robust enough for pressure/water trends."""
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pts) < 3:
        return None
    n = len(pts)
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    sxx = sum(p[0] * p[0] for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-12:
        return None
    return (n * sxy - sx * sy) / denom


def interp_xy(x: float, xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Piecewise-linear interpolation with clamped ends (xs must be ascending)."""
    pts = [(a, b) for a, b in zip(xs, ys) if a is not None and b is not None]
    if not pts:
        return None
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    lo, hi = 0, len(pts) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if pts[mid][0] <= x:
            lo = mid
        else:
            hi = mid
    x0, y0 = pts[lo]
    x1, y1 = pts[hi]
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def moving_average(values: Sequence[float | None], window: int) -> list[float | None]:
    """Centred moving average that ignores holes instead of propagating them."""
    out: list[float | None] = []
    n = len(values)
    half = max(1, window // 2)
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out.append(mean(values[lo:hi]))
    return out


def gaussian_smooth(values: Sequence[float | None], sigma: float) -> list[float | None]:
    """Circular-safe smoothing used for climatology (day-of-year is a ring, not a line)."""
    n = len(values)
    if n == 0 or sigma <= 0:
        return list(values)
    radius = max(1, int(sigma * 3))
    kernel = [math.exp(-0.5 * (k / sigma) ** 2) for k in range(-radius, radius + 1)]
    out: list[float | None] = []
    for i in range(n):
        acc = 0.0
        wsum = 0.0
        for k, w in zip(range(-radius, radius + 1), kernel):
            j = (i + k) % n  # wrap: Dec 31 is adjacent to Jan 1
            v = values[j]
            if v is not None:
                acc += v * w
                wsum += w
        out.append(acc / wsum if wsum else None)
    return out


def day_of_year(dt) -> int:
    return dt.timetuple().tm_yday


def season_signatures(doy_north: Sequence[int]) -> dict[int, float]:
    """Placeholder kept for symmetry with tests; season curves come from species.py."""
    return {doy: 0.0 for doy in doy_north}
