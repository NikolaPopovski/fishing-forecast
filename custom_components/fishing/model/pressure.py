"""Barometric pressure: the single most discussed and most misread fishing input.

Three separate signals are hiding inside one barometer, and the model keeps them apart:

1. **Anomaly** - is today's pressure high or low *for this spot at this time of year*? Slow,
   sets the background state (stable / unsettled). Measured in sigmas so a spot at 5 m and a
   spot at 400 m are comparable.
2. **Tendency** - dP/dt. This is the trigger: fish sense the fall through their swim bladder
   and move up-water / start feeding ahead of the front. Scored on the *synoptic* tendency,
   i.e. with the spot's measured diurnal pressure tide subtracted (see `series.hour_of_day_cycle`).
3. **Stability** - how much the barometer swung over the last day. A quiet 24 h suits wary
   species; a saw-toothed 24 h (a train of fronts) is scored as churn even if the mean is fine.

Elevation is never folded into the numbers by hand: the series is labelled "station" or
"mslp" and compared only against the matching normal, because reducing a station reading to sea
level with the wrong mean air temperature injects ~1 hPa per 30 m of error - exactly the
magnitude of the signal in (2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import response as rs
from .climatology import Climatology, pressure_to_sea_level
from .series import (
    Sample,
    at,
    clean,
    cycle_tendency,
    extrema,
    hour_of_day_cycle,
    mean,
    tendency as series_tendency,
)

STATION = "station"
MSLP = "mslp"

#: how much synoptic dP/dt (hPa per 3 h) counts as a full "front approaching" signal
TENDENCY_FULL_SCALE = 3.5
#: below this the barometer is simply not moving enough to matter
TENDENCY_DEADBAND = 0.35


@dataclass
class PressureAnalysis:
    when: datetime
    source: str
    value: float | None
    normal: float | None
    anomaly_sigma: float | None
    tendency_3h_raw: float | None
    tendency_3h: float | None          # diurnal-tide corrected
    tendency_6h: float | None
    tendency_24h: float | None
    stability_24h: float | None
    diurnal_corrected: bool
    diurnal_cycle: list[float | None] = field(default_factory=list)
    trend_label: str = "unknown"
    front_index: float | None = None
    front_eta_hours: float | None = None
    wind_shift_deg: float | None = None
    cloud_rise: float | None = None
    temp_jump_c: float | None = None
    score: float = 0.0
    parts: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "when": self.when.isoformat(),
            "source": self.source,
            "value": self.value,
            "normal": self.normal,
            "anomaly_sigma": self.anomaly_sigma,
            "tendency_3h_raw": self.tendency_3h_raw,
            "tendency_3h": self.tendency_3h,
            "tendency_6h": self.tendency_6h,
            "tendency_24h": self.tendency_24h,
            "stability_24h": self.stability_24h,
            "diurnal_corrected": self.diurnal_corrected,
            "trend": self.trend_label,
            "front_index": self.front_index,
            "front_eta_hours": self.front_eta_hours,
            "wind_shift_deg": self.wind_shift_deg,
            "cloud_rise": self.cloud_rise,
            "temp_jump_c": self.temp_jump_c,
            "score": self.score,
            "notes": self.notes,
        }


def analyse(
    series: list[Sample],
    when: datetime,
    *,
    source: str = MSLP,
    elevation_m: float = 0.0,
    clim: Climatology | None = None,
    diurnal_cycle: list[float | None] | None = None,
    mean_temp_c: float | None = None,
    forecast: list[Sample] | None = None,
    wind_dir: list[Sample] | None = None,
    cloud: list[Sample] | None = None,
    air_temp: list[Sample] | None = None,
) -> PressureAnalysis:
    """Full barometric picture at *when*.

    *series* must be a homogeneous series (all station or all MSLP); *forecast* is the future
    part of the same quantity, used only for locating the front.
    """
    series = clean(series)
    forecast = clean(forecast or [])
    both = clean(series + forecast)
    var = "pressure" if source == STATION else "pressure_slp"

    value = at(series, when, max_age_hours=6.0)
    nrm = clim.normal(var, when) if clim else None
    anomaly = clim.anomaly(var, value, when) if clim else None

    raw3 = series_tendency(both, when, 3.0)
    corr3 = None
    if raw3 is not None:
        # the tendency looks BACK over the last 3 h, so the tide to remove is that of the same
        # window (hour-3 -> hour); using the next 3 h would double the artefact instead of cancelling it
        expected = cycle_tendency(diurnal_cycle or [], (when.hour - 3) % 24, 3.0)
        if expected is None:
            corr3 = raw3
        else:
            corr3 = raw3 - expected
    raw6 = series_tendency(both, when, 6.0)
    corr6 = None
    if raw6 is not None and corr3 is not None:
        expected6 = cycle_tendency(diurnal_cycle or [], (when.hour - 6) % 24, 6.0)
        corr6 = raw6 - (expected6 if expected6 is not None else 0.0)
    t24 = series_tendency(both, when, 24.0)
    lo24, hi24 = extrema(both, when, 24.0)
    stability = None if lo24 is None else hi24 - lo24

    notes: list[str] = []
    if corr3 is None:
        notes.append("not enough barometric history for a tendency")
    if diurnal_cycle is None or not diurnal_cycle:
        notes.append("diurnal pressure tide not subtracted (needs >= 10 days of hourly pressure)")
    if nrm is not None and nrm.mode == "flat":
        notes.append("pressure normal is from the rest of the year, not this season")

    trend = "unknown"
    if corr3 is not None:
        if corr3 <= -1.8:
            trend = "falling fast"
        elif corr3 <= -TENDENCY_DEADBAND:
            trend = "falling"
        elif corr3 >= 1.8:
            trend = "rising fast"
        elif corr3 >= TENDENCY_DEADBAND:
            trend = "rising"
        else:
            trend = "steady"

    # --- the front: trough passage + the corroboration real forecasters look for ----------
    front_index, front_eta = _front(both, when, diurnal_cycle or [], corr3, forecast)
    wind_shift = _wind_shift(clean(wind_dir or []), when)
    cloud_rise = _cloud_rise(clean(cloud or []), when)
    temp_jump = _temp_jump(clean(air_temp or []), when)

    corroborations = 0.0
    if wind_shift is not None:
        corroborations += min(1.0, abs(wind_shift) / 60.0) * 0.4
    if cloud_rise is not None:
        corroborations += min(1.0, max(0.0, cloud_rise) / 0.45) * 0.35
    if temp_jump is not None:
        corroborations += min(1.0, abs(temp_jump) / 4.0) * 0.25
    # evidence that was *measured and flat* is still evidence: a swing the wind, cloud and
    # temperature all ignore is discounted. Only a complete lack of those series leaves it as is.
    have_evidence = wind_shift is not None or cloud_rise is not None or temp_jump is not None
    if front_index is not None and have_evidence:
        front_index = min(1.0, front_index * (0.55 + 0.45 * min(1.0, corroborations / 0.9)))
        if corroborations < 0.2:
            notes.append("pressure swing not backed by wind veer / cloud thickening - discounted")

    parts: dict[str, float] = {}
    if corr3 is not None:
        parts["tendency"] = rs.PRESSURE_TENDENCY_3H(corr3)
    if anomaly is not None:
        parts["anomaly"] = rs.PRESSURE_ANOMALY(anomaly)
    if stability is not None:
        parts["stability"] = rs.PRESSURE_STABILITY_24H(stability)
    score = 0.0
    if parts:
        inner = {"tendency": 0.45, "anomaly": 0.30, "stability": 0.25}
        used = sum(inner[k] for k in parts)
        score = sum(inner[k] * v for k, v in parts.items()) / used

    return PressureAnalysis(
        when=when,
        source=source,
        value=value,
        normal=None if nrm is None else nrm.value,
        anomaly_sigma=anomaly,
        tendency_3h_raw=raw3,
        tendency_3h=corr3,
        tendency_6h=corr6,
        tendency_24h=t24,
        stability_24h=stability,
        diurnal_corrected=bool(diurnal_cycle),
        diurnal_cycle=list(diurnal_cycle or []),
        trend_label=trend,
        front_index=front_index,
        front_eta_hours=front_eta,
        wind_shift_deg=wind_shift,
        cloud_rise=cloud_rise,
        temp_jump_c=temp_jump,
        score=score,
        parts=parts,
        notes=notes,
    )


def _front(
    series: list[Sample],
    when: datetime,
    diurnal_cycle: list[float | None],
    current_tendency: float | None,
    forecast: list[Sample],
) -> tuple[float | None, float | None]:
    """Strength and ETA of the next frontal passage inside the next 48 h.

    A front in a surface pressure series looks like: falling -> trough (minimum) -> rising.
    So the passage time is where the *corrected* tendency flips from negative to positive after
    a real drop. The strength blends how deep that drop is with how sharply it reverses; if the
    reversal never happens the front is only inferred from the present tendency, and the score
    is capped because "it has been falling" is not the same as "it is arriving".
    """
    if not series:
        return None, None
    horizon = when + timedelta(hours=48)
    future = [(t, v) for t, v in series if when <= t <= horizon]
    if len(future) < 4:
        cap = 0.0 if current_tendency is None else min(0.55, abs(current_tendency) / TENDENCY_FULL_SCALE)
        return (cap if cap > 0.02 else None), None

    step = 3.0
    samples: list[tuple[datetime, float]] = []
    cursor = when
    while cursor <= horizon:
        t3 = series_tendency(series, cursor, step)
        if t3 is not None:
            expected = cycle_tendency(diurnal_cycle, (cursor.hour - int(step)) % 24, step) or 0.0
            samples.append((cursor, t3 - expected))
        cursor += timedelta(hours=int(step))
    if not samples:
        return None, None

    drop = 0.0
    lo, hi = extrema(series, when, 24.0)
    if lo is not None:
        drop = hi - lo
    passage = None
    for (t_prev, s_prev), (t_now, s_now) in zip(samples, samples[1:]):
        if s_prev <= -0.9 and s_now >= -0.2:
            passage = t_now
            break
    if passage is not None:
        eta = (passage - when).total_seconds() / 3600.0
        depth = min(1.0, max(0.0, drop) / 8.0)
        urgency = 1.0 if eta <= 6.0 else max(0.25, 1.0 - (eta - 6.0) / 30.0)
        return min(1.0, 0.35 + 0.65 * depth) * urgency, eta

    # no reversal in the window: a deep ongoing fall is still a live front, just not a located one
    falling = min(1.0, max(0.0, -(current_tendency or 0.0)) / TENDENCY_FULL_SCALE)
    depth = min(1.0, max(0.0, drop) / 8.0)
    index = 0.45 * falling + 0.35 * depth
    return (index if index > 0.02 else None), None


def _wind_shift(wind_dir: list[Sample], when: datetime) -> float | None:
    """Signed wind direction change over ~9 h - the veer that marks a warm/near front."""
    if len(wind_dir) < 3:
        return None
    now = at(wind_dir, when, max_age_hours=4.0)
    then = at(wind_dir, when - timedelta(hours=9), max_age_hours=5.0)
    if now is None or then is None:
        return None
    delta = ((now - then + 180.0) % 360.0) - 180.0  # -180..180, positive = veering (clockwise)
    return delta


def _cloud_rise(cloud: list[Sample], when: datetime) -> float | None:
    """Cloud amount change over 12 h: thickening lower/mid sheet ahead of a warm front."""
    if len(cloud) < 4:
        return None
    recent = mean(cloud, when, 4.0)
    earlier = mean(cloud, when - timedelta(hours=10), 6.0)
    if recent is None or earlier is None:
        return None
    return recent - earlier


def _temp_jump(air_temp: list[Sample], when: datetime) -> float | None:
    """Air temperature change over 6 h: the airmass change a front leaves behind.

    Corrected for the daily cycle. Raw "now minus six hours ago" is about -5 degC every evening
    after sunset and +5 every morning, which would read as a cold front twice a day. The same
    six-hour window one day earlier shows what the daily cycle alone does at this time of day, so
    the difference is the part that is weather. Without that previous day the two cannot be
    separated, and the honest answer is None.
    """
    if len(air_temp) < 3:
        return None
    now = at(air_temp, when, max_age_hours=3.0)
    then = at(air_temp, when - timedelta(hours=6), max_age_hours=4.0)
    now_prev = at(air_temp, when - timedelta(hours=24), max_age_hours=3.0)
    then_prev = at(air_temp, when - timedelta(hours=30), max_age_hours=4.0)
    if None in (now, then, now_prev, then_prev):
        return None
    return (now - then) - (now_prev - then_prev)


def build_diurnal_cycle(hourly_pressure: list[Sample], min_hours: int = 12) -> list[float | None]:
    """Estimate the spot's diurnal pressure tide from 10+ days of hourly readings."""
    cycle = hour_of_day_cycle(hourly_pressure)
    if sum(1 for v in cycle if v is not None) < min_hours:
        return []
    filled = _fill_missing(cycle)
    base = sum(filled) / len(filled)
    return [v - base for v in filled]


def _fill_missing(cycle: list[float | None]) -> list[float]:
    vals = [v for v in cycle if v is not None]
    if not vals:
        return [0.0] * len(cycle)
    fallback = sum(vals) / len(vals)
    out = list(cycle)
    for i, v in enumerate(out):
        if v is None:
            prev = next((out[(i - k) % len(out)] for k in range(1, len(out)) if out[(i - k) % len(out)] is not None), fallback)
            nxt = next((out[(i + k) % len(out)] for k in range(1, len(out)) if out[(i + k) % len(out)] is not None), fallback)
            out[i] = (prev + nxt) / 2.0 if prev is not None and nxt is not None else fallback
    return [float(v) for v in out]


def sensor_offset(
    sensor_station: list[Sample],
    model_mslp: list[Sample],
    elevation_m: float,
    mean_temp_c: float,
    window_hours: float = 48.0,
) -> tuple[float | None, float | None]:
    """Bias between a local barometer and the forecast's pressure field (the "second barometer").

    The forecast's MSLP reduced to the spot's elevation should agree with the local station
    reading to well within 1 hPa in the same air column; a persistent offset is the local
    model's bias (or a barometer needing calibration) and is worth storing, because every
    pressure *anomaly* the model computes inherits it.
    """
    sensor = clean(sensor_station)
    model = clean(model_mslp)
    if len(sensor) < 6 or len(model) < 6:
        return None, None
    diffs: list[float] = []
    for when, station in sensor:
        mslp = at(model, when, max_age_hours=2.0)
        if mslp is None:
            continue
        diffs.append(station - sea_level_to_station_local(mslp, elevation_m, mean_temp_c))
    if len(diffs) < 6:
        return None, None
    avg = sum(diffs) / len(diffs)
    spread = (sum((d - avg) ** 2 for d in diffs) / (len(diffs) - 1)) ** 0.5
    return avg, spread


def sea_level_to_station_local(mslp: float, elevation_m: float, mean_temp_c: float) -> float:
    from .climatology import sea_level_to_station

    return sea_level_to_station(mslp, elevation_m, mean_temp_c)


def expected_station_from_mslp(mslp: float, elevation_m: float, mean_temp_c: float) -> float:
    """Convenience for the integration: show both numbers in the diagnostics sensor."""
    return sea_level_to_station_local(mslp, elevation_m, mean_temp_c)


def mslp_from_station(station: float, elevation_m: float, mean_temp_c: float) -> float:
    return pressure_to_sea_level(station, elevation_m, mean_temp_c)
