"""The weighted factor engine: from one spot's data to "how good is the fishing right now?".

Everything below this module answers a narrow physical question (how is the barometer moving,
what is the water temperature, how bright is the sky). This module is where those answers are
turned into one number per species - and, just as important, where it admits what it does not
know.

Three habits run through it, all of them learned the hard way by anglers:

* **No score is invented.** A factor that cannot be computed is dropped, the remaining weights are
  renormalised, and the loss is reported as lower `confidence` plus a sentence in `notes`. A
  factor that simply does not *apply* (moonlight at noon, tide in a landlocked lake) is removed
  without penalty, because an honest "not applicable" is not missing data.
* **The displayed score is shrunk toward "average" as confidence falls.** A forecast built on a
  third of the usual inputs should not look as certain as one built on all of them.
* **Light and timing come from the sun's altitude**, never the clock hour, so the same engine is
  right in the tropics and at 64 degrees north.

Datetime contract: the public functions take a **naive local wall-clock** `when` (the same
convention as every series in the model) and also accept an aware one, which is converted. The
astronomy layer returns aware-UTC datetimes, so this module always hands it an aware version of
`when` and converts what comes back; nothing downstream ever compares naive with aware.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import astronomy as astro
from . import hydro, pressure, solunar
from . import response as rs
from . import species as sp
from . import water_temp as wt
from .context import DayAstronomy, SpotContext
from .series import Sample, at

#: hourly weather is trusted this far from the requested instant (hours)
OBS_MAX_AGE_H = 3.0
#: how fast one hour of equilibrium-tide level change reaches a "full run" (unit amplitude, 12.42 h)
_FULL_TIDE_RUN_PER_H = 0.50

BANDS = (
    (-0.50, "terrible"),
    (-0.20, "poor"),
    (0.10, "fair"),
    (0.40, "good"),
)


def band(raw: float) -> str:
    """Words for a -1..+1 score: the card shows the word beside the number."""
    for limit, word in BANDS:
        if raw < limit:
            return word
    return "excellent"


# --------------------------------------------------------------------------------------
# result types
# --------------------------------------------------------------------------------------


@dataclass
class FactorScore:
    name: str                   # one of species.FACTORS
    label: str                  # human: "Barometer", "Water temperature", "Light window"
    value: float | None         # the number behind the score (None when unavailable)
    unit: str                   # 'hPa/3h', 'degC', 'm/s', 'sigma', '' for indices
    score: float                # -1..1
    weight: float               # share of the *used* weight (0 for an unavailable factor)
    contribution: float         # weight * score
    why: str                    # one sentence in plain language, with the number in it
    source: str                 # 'open-meteo' | 'sensor' | 'sst' | 'model' | 'unavailable'

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "value": None if self.value is None else round(self.value, 3),
            "unit": self.unit,
            "score": round(self.score, 3),
            "weight": round(self.weight, 3),
            "contribution": round(self.contribution, 4),
            "why": self.why,
            "source": self.source,
        }


@dataclass
class Prepared:
    """Everything species-independent about one instant, computed once.

    Scoring 22 species, or the same instant under several what-ifs, then costs almost nothing
    beyond the cheap response curves.
    """

    when: datetime                              # naive local wall clock
    now: datetime                               # the instant the sensors refer to (naive local)
    aware: datetime                             # `when` in the spot's timezone, for astronomy
    sun_altitude: float
    moonshine: float
    day: DayAstronomy
    solunar: solunar.SolunarState
    tide: astro.TideState | None
    pressure: pressure.PressureAnalysis
    hydro: hydro.HydroState
    water_series: list[Sample]                  # level-corrected series used for trend/anomaly
    water_level: float | None
    water_source: str
    obs: dict[str, float | None] = field(default_factory=dict)


@dataclass
class Evaluation:
    when: datetime
    species_id: str
    raw: float                  # -1..1 after the season factor, before confidence shrinkage
    score: float                # 0..10 shown, confidence-shrunk
    band: str                   # terrible | poor | fair | good | excellent
    confidence: float           # 0..1
    coverage: float             # fraction of the (applicable) profile weight actually present
    factors: list[FactorScore]
    notes: list[str]
    light: astro.DayLight
    solunar: solunar.SolunarState | None
    sun_altitude: float
    moonshine: float
    season_factor: float = 1.0
    not_applicable: list[str] = field(default_factory=list)
    water: wt.WaterState | None = None
    pressure: pressure.PressureAnalysis | None = None
    hydro: hydro.HydroState | None = None
    obs: dict[str, float | None] = field(default_factory=dict)

    def factor(self, name: str) -> FactorScore | None:
        return next((f for f in self.factors if f.name == name), None)

    def as_dict(self) -> dict:
        return {
            "when": self.when.isoformat(),
            "species": self.species_id,
            "score": round(self.score, 2),
            "raw": round(self.raw, 3),
            "band": self.band,
            "confidence": round(self.confidence, 3),
            "coverage": round(self.coverage, 3),
            "season_factor": self.season_factor,
            "sun_altitude": round(self.sun_altitude, 1),
            "moonshine": round(self.moonshine, 3),
            "factors": [f.as_dict() for f in self.factors],
            "not_applicable": self.not_applicable,
            "notes": self.notes,
        }


@dataclass
class Span:
    """A contiguous run of good hours, for "best window today"."""

    start: datetime
    end: datetime
    peak_at: datetime
    peak_score: float
    mean_score: float

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "peak_at": self.peak_at.isoformat(),
            "peak_score": round(self.peak_score, 2),
            "mean_score": round(self.mean_score, 2),
        }


# --------------------------------------------------------------------------------------
# time helpers: the one place naive-local and aware are reconciled
# --------------------------------------------------------------------------------------


def to_local(ctx: SpotContext, when: datetime) -> datetime:
    """Naive spot-local wall clock, whatever the caller passed."""
    if when.tzinfo is None:
        return when
    return when.astimezone(ctx.tz).replace(tzinfo=None)


def to_aware(ctx: SpotContext, when: datetime) -> datetime:
    """The spot-local wall clock `when` as an aware datetime (what astronomy needs)."""
    return to_local(ctx, when).replace(tzinfo=ctx.tz)


def local_event(ctx: SpotContext, event: datetime | None) -> datetime | None:
    """An aware astronomy event as a naive local datetime comparable with the series."""
    return None if event is None else to_local(ctx, event)


# --------------------------------------------------------------------------------------
# preparation: the sub-analyses, once per instant
# --------------------------------------------------------------------------------------

_WATER_MODEL_KEY = "scoring.water_model"


def _water_model_series(ctx: SpotContext) -> list[Sample]:
    """The heat-budget water series for the whole air history, built once per refresh."""
    cached = ctx.memo.get(_WATER_MODEL_KEY)
    if cached is not None:
        return cached  # type: ignore[return-value]
    series, _ = wt.model_series(
        ctx.get("air_temp"),
        ctx.water_body,
        ctx.lat,
        ctx.lon,
        cloud=ctx.get("cloud"),
        humidity=ctx.get("humidity"),
        wind_speed=ctx.get("wind_speed"),
        dewpoint=ctx.get("dewpoint"),
        shortwave=ctx.get("shortwave"),
        elevation_m=ctx.elevation_m,
    )
    ctx.memo[_WATER_MODEL_KEY] = series
    return series


def water_model_series(ctx: SpotContext) -> list[Sample]:
    """The modelled water-temperature series (built once and cached), for callers outside scoring."""
    return _water_model_series(ctx)


def _water_inputs(ctx: SpotContext, when: datetime, now: datetime) -> tuple[list[Sample], float | None, str]:
    """(series, level at `when`, source): a real reading sets the level, the model the shape.

    A sensor (or an offshore SST pixel) says what the water *is*; only the model can say how it is
    *changing*. So the model series is shifted by the constant offset between the two at `now` -
    a persistent bias, which is what a sensor-versus-model gap almost always is - instead of
    splicing a level into a series it does not belong to (which would read the gap as a thermal
    shock). Without any reading, the model stands alone.
    """
    measured = ctx.get("water_temp")
    if measured and at(measured, when, 12.0) is not None:
        return measured, at(measured, when, 12.0), "sensor"

    model = _water_model_series(ctx)
    anchor, src = wt.fuse_water_temp(
        ctx.water_sensor, ctx.sea_surface_temp, None, sensor_age_hours=ctx.water_sensor_age_hours
    )
    if anchor is not None and src in ("sensor", "sst"):
        model_now = at(model, now, 12.0) if model else None
        if model_now is not None:
            offset = anchor - model_now
            shifted = [(t, v + offset) for t, v in model]
            return shifted, at(shifted, when, 12.0), src
        return [], anchor, src
    if model:
        level = at(model, when, 12.0)
        return model, level, "model" if level is not None else "none"
    if anchor is not None:                                  # a stale sensor beats nothing
        return [], anchor, src
    return [], None, "none"


def prepare(ctx: SpotContext, when: datetime, *, now: datetime | None = None) -> Prepared:
    """Run every species-independent sub-analysis for one instant.

    `now` is the moment the live sensor readings refer to; for a forecast curve it is the start
    of the curve, so future hours reuse the sensor-corrected model instead of the raw sensor.
    """
    when = to_local(ctx, when)
    now = when if now is None else to_local(ctx, now)
    aware = to_aware(ctx, when)

    day = ctx.astronomy_for(when)
    # windows are keyed on local days; start a day early so a peak just before midnight still
    # reaches the early hours of today
    windows = ctx.solunar_windows(aware - timedelta(days=1), days=3)
    sol = solunar.analyse(aware, windows)

    air = ctx.get("air_temp")
    pa = pressure.analyse(
        ctx.get("pressure"),
        when,
        source=ctx.pressure_source,
        elevation_m=ctx.elevation_m,
        clim=ctx.clim,
        diurnal_cycle=ctx.diurnal_pressure_cycle or None,
        wind_dir=ctx.get("wind_dir"),
        cloud=ctx.get("cloud"),
        air_temp=air,
    )
    hs = hydro.analyse(
        when,
        spot_kind=ctx.spot_kind,
        discharge=ctx.get("discharge"),
        rain_hourly=ctx.get("precip") or ctx.get("rain"),
        catchment_response=ctx.catchment_response,
        clim=ctx.clim,
    )
    series, level, source = _water_inputs(ctx, when, now)

    obs = {
        name: at(ctx.get(name), when, OBS_MAX_AGE_H)
        for name in ("air_temp", "wind_speed", "wind_dir", "cloud", "precip", "uv_index", "humidity", "cape")
    }
    return Prepared(
        when=when,
        now=now,
        aware=aware,
        sun_altitude=astro.sun_altitude(aware, ctx.lat, ctx.lon),
        moonshine=astro.moonshine(aware, ctx.lat, ctx.lon),
        day=day,
        solunar=sol,
        tide=astro.tide_state(aware, ctx.lon) if ctx.tidal else None,
        pressure=pa,
        hydro=hs,
        water_series=series,
        water_level=level,
        water_source=source,
        obs=obs,
    )


# --------------------------------------------------------------------------------------
# the factors
# --------------------------------------------------------------------------------------


def _unavailable(name: str, label: str, why: str) -> FactorScore:
    return FactorScore(name, label, None, "", 0.0, 0.0, 0.0, why, "unavailable")


def _factor(name, label, value, unit, score, why, source="open-meteo") -> FactorScore:
    return FactorScore(name, label, value, unit, rs.clamp(float(score), -1.0, 1.0), 0.0, 0.0, why, source)


def _tide_run(ctx: SpotContext, p: Prepared) -> float:
    """How hard the water is moving right now, 0 (slack) .. 1 (mid-tide on a spring)."""
    before = astro.tide_state(p.aware - timedelta(minutes=30), ctx.lon).level
    after = astro.tide_state(p.aware + timedelta(minutes=30), ctx.lon).level
    run = min(1.0, abs(after - before) / _FULL_TIDE_RUN_PER_H)
    spring = p.tide.range_index if p.tide is not None else 0.5
    return run * (0.7 + 0.3 * spring)


def _factors(ctx: SpotContext, p: Prepared, prof: sp.SpeciesProfile) -> tuple[dict[str, FactorScore], list[str], wt.WaterState]:
    """Every factor for this species; (factors by name, not-applicable names, water state)."""
    out: dict[str, FactorScore] = {}
    na: list[str] = []
    o = p.obs
    pa, hs = p.pressure, p.hydro

    # -- water temperature -----------------------------------------------------------------
    water = wt.analyse(
        p.water_series,
        p.when,
        clim=ctx.clim,
        fused_value=p.water_level,
        source=p.water_source,
        species_optimum_c=prof.water_temp_optimum_c,
    )
    if water.temp is None:
        out["water_temp"] = _unavailable("water_temp", "Water temperature", "no air or water temperature data to estimate it from")
    else:
        off = water.species_offset_c
        gap = "" if off is None else f", {abs(off):.1f} degC {'above' if off > 0 else 'below'} the {prof.name} optimum"
        trend = "" if water.trend == "steady" else f" and {water.trend}"
        out["water_temp"] = _factor(
            "water_temp", "Water temperature", water.temp, "degC", water.score,
            f"Water is {water.temp:.1f} degC{gap}{trend}.", water.source,
        )

    # -- barometer, front -------------------------------------------------------------------
    if pa.parts:
        t3 = pa.tendency_3h
        words = pa.trend_label if t3 is None else f"{pa.trend_label} ({t3:+.1f} hPa/3h)"
        out["pressure"] = _factor(
            "pressure", "Barometer", t3, "hPa/3h", pa.score * prof.pressure_sensitivity,
            f"Barometer is {words}.",
        )
    else:
        out["pressure"] = _unavailable("pressure", "Barometer", "no pressure history to read a tendency from")
    if pa.front_index is not None:
        eta = "" if pa.front_eta_hours is None else f", passing in about {pa.front_eta_hours:.0f} h"
        out["front"] = _factor(
            "front", "Weather front", pa.front_index, "index", rs.FRONT_INDEX(pa.front_index) * prof.pressure_sensitivity,
            f"A front is {'likely' if pa.front_index >= 0.5 else 'possible'}{eta}.",
        )
    elif pa.parts:
        # pressure is known and shows no swing: that is "no front", a real (slightly negative) answer,
        # not missing data - reporting it as unavailable would lower confidence on every calm day
        out["front"] = _factor("front", "Weather front", 0.0, "index", rs.FRONT_INDEX(0.0) * prof.pressure_sensitivity, "No front in sight.")
    else:
        out["front"] = _unavailable("front", "Weather front", "no pressure data to locate a front")

    # -- wind, shelter ----------------------------------------------------------------------
    wind, wdir = o["wind_speed"], o["wind_dir"]
    sheltered = ctx.shelter.shelteredness(wdir)
    if wind is not None:
        score = rs.WIND_SPEED(wind)
        if sheltered is not None:
            score = 0.75 * score + 0.25 * rs.SHELTEREDNESS(sheltered)
        out["wind"] = _factor("wind", "Wind", wind, "m/s", score, f"Wind is {wind:.1f} m/s.")
    else:
        out["wind"] = _unavailable("wind", "Wind", "no wind data")
    if sheltered is not None:
        out["shelter"] = _factor(
            "shelter", "Shelter", sheltered, "index", rs.SHELTEREDNESS(sheltered),
            f"This bank is {'well' if sheltered >= 0.6 else 'poorly' if sheltered <= 0.3 else 'partly'} sheltered from a {wdir:.0f} degree wind.",
        )
    else:
        out["shelter"] = _unavailable("shelter", "Shelter", "no wind direction")

    # -- sky ------------------------------------------------------------------------------
    cloud = o["cloud"]
    if cloud is not None:
        out["cloud"] = _factor("cloud", "Cloud cover", cloud, "%", rs.CLOUD_COVER(cloud / 100.0), f"Cloud cover is {cloud:.0f}%.")
    else:
        out["cloud"] = _unavailable("cloud", "Cloud cover", "no cloud data")

    if p.sun_altitude <= 0.0:
        na.append("uv")                              # no sun, no brightness question
    elif o["uv_index"] is not None:
        out["uv"] = _factor("uv", "Brightness (UV)", o["uv_index"], "", rs.UV_INDEX(o["uv_index"]), f"UV index is {o['uv_index']:.0f}.")
    else:
        out["uv"] = _unavailable("uv", "Brightness (UV)", "no UV data")

    # -- rain ------------------------------------------------------------------------------
    rain_now, rain_72 = o["precip"], hs.rain_72h_mm
    if rain_now is not None or rain_72 is not None:
        parts, wts = [], []
        if rain_now is not None:
            parts.append(rs.PRECIP_LIGHT(rain_now))
            wts.append(0.6)
        if rain_72 is not None:
            parts.append(rs.RAIN_ON_DAYS(rain_72))
            wts.append(0.4)
        score = sum(a * b for a, b in zip(parts, wts)) / sum(wts)
        bits = []
        if rain_now is not None:
            bits.append(f"{rain_now:.1f} mm/h falling now")
        if rain_72 is not None:
            bits.append(f"{rain_72:.0f} mm in the last 3 days")
        out["precip"] = _factor("precip", "Rain", rain_now if rain_now is not None else rain_72, "mm", score, "Rain: " + ", ".join(bits) + ".")
    else:
        out["precip"] = _unavailable("precip", "Rain", "no rainfall data")

    # -- water state (level, turbidity) ------------------------------------------------------
    if hs.parts:
        clarity = hydro.fishing_water_clarity_words(hs.turbidity_index)
        extra = "" if hs.discharge_percentile is None else f", level {hs.level_words}"
        out["hydro"] = _factor("hydro", "Water clarity & level", hs.turbidity_index, "index", hs.score, f"Water is likely {clarity}{extra}.", "model")
    else:
        out["hydro"] = _unavailable("hydro", "Water clarity & level", "no rainfall or river-level data")

    # -- solunar, light, moon, tide ---------------------------------------------------------
    sol_score = p.solunar.score
    if ctx.tidal and p.tide is not None:
        bonus = solunar.tide_bonus(ctx.tide_range_m, 1.0 + 0.5 * p.tide.range_index)
        sol_score = sol_score + bonus * p.solunar.intensity
    if p.solunar.in_window:
        why = f"Inside a {p.solunar.in_window} solunar window ({p.solunar.label})."
    elif p.solunar.hours_to_next is not None:
        why = f"Next solunar window in {p.solunar.hours_to_next:.1f} h."
    else:
        why = "Between solunar windows."
    out["solunar"] = _factor("solunar", "Solunar timing", p.solunar.intensity, "index", sol_score, why, "model")

    light = rs.LIGHT_CURVES.get(prof.light_window, rs.LIGHT_OPPORTUNISTIC)
    alt = p.sun_altitude
    phase = "night" if alt < -6 else "twilight" if alt < 0 else "low sun" if alt < 12 else "mid sun" if alt < 30 else "high sun"
    out["time_of_day"] = _factor(
        "time_of_day", "Light window", alt, "deg", light(alt),
        f"Sun is {alt:+.0f} degrees ({phase}); {prof.name} favour {prof.light_window.replace('_', ' ')} light.", "model",
    )

    if alt > 0.0:
        na.append("moonlight")                       # a daytime moon is not a lighting question
    else:
        mscore = prof.moonlight_effect * rs.MOONLIGHT_EFFECT(p.moonshine)
        feel = "helps" if mscore > 0.02 else "hinders" if mscore < -0.02 else "barely matters for"
        out["moonlight"] = _factor("moonlight", "Moonlight", p.moonshine, "index", mscore, f"Moonlight at {p.moonshine * 100:.0f}% {feel} {prof.name}.", "model")

    if ctx.tidal and p.tide is not None:
        run = _tide_run(ctx, p)
        out["tide"] = _factor("tide", "Tide", run, "index", rs.TIDE_MOTION(run), f"Tide is {p.tide.label}, {run * 100:.0f}% of a full run.", "model")
    else:
        na.append("tide")

    return out, na, water


_MISSING_NOTES = {
    "water_temp": "No water-temperature estimate: needs air-temperature history or a probe.",
    "pressure": "No barometric history: pressure and front effects are ignored.",
    "front": "No forecast pressure: an approaching front cannot be located.",
    "wind": "No wind data: wind effects are ignored.",
    "shelter": "No wind direction: shelter from the bank geometry is ignored.",
    "cloud": "No cloud data: light-diffusion effects are ignored.",
    "uv": "No UV data: brightness effects are ignored.",
    "precip": "No rainfall data: rain effects are ignored.",
    "hydro": "No rainfall or river-level data: water-colour and level effects are ignored.",
}


def _clim_trust(ctx: SpotContext, when: datetime) -> float:
    """How many of the key local normals the climatology can actually stand behind (0..1)."""
    if ctx.clim is None:
        return 0.0
    variables = ("pressure" if ctx.pressure_source == pressure.STATION else "pressure_slp", "water_temp", "air_temp")
    return sum(1 for v in variables if ctx.clim.normal(v, when).usable) / len(variables)


# --------------------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------------------


def evaluate(
    ctx: SpotContext,
    when: datetime,
    prof: sp.SpeciesProfile | None = None,
    prepared: Prepared | None = None,
) -> Evaluation:
    """Score one instant for one species."""
    prof = prof or sp.profile("mixed_general")
    p = prepared if prepared is not None and prepared.when == to_local(ctx, when) else prepare(ctx, when)
    factors, not_applicable, water = _factors(ctx, p, prof)

    weights = {k: v for k, v in prof.weight_map().items() if k not in not_applicable}
    scores = {k: (f.score if f.source != "unavailable" else None) for k, f in factors.items()}
    raw, coverage, dropped = rs.weighted_score(scores, weights)

    used = sum(w for k, w in weights.items() if scores.get(k) is not None)
    # a factor built from only part of its inputs (a river judged on rainfall alone) counts as
    # only that part present, so confidence falls when a gauge goes missing even though the
    # factor still produces a number
    if scores.get("hydro") is not None:
        total = sum(weights.values()) or 1.0
        coverage -= weights["hydro"] * (1.0 - p.hydro.completeness) / total
    ordered: list[FactorScore] = []
    for name in sp.FACTORS:
        f = factors.get(name)
        if f is None or name in not_applicable:
            continue
        if f.source != "unavailable" and used > 0:
            f.weight = weights.get(name, 0.0) / used
            f.contribution = f.weight * f.score
        ordered.append(f)

    season = prof.season_factor(p.when.date())
    raw = raw * season if raw > 0 else raw * (2.0 - season)       # off season lowers the ceiling, deepens the floor
    raw = rs.clamp(raw, -1.0, 1.0)

    confidence = rs.clamp(
        0.55 * coverage + 0.25 * _clim_trust(ctx, p.when) + 0.20 * (1.0 if p.pressure.diurnal_corrected else 0.5),
        0.0,
        1.0,
    )
    score10 = (raw + 1.0) * 5.0
    shown = 5.0 + (score10 - 5.0) * (0.55 + 0.45 * confidence)     # never display certainty we do not have

    notes: list[str] = []
    for name in dropped:
        if weights.get(name, 0.0) >= 0.03 and name in _MISSING_NOTES:
            notes.append(_MISSING_NOTES[name])
    for source_notes in (water.notes, p.pressure.notes, p.hydro.notes):
        for n in source_notes:
            # a gauge is only expected on running water; on a lake its absence is not news
            if "discharge/level" in n and ctx.spot_kind not in ("river", "stream"):
                continue
            n = n[0].upper() + n[1:]
            if n not in notes:
                notes.append(n)
    if season < 0.85:
        notes.append(f"{prof.name} are out of their best season, which caps the score.")

    return Evaluation(
        when=p.when,
        species_id=prof.id,
        raw=raw,
        score=shown,
        band=band((shown - 5.0) / 5.0),
        confidence=confidence,
        coverage=coverage,
        factors=ordered,
        notes=notes,
        light=p.day.light,
        solunar=p.solunar,
        sun_altitude=p.sun_altitude,
        moonshine=p.moonshine,
        season_factor=season,
        not_applicable=not_applicable,
        water=water,
        pressure=p.pressure,
        hydro=p.hydro,
        obs=dict(p.obs),
    )


def hourly_curve(
    ctx: SpotContext,
    start: datetime,
    hours: int = 48,
    prof: sp.SpeciesProfile | None = None,
    step_min: int = 60,
) -> list[tuple[datetime, Evaluation]]:
    """Scores across a forecast window. Deterministic: same context in, same curve out."""
    prof = prof or sp.profile("mixed_general")
    start = to_local(ctx, start)
    out: list[tuple[datetime, Evaluation]] = []
    step = timedelta(minutes=step_min)
    t = start
    end = start + timedelta(hours=hours)
    while t < end:
        out.append((t, evaluate(ctx, t, prof, prepare(ctx, t, now=start))))
        t += step
    return out


def best_spans(
    ctx: SpotContext,
    start: datetime,
    hours: int = 48,
    prof: sp.SpeciesProfile | None = None,
    min_score: float = 6.0,
    step_min: int = 60,
) -> list[Span]:
    """Merged contiguous runs of the curve at or above `min_score`."""
    return spans_from_curve(hourly_curve(ctx, start, hours, prof, step_min), min_score, step_min)


def spans_from_curve(curve: list[tuple[datetime, Evaluation]], min_score: float = 6.0, step_min: int = 60) -> list[Span]:
    spans: list[Span] = []
    run: list[tuple[datetime, Evaluation]] = []

    def close() -> None:
        if not run:
            return
        peak_t, peak_ev = max(run, key=lambda item: item[1].score)
        spans.append(
            Span(
                start=run[0][0],
                end=run[-1][0] + timedelta(minutes=step_min),
                peak_at=peak_t,
                peak_score=peak_ev.score,
                mean_score=sum(ev.score for _, ev in run) / len(run),
            )
        )
        run.clear()

    for t, ev in curve:
        if ev.score >= min_score and (not run or t - run[-1][0] <= timedelta(minutes=step_min)):
            run.append((t, ev))
        else:
            close()
            if ev.score >= min_score:
                run.append((t, ev))
    close()
    return spans


__all__ = [
    "BANDS", "Evaluation", "FactorScore", "Prepared", "Span",
    "band", "best_spans", "evaluate", "hourly_curve", "local_event", "prepare",
    "spans_from_curve", "to_aware", "to_local",
]
