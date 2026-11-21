"""Small, boring time-series helpers shared by the factor modules.

Weather APIs hand back lists of (timestamp, value) with gaps, mixed timezones and uneven
spacing. Every factor module would otherwise reinvent the same tolerant sampling - and each
reinvention would disagree about what "the pressure 3 hours ago" means when the sample is
missing. Deliberately naive data structures (lists of tuples) keep the model portable: the HA
layer converts its own states into these.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta

Sample = tuple[datetime, float]


def _when(sample: Sample) -> datetime:
    return sample[0]


def _window(series: list[Sample], lo: datetime, hi: datetime, *, open_lo: bool = False) -> list[float]:
    """Values with lo <= t <= hi (or lo < t <= hi), by binary search on a *sorted* series.

    The model asks the same few window questions thousands of times per run (every step of the
    water-temperature integration, every hour of a 48 h curve); a linear scan per question made
    that quadratic in the history length.
    """
    start = (bisect_right if open_lo else bisect_left)(series, lo, key=_when)
    stop = bisect_right(series, hi, key=_when)
    return [v for _, v in series[start:stop]]


def clean(series: list[Sample]) -> list[Sample]:
    """Drop Nones/NaNs, coerce to float, sort, and de-duplicate identical timestamps."""
    out: dict[datetime, float] = {}
    for when, value in series or ():
        if when is None or value is None:
            continue
        try:
            val = float(value)
        except (TypeError, ValueError):
            continue
        if val != val:  # NaN
            continue
        out[when] = val
    return sorted(out.items(), key=lambda kv: kv[0])


def at(series: list[Sample], when: datetime, max_age_hours: float = 6.0) -> float | None:
    """Value at *when*, falling back to the nearest sample inside *max_age_hours*.

    A nearest-sample fallback (rather than an interpolation across a big gap) is deliberate for
    sensor overrides: if your barometer stopped reporting 9 h ago, the model should say "last
    known", not invent a trend line through the hole.
    """
    if not series:
        return None
    i = bisect_left(series, when, key=_when)
    best: tuple[float, Sample] | None = None
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(series):
            age = abs((series[j][0] - when).total_seconds()) / 3600.0
            if age <= max_age_hours and (best is None or age < best[0]):
                best = (age, series[j])
    return None if best is None else best[1][1]


def value_and_slope(series: list[Sample], when: datetime, window_hours: float) -> tuple[float | None, float | None]:
    """Least-squares slope (units per hour) over a window ending at *when*.

    Regression instead of "last minus first": a single noisy hourly sample or a gap should not
    turn into a fake 5 hPa swing. Returns (value_at_when, slope) with either as None when the
    window is too empty to say.
    """
    if not series or window_hours <= 0:
        return None, None
    lo = when - timedelta(hours=window_hours)
    win = [(t, v) for t, v in series if lo <= t <= when]
    if len(win) < 3:
        return at(series, when, max_age_hours=window_hours), None
    n = len(win)
    base = win[0][0]
    xs = [(t - base).total_seconds() / 3600.0 for t, _ in win]
    ys = [v for _, v in win]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return my, None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    # value at `when` from the fit, but never further from the last sample than the fit says
    value = my + slope * ((when - base).total_seconds() / 3600.0 - mx)
    last = at(series, when, max_age_hours=window_hours)
    if last is not None and abs(value - last) > 2.5 * abs(slope or 0.0) + 0.01:
        value = last
    return value, slope


def tendency(series: list[Sample], when: datetime, hours: float) -> float | None:
    _, slope = value_and_slope(series, when, hours)
    if slope is None:
        return None
    return slope * hours


def extrema(series: list[Sample], when: datetime, hours: float) -> tuple[float | None, float | None]:
    win = _window(series, when - timedelta(hours=hours), when)
    if not win:
        return None, None
    return min(win), max(win)


def mean(series: list[Sample], when: datetime, hours: float) -> float | None:
    win = _window(series, when - timedelta(hours=hours), when)
    if not win:
        return None
    return sum(win) / len(win)


def sum_over(series: list[Sample], when: datetime, hours: float) -> float | None:
    """For rain-like series where the values are per-period totals.

    Negative values are clamped to zero: a gauge or feed glitch must never subtract rain that fell.
    """
    win = _window(series, when - timedelta(hours=hours), when, open_lo=True)
    if not win:
        return None
    return sum(max(0.0, v) for v in win)


def decayed_sum(series: list[Sample], when: datetime, days: float, daily_decay: float = 0.82) -> float | None:
    """Antecedent wetness: rain from earlier days counts less, exponentially.

    A stream does not remember a storm a fortnight ago the way it remembers yesterday's, and
    `daily_decay` is the knob for that memory (soil saturation, groundwater lag).
    """
    total = 0.0
    seen = False
    for t, v in series:
        age_days = (when - t).total_seconds() / 86400.0
        if 0 <= age_days <= days:
            total += v * (daily_decay ** age_days)
            seen = True
    return total if seen else None


def hour_of_day_cycle(series: list[Sample], hours: int = 24) -> list[float | None]:
    """Mean value for each hour of day: the spot's own diurnal cycle.

    This is what makes a *corrected* pressure tendency possible. The solar semi-diurnal tide
    (S2) moves surface pressure by 1-1.5 hPa over a day at mid latitudes, peaking near 10:00
    and 22:00 local; a raw 3 h slope taken at 03:00 therefore reports a "falling barometer"
    that has nothing to do with a front. Subtracting the spot's measured diurnal tendency is
    what weather services do; the model needs the same correction or it will invent fronts.
    """
    buckets: list[list[float]] = [[] for _ in range(hours)]
    for when, value in series:
        buckets[when.hour].append(value)
    return [None if not b else sum(b) / len(b) for b in buckets]


def cycle_tendency(cycle: list[float | None], hour: int, hours_window: float) -> float | None:
    """Expected dP/dt (per `hours_window`) implied by a diurnal cycle."""
    if not cycle or len(cycle) < 2:
        return None
    n = len(cycle)

    def value_at(hour_fraction: float) -> float | None:
        i = int(hour_fraction) % n
        j = (i + 1) % n
        a, b = cycle[i], cycle[j]
        if a is None or b is None:
            return None
        frac = hour_fraction - int(hour_fraction)
        return a + (b - a) * frac

    start = value_at(hour)
    end = value_at(hour + hours_window)
    if start is None or end is None:
        return None
    return end - start


def daily_from_hourly(hourly: list[Sample]) -> list[tuple[datetime, float]]:
    """Aggregate hourly samples into daily means (the shape the climatology store wants)."""
    buckets: dict[tuple[int, ...], list[float]] = {}
    for when, value in hourly:
        key = (when.year, when.month, when.day)
        buckets.setdefault(key, []).append(value)
    out = []
    for (y, m, d), vals in sorted(buckets.items()):
        out.append((datetime(y, m, d), sum(vals) / len(vals)))
    return out


def resample(series: list[Sample], step_minutes: int) -> list[Sample]:
    """Linear resampling onto a regular grid, gaps bridged only if bounded (<= 2 missing steps)."""
    if not series:
        return []
    step = timedelta(minutes=step_minutes)
    start = series[0][0].replace(second=0, microsecond=0)
    end = series[-1][0]
    out: list[Sample] = []
    cursor = start
    i = 0
    while cursor <= end:
        while i + 1 < len(series) and series[i + 1][0] < cursor:
            i += 1
        if i + 1 >= len(series):
            break
        t0, v0 = series[i]
        t1, v1 = series[i + 1]
        if (t1 - t0) > step * 3:  # a real gap: do not bridge it with fiction
            i += 1          # step past the gap, or the same gap is found again forever
            cursor = t1
            continue
        span = (t1 - t0).total_seconds() or 1.0
        frac = (cursor - t0).total_seconds() / span
        out.append((cursor, v0 + (v1 - v0) * min(1.0, max(0.0, frac))))
        cursor += step
    return out


@dataclass
class Window:
    start: datetime
    end: datetime

    @property
    def mid(self) -> datetime:
        return self.start + (self.end - self.start) / 2

    def contains(self, when: datetime) -> bool:
        return self.start <= when <= self.end

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0
