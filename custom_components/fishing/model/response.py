"""Response curves: the place where fishing *knowledge* lives, as data.

A factor module's job is to turn the world into a number (anomaly in sigmas, hPa/3 h, m/s,
degC). This module's job is to turn that number into -1..+1 "how good is this for fishing",
with the shape - not a magic constant - carrying the reasoning. Shapes are exposed as data so
species profiles and the calibration loop can retune them without touching physics code.

Signs are deliberate: the classic angler folklore ("barometer falling = feed", "bright sun =
bad") is *conditional*, so the condition belongs in the factor that computes the value, and the
curve stays a simple, inspectable shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import cos, exp, pi


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def interp(points: list[tuple[float, float]], x: float) -> float:
    """Piecewise-linear interpolation with flat extrapolation outside the anchors."""
    if not points:
        return 0.0
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return points[-1][1]


def smoothstep(x: float, x0: float, x1: float) -> float:
    """0 at x0, 1 at x1, S-curved between (never a cliff - scores must not flicker)."""
    if x1 == x0:
        return 1.0 if x >= x1 else 0.0
    t = (x - x0) / (x1 - x0)
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    return t * t * (3.0 - 2.0 * t)


def gaussian(x: float, optimum: float, width: float) -> float:
    """1 at the optimum, falling to ~0.01 at 3 sigma - the shape of a temperature preference."""
    if width <= 0:
        return 0.0
    return exp(-0.5 * (x / width) ** 2)


def cosine_bump(x: float, lo: float, hi: float) -> float:
    """0 outside [lo, hi], 1 at the midpoint - daylight/season length preferences."""
    if hi <= lo or not (lo <= x <= hi):
        return 0.0
    t = (x - lo) / (hi - lo)
    return 0.5 - 0.5 * cos(2.0 * pi * t)


@dataclass
class Curve:
    """A named response shape: anchors of (value, score), value in the curve's own unit.

    `kind` documents what the x-axis means so a calibration pass can tell anomalies (sigmas)
    from physical units (degC, hPa/3h, m/s) and re-fit the right anchors.
    """

    name: str
    points: list[tuple[float, float]]
    kind: str = "anomaly_sigma"  # anomaly_sigma | physical | index01 | hours
    notes: str = ""

    def __call__(self, x: float | None) -> float:
        if x is None:
            return 0.0
        return clamp(interp(self.points, float(x)))

    def shifted(self, shift: float) -> "Curve":
        """Re-centre (used by calibration and by species that prefer it colder/warmer)."""
        return Curve(self.name, [(x + shift, y) for x, y in self.points], self.kind, self.notes)

    def scaled(self, factor: float) -> "Curve":
        return Curve(self.name, [(x, clamp(y * factor)) for x, y in self.points], self.kind, self.notes)


# --------------------------------------------------------------------------------------
# the canonical library of shapes used by the factor modules
# --------------------------------------------------------------------------------------

# Pressure *anomaly* (sigmas from this spot's seasonal normal), not absolute hPa: see docs
# section "elevation is a first-class variable". Falling tendency is scored separately by
# pressure.py; this curve is the slow background state.
PRESSURE_ANOMALY = Curve(
    "pressure_anomaly",
    [
        (-2.0, -0.55),   # deep low: storm, dirty water, unsafe - most anglers are off the water
        (-1.0, 0.15),    # below normal: unstable, but often a good pre-front window
        (-0.25, 0.35),
        (0.25, 0.55),    # slightly above normal: stable, the everyday winner
        (1.0, 0.25),
        (2.2, -0.20),    # strong high, glassy and slow (especially in summer heat)
    ],
    notes="inverted-U: both extremes hurt, mild positive normal wins",
)

# Surface pressure tendency, hPa per 3 h. The classic trigger: the *change* moves fish
# sideways and starts a bite window before the weather actually arrives.
PRESSURE_TENDENCY_3H = Curve(
    "pressure_tendency_3h",
    [
        (-4.5, 0.20),   # a crashing fall usually means a violent front: after the passage it is dead
        (-2.5, 0.80),   # steady fall = front approaching: the classic feed
        (-0.8, 0.45),
        (0.0, 0.10),    # steady: neutral
        (0.9, 0.30),    # a new high building behind a front: brief post-front pop
        (2.5, -0.25),   # pressure rocketing = cold, clear and locked down
    ],
    kind="physical",
    notes="asymmetric: falling beats rising for triggering feeding",
)

PRESSURE_STABILITY_24H = Curve(
    "pressure_stability_24h",
    [
        (0.0, 0.45),    # a calm 24 h is stable - good for wary species (carp, bream)
        (4.0, 0.20),
        (8.0, -0.15),   # big daily swing = a rolling succession of fronts
        (14.0, -0.45),
    ],
    kind="physical",
    notes="|max-min| over 24 h; small is good, huge is chaos",
)

FRONT_INDEX = Curve(
    "front_index",
    [(0.0, -0.15), (0.35, 0.25), (0.7, 0.75), (1.0, 0.55)],
    kind="index01",
    notes="peak just before/through the passage, not after it",
)

AIR_TEMP_ANOMALY = Curve(
    "air_temp_anomaly",
    [(-4.0, -0.35), (-1.5, 0.10), (0.0, 0.25), (1.2, 0.45), (3.0, 0.10), (5.0, -0.45)],
    notes="mild warmth above the seasonal normal wins; cold snaps and heat waves both hurt",
)

WATER_TEMP_SUITABILITY = Curve(
    "water_temp_suitability",
    # x is degrees C from this species' optimum (species.py supplies the optimum)
    [(-12.0, -0.80), (-6.0, -0.30), (-2.5, 0.35), (0.0, 1.00), (2.5, 0.45), (6.0, -0.35), (12.0, -0.85)],
    kind="physical",
    notes="the strongest single predictor for temperate sport fish",
)

WATER_TEMP_CHANGE_7D = Curve(
    "water_temp_change_7d",
    [(-8.0, -0.60), (-3.0, -0.15), (-0.5, 0.25), (0.4, 0.55), (2.0, 0.35), (5.0, -0.30)],
    kind="physical",
    notes="a slow seasonal rise is a green-light; a crash or a heat spike shuts fishing down",
)

WIND_SPEED = Curve(
    "wind_speed",
    [
        (0.0, -0.25),   # flat glass: pressured water, no surface food cycle
        (1.5, 0.25),
        (3.5, 0.75),    # ripple, oxygenation, baitfish pushed to structure
        (6.5, 0.35),
        (9.5, -0.35),   # unsafe/uneconomic, fish driven to cover
        (14.0, -0.85),
    ],
    kind="physical",
)

SHELTEREDNESS = Curve(
    "shelteredness",
    [(0.0, -0.35), (0.5, 0.15), (1.0, 0.60)],
    kind="index01",
    notes="how much the local shore/fetch geometry protects the bank from the wind direction",
)

CLOUD_COVER = Curve(
    "cloud_cover",
    [(0.0, -0.30), (0.25, 0.10), (0.55, 0.50), (0.8, 0.35), (1.0, 0.05)],
    kind="index01",
    notes="broken cover beats both clear sky and flat overcast; bright sun pushes fish deep",
)

PRECIP_LIGHT = Curve(
    "precip_light",
    [(0.0, 0.05), (0.3, 0.45), (1.5, 0.55), (4.0, 0.05), (10.0, -0.55)],
    kind="physical",
    notes="mm/h: drizzle and short showers help, a cloudburst scours and chills",
)

RAIN_ON_DAYS = Curve(
    "rain_on_days",
    [(0.0, 0.10), (5.0, 0.30), (15.0, 0.55), (30.0, 0.15), (60.0, -0.45)],
    kind="physical",
    notes="mm over the previous 3 days: a slow wetting feeds insects, a deluge muds the water",
)

HUMIDITY_COMFORT = Curve(
    "humidity_comfort",
    [(0.25, -0.25), (0.45, 0.15), (0.70, 0.30), (0.92, 0.05), (1.0, -0.15)],
    kind="index01",
    notes="dry air dries out bait and kills the midge hatch; saturated air means mugginess",
)

UV_INDEX = Curve(
    "uv_index",
    [(0.0, 0.30), (2.0, 0.35), (5.0, 0.05), (8.0, -0.35), (11.0, -0.60)],
    kind="physical",
    notes="visible for sight hunters: high UV drives predatory fish down and makes lures flash harder",
)

THUNDERSTORM_PROXIMITY = Curve(
    "thunderstorm",
    [(0.0, -0.55), (0.25, -0.25), (0.5, 0.10), (0.75, 0.45), (1.0, 0.15)],
    kind="index01",
    notes="1 = storm close. Angry static and unsafe water; the hours *before* one are excellent",
)

DIURNAL_STABILITY = Curve(
    "diurnal_stability",
    [(0.0, -0.40), (3.0, -0.10), (7.0, 0.30), (12.0, 0.55), (20.0, 0.30)],
    kind="physical",
    notes="air mass to water surface: thermal turnover/stability index (degC, signed)",
)

# Preference by *light level*, not clock time: the sun's altitude is the variable the fish
# actually experience, which is what makes the model work in the tropics (where "evening" is
# always ~18:00) and at 64 deg N (where a June night is never dark). A clock-hour curve would be
# wrong in both places.
LIGHT_DIURNAL = Curve(
    "light_diurnal",
    [(-18.0, -0.75), (-6.0, -0.45), (-1.0, 0.05), (8.0, 0.55), (22.0, 0.60), (40.0, 0.30), (65.0, -0.10)],
    kind="physical",
    notes="x = sun altitude in deg: daylight species, tolerant of brightness",
)
LIGHT_CREPUSCULAR = Curve(
    "light_crepuscular",
    [(-18.0, -0.35), (-9.0, -0.05), (-3.0, 0.80), (4.0, 0.70), (12.0, 0.15), (30.0, -0.35), (60.0, -0.60)],
    kind="physical",
    notes="the pike/perch/bass curve: peak at civil twilight, hard light is a penalty",
)
LIGHT_NOCTURNAL = Curve(
    "light_nocturnal",
    [(-25.0, 0.80), (-9.0, 0.70), (-1.5, 0.10), (6.0, -0.55), (25.0, -0.80), (60.0, -0.85)],
    kind="physical",
    notes="zander/catfish/eel: darkness is the habitat, daytime is a liability",
)
LIGHT_OPPORTUNISTIC = Curve(
    "light_opportunistic",
    [(-18.0, -0.15), (-6.0, 0.15), (2.0, 0.40), (18.0, 0.35), (45.0, 0.05), (70.0, -0.20)],
    kind="physical",
    notes="carp, bream, carp-like cyprinids: light matters much less than temperature",
)

LIGHT_CURVES = {
    "diurnal": LIGHT_DIURNAL,
    "crepuscular": LIGHT_CREPUSCULAR,
    "nocturnal": LIGHT_NOCTURNAL,
    "opportunistic": LIGHT_OPPORTUNISTIC,
}

MOONLIGHT_EFFECT = Curve(
    "moonlight",
    [(0.0, 0.0), (0.25, 0.20), (0.5, 0.45), (0.75, 0.70), (1.0, 0.90)],
    kind="index01",
    notes="applied with the species' signed moonlight_effect, so the sign does the arguing",
)

TIDE_MOTION = Curve(
    "tide_motion",
    [(0.0, -0.45), (0.2, 0.10), (0.5, 0.55), (0.8, 0.75), (1.0, 0.55)],
    kind="index01",
    notes="normalised tidal run: slack water is dead water for most tidal species",
)

CAPE_INDEX = Curve(
    "cape",
    [(0.0, 0.0), (300.0, 0.15), (800.0, 0.35), (1500.0, 0.10), (2500.0, -0.35), (4500.0, -0.70)],
    kind="physical",
    notes="J/kg instability: a brew of thunderstorms lifts a lake for hours, a squing line ends it",
)

TIME_OF_DAY = Curve(
    "time_of_day",
    [
        (0.0, 0.35), (3.0, 0.30), (5.0, 0.60), (7.0, 0.75), (10.0, 0.05),
        (13.0, -0.30), (16.0, 0.10), (18.5, 0.80), (21.0, 0.55), (23.0, 0.40),
    ],
    kind="hours",
    notes="solar hour, generic backstop only; golden_windows() does the real work",
)

DAY_LENGTH = Curve(
    "day_length",
    [(7.5, -0.20), (10.0, 0.10), (12.5, 0.30), (15.5, 0.45), (18.0, 0.25), (21.0, -0.15)],
    kind="hours",
    notes="photoperiod drives seasonal movement and spawn timing",
)


def normalise_weights(factors: dict[str, float | None]) -> dict[str, float]:
    """Drop unavailable factors and renormalise what is left.

    This is the whole degradation story: a missing sensor or a failed API removes a term
    instead of silently assuming "average", and the caller reports the reduced weight sum as
    confidence so the UI can say "this is 60 % of what I normally know".
    """
    present = {k: float(w) for k, w in factors.items() if w is not None and float(w) > 0.0}
    total = sum(present.values())
    if total <= 0:
        return {}
    return {k: w / total for k, w in present.items()}


def weighted_score(
    scores: dict[str, float | None],
    weights: dict[str, float],
    *,
    require: tuple[str, ...] = (),
) -> tuple[float, float, list[str]]:
    """Return (score -1..1, coverage 0..1, dropped factor names).

    `require` lists factors without which the whole evaluation is meaningless (e.g. solunar for
    a species profile that leans on it); those are reported as dropped rather than skipped.
    """
    used = 0.0
    acc = 0.0
    dropped: list[str] = []
    for name, w in weights.items():
        val = scores.get(name)
        if val is None or w <= 0:
            dropped.append(name)
            continue
        acc += w * val
        used += w
    if used <= 0:
        return 0.0, 0.0, sorted(set(dropped))
    missing_required = [r for r in require if r in dropped]
    if missing_required:
        return 0.0, 0.0, sorted(set(dropped))
    total_weight = sum(max(0.0, w) for w in weights.values()) or 1.0
    return clamp(acc / used, -1.0, 1.0), used / total_weight, dropped
