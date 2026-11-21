"""Water state: flows, levels, turbidity and what rain actually does to a fishing spot.

Rain is not a single variable. The same 20 mm can be the best fishing of the month or the worst,
depending on what the ground was already holding, how fast the catchment answers, and whether the
water is a mountain stream, a lowland drain, a lake or an estuary. This module separates:

* **level/discharge state** - is the water low, normal or high *for this watercourse*, measured
  against its own recent distribution (discharge is log-normally distributed, so the anomaly is
  taken on log(Q) - a linear z-score on a river makes every flood look impossible),
* **level trend** - rising or falling, and how fast: the classic "fish feed ahead of a rise and
  after it clears" pattern lives here,
* **turbidity proxy** - a deliberately simple convolution: rain, weighted by antecedent wetness
  (dry ground soaks it up, saturated ground sends it straight in with the soil), plus
  resuspension while the flow is rising, minus settling time since the peak,
* **runoff/plume** - freshwater reaching a lake, estuary or the coast.

Everything is a *proxy* unless the user connects a real turbidity/level sensor, and the module
says so in `notes`: an estuary spot with no marine data gets a lower confidence rather than a
confident guess.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from math import exp, log

from . import response as rs
from .climatology import Climatology
from .series import Sample, at, clean, decayed_sum, sum_over, value_and_slope

# --------------------------------------------------------------------------------------
# response shapes owned by this module
# --------------------------------------------------------------------------------------

DISCHARGE_STATE = rs.Curve(
    "discharge_state",
    [
        (-2.2, -0.70),   # hammered: fish pinned to cover, no forage movement
        (-1.2, -0.25),
        (-0.4, 0.20),
        (0.2, 0.55),     # a touch above normal: mobile fish spread over the banks
        (1.0, 0.30),
        (1.8, -0.45),    # the water is out of balance with its channel
        (3.0, -0.85),    # flood: safety, debris, mud, and fish holding in slack
    ],
    kind="anomaly_sigma",
    notes="log-discharge sigma: 'high for this river', not an absolute level",
)

LEVEL_TREND = rs.Curve(
    "level_trend",
    [(-1.6, -0.30), (-0.6, 0.15), (-0.15, 0.30), (0.15, 0.35), (0.8, 0.70), (1.8, 0.10), (3.5, -0.55)],
    kind="anomaly_sigma",
    notes="a moderate rise triggers feeding; a violent rise or a collapse shuts it down",
)

TURBIDITY = rs.Curve(
    "turbidity",
    [(0.0, -0.20), (0.18, 0.20), (0.38, 0.45), (0.62, 0.05), (0.8, -0.45), (1.0, -0.80)],
    kind="index01",
    notes="a little stained water kills visibility advantages; thick flood water kills lures",
)

ANTECEDENT_DRYNESS = rs.Curve(
    "antecedent",
    [(0.0, 0.15), (0.3, 0.30), (0.7, 0.10), (1.0, -0.35)],
    kind="index01",
    notes="how wet the catchment already was: first rain after a dry spell washes in insects",
)

RAINDROP_STILL_WATER = rs.Curve(
    "raindrop_still",
    [(0.0, 0.0), (0.2, 0.35), (0.8, 0.50), (2.5, 0.20), (8.0, -0.40)],
    kind="physical",
    notes="mm/h on a lake: raindrops break the surface film and hide anglers - the classic dry-fly paradox",
)

FRESHWATER_PLUME = rs.Curve(
    "plume",
    [(0.0, 0.10), (0.3, 0.45), (0.6, 0.15), (1.0, -0.50)],
    kind="index01",
    notes="freshwater reaching salt/brackish water: a feeding edge for sea-run and estuary species, a wall for strict marine ones",
)

#: how a spot's catchment answers to rain: 0 = sandy/forested/karst-buffered (slow, clear),
#: 1 = clay/urban/drained (fast, muddy)
CATCHMENT_RESPONSES = {
    "forest_sandy": 0.12,
    "peat_bog": 0.30,
    "chalk_karst": 0.45,
    "mixed_farmland": 0.60,
    "clay_drained": 0.75,
    "urban_storm": 0.95,
    "mountain_steep": 0.90,
}


@dataclass
class HydroState:
    when: datetime
    spot_kind: str                      # stream | river | lake | reservoir | estuary | coast
    discharge: float | None
    discharge_z: float | None           # on log(Q): the only honest way to standardise a river
    discharge_percentile: float | None
    level_trend_z: float | None
    rain_24h_mm: float | None
    rain_72h_mm: float | None
    rain_intensity_mmh: float | None
    antecedent_mm: float | None
    turbidity_index: float | None
    plume_index: float | None
    score: float = 0.0
    parts: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: share of this spot kind's hydro weight that had data (1 = full picture). A river scored from
    #: rainfall alone is a partial picture, and the engine lowers its confidence by this much.
    completeness: float = 1.0

    @property
    def level_words(self) -> str:
        if self.discharge_percentile is None:
            return "unknown"
        p = self.discharge_percentile
        if p < 0.10:
            return "very low for this water"
        if p < 0.30:
            return "low for this water"
        if p < 0.70:
            return "normal for this water"
        if p < 0.90:
            return "high for this water"
        return "very high for this water"

    def as_dict(self) -> dict:
        return {
            "when": self.when.isoformat(),
            "spot_kind": self.spot_kind,
            "discharge": self.discharge,
            "discharge_z": self.discharge_z,
            "discharge_percentile": self.discharge_percentile,
            "level_words": self.level_words,
            "level_trend_z": self.level_trend_z,
            "rain_24h_mm": self.rain_24h_mm,
            "rain_72h_mm": self.rain_72h_mm,
            "rain_intensity_mmh": self.rain_intensity_mmh,
            "antecedent_mm": self.antecedent_mm,
            "turbidity_index": self.turbidity_index,
            "plume_index": self.plume_index,
            "score": self.score,
            "completeness": self.completeness,
            "notes": self.notes,
        }


def log_discharge_stats(discharge: list[Sample]) -> tuple[float, float, int]:
    """mean/sd of log(Q + 1): discharge is heavy-tailed, so this is the right space to ask
    "is today high for this river?" in."""
    vals = [log(max(0.0, q) + 1.0) for _, q in discharge if q is not None and q >= 0.0]
    if len(vals) < 8:
        return 0.0, 0.0, len(vals)
    mu = sum(vals) / len(vals)
    var = sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)
    return mu, var**0.5, len(vals)


def level_slope_per_day(discharge: list[Sample], when: datetime) -> float | None:
    """Rate of change of log(Q + 1) per day: "is the river rising, and how fast for *its* size?".

    Working on the log keeps it scale-free (a 50 % rise is a 50 % rise on a brook and on the
    Rhine) and comparable with `log_discharge_stats`' sd, which is also in log space. The flood
    API is *daily*, and a regression needs three points, so a 24 h window holds too few of them;
    widen to 72 h rather than silently never reporting a trend.
    """
    logq = [(t, log(max(0.0, q) + 1.0)) for t, q in discharge if q is not None]
    for hours in (24.0, 72.0):
        _, slope = value_and_slope(logq, when, hours)
        if slope is not None:
            return slope * 24.0
    return None


def percentile_rank(discharge: list[Sample], value: float) -> float | None:
    vals = sorted(q for _, q in discharge if q is not None)
    if len(vals) < 8:
        return None
    below = sum(1 for q in vals if q < value)
    equal = sum(1 for q in vals if q == value)
    return (below + 0.5 * equal) / len(vals)


def turbidity_proxy(
    rain_hourly: list[Sample],
    discharge: list[Sample],
    when: datetime,
    catchment_response: float,
    *,
    days: float = 6.0,
) -> float | None:
    """Muddy-ness as a decaying convolution of rain, scaled by how the land answers.

    Two real effects drive it: antecedent wetness (saturated ground delivers rain as turbid
    overland flow, dry ground delivers it as clean baseflow) and resuspension while the flow is
    still rising. The exponential decay after the peak is what makes the "it rained Tuesday but
    it fished well Saturday" observation make sense.
    """
    if not rain_hourly:
        return None
    recent = decayed_sum(rain_hourly, when, days, daily_decay=0.72)
    if recent is None:
        return None
    wetness = decayed_sum(rain_hourly, when, 21.0, daily_decay=0.94) or 0.0
    # saturating wetness factor: 0 dry, ~1 soaked
    wet_factor = 1.0 - exp(-wetness / 35.0)
    drive = recent * (0.35 + 0.65 * wet_factor)
    base = 0.10 + 0.9 * (1.0 - exp(-drive / (6.0 + 26.0 * (1.0 - catchment_response))))
    base *= 0.45 + 0.55 * catchment_response
    if discharge:
        slope_day = level_slope_per_day(discharge, when)
        mu, sd, n = log_discharge_stats(discharge)
        if slope_day is not None and sd > 0:
            zs = (slope_day * 18.0 / 24.0) / (sd * 1.0 + 1e-9)
            if zs > 0.5:
                base *= 1.0 + min(0.55, 0.28 * zs)     # resuspension on the rising limb
            elif zs < -0.5:
                base *= max(0.45, 1.0 - min(0.45, 0.22 * abs(zs)))   # settling on the recession
    return max(0.0, min(1.0, base))


def plume_index(rain_72h_mm: float | None, antecedent_mm: float | None, spot_kind: str) -> float | None:
    """How much freshwater is pushing into brackish/salt water (or into a lake's shallows)."""
    if rain_72h_mm is None:
        return None
    if spot_kind not in ("estuary", "coast", "lake", "reservoir"):
        return None
    wet = 0.0 if antecedent_mm is None else antecedent_mm
    return max(0.0, min(1.0, (rain_72h_mm + 0.35 * wet) / 55.0))


def analyse(
    when: datetime,
    *,
    spot_kind: str = "river",
    discharge: list[Sample] | None = None,
    rain_hourly: list[Sample] | None = None,
    rain_daily: list[Sample] | None = None,
    catchment_response: float = 0.6,
    clim: Climatology | None = None,
) -> HydroState:
    """Full water-state picture at *when*, degrading cleanly when the river gauge is missing."""
    discharge = clean(discharge or [])
    rain = clean(rain_hourly or rain_daily or [])
    hourly_rain = bool(rain_hourly)
    notes: list[str] = []

    q = at(discharge, when, max_age_hours=12.0)
    q_z = q_pct = trend_z = None
    if q is None:
        notes.append("no discharge/level data: river state inferred from rainfall only")
    else:
        mu, sd, n = log_discharge_stats(discharge)
        if sd > 0 and n >= 8:
            q_z = (log(max(0.0, q) + 1.0) - mu) / sd
            q_pct = percentile_rank(discharge, q)
            slope_day = level_slope_per_day(discharge, when)
            if slope_day is not None:
                trend_z = slope_day / (sd * 1.0 + 1e-9) / 1.6
        else:
            notes.append("discharge history too short for a level anomaly (needs >= 8 daily values)")

    rain_24 = sum_over(rain, when, 24.0)
    rain_72 = sum_over(rain, when, 72.0)
    # an intensity (mm/h) only exists for an hourly series; a daily total says nothing about how
    # hard it fell, and read as mm/h a 10 mm day would look like a cloudburst
    intensity = at(rain, when, max_age_hours=3.0) if hourly_rain else None
    antecedent = decayed_sum(rain, when, 21.0, daily_decay=0.94)
    turbidity = turbidity_proxy(rain, discharge, when, catchment_response)
    plume = plume_index(rain_72, antecedent, spot_kind)

    if turbidity is None and rain:
        notes.append("turbidity proxy from rainfall (no gauge to resuspend from)")

    parts: dict[str, float] = {}
    weights_by_kind: dict[str, dict[str, float]] = {
        "stream": {"state": 0.34, "trend": 0.24, "turbidity": 0.32, "antecedent": 0.10},
        "river": {"state": 0.34, "trend": 0.24, "turbidity": 0.32, "antecedent": 0.10},
        "lake": {"turbidity": 0.34, "raindrop": 0.28, "antecedent": 0.16, "plume": 0.22},
        "reservoir": {"turbidity": 0.36, "raindrop": 0.26, "antecedent": 0.16, "plume": 0.22},
        "estuary": {"plume": 0.40, "turbidity": 0.40, "trend": 0.20},
        "coast": {"plume": 0.34, "turbidity": 0.44, "trend": 0.22},
    }
    weights = weights_by_kind.get(spot_kind, weights_by_kind["river"])

    if q_z is not None:
        parts["state"] = DISCHARGE_STATE(q_z)
    if trend_z is not None:
        parts["trend"] = LEVEL_TREND(trend_z)
    if turbidity is not None:
        parts["turbidity"] = TURBIDITY(turbidity)
    if antecedent is not None:
        parts["antecedent"] = ANTECEDENT_DRYNESS(min(1.0, antecedent / 45.0))
    if intensity is not None and spot_kind in ("lake", "reservoir"):
        parts["raindrop"] = RAINDROP_STILL_WATER(max(0.0, float(intensity)))
    if plume is not None:
        parts["plume"] = FRESHWATER_PLUME(plume)

    score = 0.0
    completeness = 0.0
    if parts:
        used = sum(weights.get(k, 0.0) for k in parts)
        score = sum(weights.get(k, 0.0) * v for k, v in parts.items()) / used if used > 0 else 0.0
        completeness = used / (sum(weights.values()) or 1.0)

    return HydroState(
        when=when,
        spot_kind=spot_kind,
        discharge=q,
        discharge_z=q_z,
        discharge_percentile=q_pct,
        level_trend_z=trend_z,
        rain_24h_mm=rain_24,
        rain_72h_mm=rain_72,
        rain_intensity_mmh=intensity,
        antecedent_mm=antecedent,
        turbidity_index=turbidity,
        plume_index=plume,
        score=score,
        parts=parts,
        notes=notes,
        completeness=completeness,
    )


def fishing_water_clarity_words(turbidity_index: float | None) -> str:
    if turbidity_index is None:
        return "unknown"
    if turbidity_index < 0.16:
        return "gin clear"
    if turbidity_index < 0.34:
        return "lightly stained"
    if turbidity_index < 0.55:
        return "stained"
    if turbidity_index < 0.75:
        return "murky"
    return "flood-coloured"
