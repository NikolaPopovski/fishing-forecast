"""Climatology: what "normal" means *for this spot*, without a climate service.

Fishing judgements are comparisons: "the barometer is low", "the water is warm for the time of
year", "the river is high". Every one of those is meaningless unless normal is defined locally.
A spot's normal depends on latitude, distance from the coast and above all elevation (-6.5 K/km
means a spot 300 m up is ~2 degC cooler year-round; ~35 hPa lower in station pressure), so this
module builds normals from the spot's own data instead of importing someone else's table:

* Open-Meteo hands us 92 days of past daily values on day one, which is enough for a *flat*
  baseline for the current season (mode "flat");
* each day the integration observes today's values, filling one day-of-year bucket per year,
  so after a couple of years the profile is a genuine annual cycle (mode "annual");
* running statistics (count/mean/M2 per day-of-year) mean the store stays small - about
  366 buckets per variable - and never needs the raw history.

Normals are pooled over a circular +-window at query time, so a spot with two years of data
still gets a smooth curve, and `sd` comes out of real variability (that is what makes a
pressure "anomaly" comparable between the North Sea coast and the Eifel).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

SCHEMA = 1

#: Variables the model keeps normals for. Names are stable: they are the keys of the persisted
#: store and the handles the factor modules ask for.
VARIABLES = (
    "air_temp",           # daily mean, degC at the spot
    "air_temp_max",
    "air_temp_min",
    "air_temp_range",     # diurnal range: big range = stable, high-pressure weather
    "pressure",           # *station* pressure, hPa (never mix with sea-level: see below)
    "pressure_slp",       # sea-level reduced, for synoptic comparison only
    "wind_speed",         # m/s daily mean
    "cloud",              # 0..1 daily mean total cloud cover
    "humidity",           # 0..1
    "precip",             # mm day total
    "water_temp",         # degC (modelled or sensor; see water_temp.py)
    "day_length",         # hours - computed locally, so it is exact from day one
)

DAYS_IN_YEAR = 366  # bucket space: day-of-year 1..366, leap-safe by construction

# StandardEnvironment-lapse constants, kept here so pressure/temperature reductions live in
# exactly one place (see docs "elevation is a first-class variable").
LAPSE_RATE_K_PER_M = -0.0065
STANDARD_SEA_LEVEL_PRESSURE = 1013.25
STANDARD_TEMP_K = 288.15
G_OVER_R = 9.80665 / 287.053  # g / R_specific(dry air), in K/m (0.034163 K/m)


MIN_ANOMALY_SAMPLES = 8  # below this many observations the "anomaly" is noise, not signal


@dataclass
class Normal:
    """A local, seasonal reference value plus its spread and how well it is known."""

    value: float | None      # mean (or the flat baseline when coverage is thin)
    sd: float | None
    p10: float | None
    p90: float | None
    samples: int
    mode: str                # "none" | "flat" | "local" | "annual"
    covered_days: int = 0    # distinct day-of-year buckets that fed this normal

    @property
    def usable(self) -> bool:
        return self.value is not None and self.sd not in (None, 0.0) and self.samples >= MIN_ANOMALY_SAMPLES

    @property
    def trust(self) -> float:
        """0..1 confidence in the normal itself - feeds the model's overall confidence."""
        if not self.usable:
            return 0.0
        by_samples = min(1.0, self.samples / 120.0)
        by_days = min(1.0, self.covered_days / max(1, self_sd_days := 30))
        base = 0.35 + 0.65 * min(by_samples, by_days)
        return base * (0.6 if self.mode == "flat" else 1.0)


def doy_of(day: date | datetime) -> int:
    """Day of year in 1..366; Feb 29 folds into 366 so buckets stay leap-safe."""
    if isinstance(day, datetime):
        day = day.date()
    d = day.timetuple().tm_yday
    if day.month == 2 and day.day == 29:
        d = 366
    return d


def pressure_to_sea_level(station_hpa: float, elevation_m: float, mean_temp_c: float) -> float:
    """Reduce a station reading to sea level (the BPRF/Bellani form used by weather services).

    Do *not* do this to compare two forecasts: reducing with the wrong mean air temperature is
    a ~1 hPa error per 30 m of elevation error, which is exactly the size of the signal the
    pressure factor is looking for. Keep station pressure as the primary series and use this
    only for talking to synoptic charts.
    """
    t_k = mean_temp_c + 273.15
    if station_hpa <= 0:
        return 0.0
    expo = math.exp(elevation_m * G_OVER_R / t_k)
    return station_hpa * expo


def sea_level_to_station(sea_level_hpa: float, elevation_m: float, mean_temp_c: float) -> float:
    t_k = mean_temp_c + 273.15
    return sea_level_hpa * math.exp(-elevation_m * G_OVER_R / t_k)


def apply_lapse(temp_sea_level_c: float, elevation_m: float) -> float:
    """Sea-level/valley-floor temperature to the spot's temperature."""
    return temp_sea_level_c + LAPSE_RATE_K_PER_M * elevation_m


# --------------------------------------------------------------------------------------
# running statistics per day-of-year bucket (no raw history kept)
# --------------------------------------------------------------------------------------


def _new_bucket() -> dict:
    return {"n": 0, "mean": 0.0, "m2": 0.0, "min": None, "max": None}


def update_bucket(bucket: dict, value: float) -> None:
    """Welford update plus min/max. Percentiles are intentionally not tracked here: a
    +-21 day window over ~700 samples per query would need histograms, and mean+-sd is enough
    for a fishing anomaly."""
    n = bucket.get("n", 0) + 1
    delta = value - bucket.get("mean", 0.0)
    new_mean = bucket.get("mean", 0.0) + delta / n
    bucket["m2"] = bucket.get("m2", 0.0) + delta * (value - new_mean)
    bucket["mean"] = new_mean
    bucket["n"] = n
    if bucket.get("min") is None or value < bucket["min"]:
        bucket["min"] = value
    if bucket.get("max") is None or value > bucket["max"]:
        bucket["max"] = value


def pool_buckets(buckets: list[dict]) -> tuple[float | None, float | None, int]:
    """Exact parallel combination of (n, mean, M2) triples - Chan et al."""
    n_total = 0
    mean = 0.0
    m2 = 0.0
    for b in buckets:
        n = b.get("n", 0)
        if n <= 0:
            continue
        m = b["mean"]
        if n_total == 0:
            n_total, mean, m2 = n, m, b.get("m2", 0.0)
            continue
        delta = m - mean
        combined = n_total + n
        m2 = m2 + b.get("m2", 0.0) + delta * delta * n_total * n / combined
        mean = mean + delta * n / combined
        n_total = combined
    if n_total == 0:
        return None, None, 0
    var = m2 / (n_total - 1) if n_total > 1 else 0.0
    return mean, math.sqrt(max(0.0, var)), n_total


class Climatology:
    """Per-spot, per-variable, per-day-of-year normals with honest coverage reporting.

    Typical use::

        clim = Climatology(lat, elev, spot_name)
        clim.load(json_dict)                          # persisted store, may be empty
        clim.ingest_daily(date(2026, 5, 3), {"air_temp": 14.2, "pressure": 1009.7, ...})
        n = clim.normal("air_temp", date(2026, 5, 3))
        z = clim.anomaly("air_temp", 18.9, date(2026, 5, 3))   # +1.4 sigma -> "warm for May"
        clim.save()                                   # -> json dict for HA storage
    """

    def __init__(self, latitude: float, elevation_m: float, name: str = "", window_days: int = 21):
        self.latitude = latitude
        self.elevation_m = elevation_m
        self.name = name
        self.window_days = window_days
        self.data: dict[str, dict[int, dict]] = {}
        self.updated: dict[str, str] = {}
        self.days_observed = 0

    # -- persistence ---------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "latitude": self.latitude,
            "elevation_m": self.elevation_m,
            "name": self.name,
            "window_days": self.window_days,
            "days_observed": self.days_observed,
            "updated": self.updated,
            "data": {var: {str(k): v for k, v in buckets.items()} for var, buckets in self.data.items()},
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Climatology":
        obj = cls(
            float(payload.get("latitude", 0.0)),
            float(payload.get("elevation_m", 0.0)),
            str(payload.get("name", "")),
            int(payload.get("window_days", 21)),
        )
        if int(payload.get("schema", 0)) != SCHEMA:
            # a store from another version is discarded rather than guessed at: normals built on
            # a half-migrated schema would silently mis-score every factor
            return obj
        obj.days_observed = int(payload.get("days_observed", 0))
        obj.updated = dict(payload.get("updated", {}))
        for var, buckets in (payload.get("data") or {}).items():
            if var not in VARIABLES:
                continue
            out: dict[int, dict] = {}
            for key, val in (buckets or {}).items():
                try:
                    idx = int(key)
                except (TypeError, ValueError):
                    continue
                if not 1 <= idx <= DAYS_IN_YEAR or not isinstance(val, dict):
                    continue
                out[idx] = {
                    "n": int(val.get("n", 0)),
                    "mean": float(val.get("mean", 0.0)),
                    "m2": float(val.get("m2", 0.0)),
                    "min": val.get("min"),
                    "max": val.get("max"),
                }
            if out:
                obj.data[var] = out
        return obj

    # -- feeding -------------------------------------------------------------------

    def ingest_daily(self, day: date | datetime, values: dict[str, float | None]) -> int:
        """Add one day of observations; returns how many variables were updated."""
        idx = doy_of(day)
        count = 0
        for var, value in values.items():
            if var not in VARIABLES or value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            buckets = self.data.setdefault(var, {})
            bucket = buckets.setdefault(idx, _new_bucket())
            update_bucket(bucket, value)
            count += 1
        if count:
            self.days_observed += 1
            self.updated["all"] = (day if isinstance(day, date) else day.date()).isoformat()
        return count

    def ingest_series(self, daily: list[tuple[date, dict[str, float | None]]]) -> int:
        return sum(self.ingest_daily(d, v) for d, v in daily)

    def merge(self, other: "Climatology") -> None:
        """Merge another store (e.g. a rebuilt baseline) into this one, bucket by bucket."""
        for var, buckets in other.data.items():
            mine = self.data.setdefault(var, {})
            for idx, bucket in buckets.items():
                if idx not in mine:
                    mine[idx] = dict(bucket)
                else:
                    # counts/means/variances are all we can combine - raw values are not kept
                    merged = pool_buckets([mine[idx], bucket])
                    mean, sd, n = merged
                    if n:
                        var_m2 = sd * sd * (n - 1) if n > 1 else 0.0
                        mine[idx] = {
                            "n": n,
                            "mean": mean,
                            "m2": var_m2,
                            "min": _pick_min(mine[idx].get("min"), bucket.get("min")),
                            "max": _pick_max(mine[idx].get("max"), bucket.get("max")),
                        }
        self.days_observed = max(self.days_observed, other.days_observed)

    # -- queries -------------------------------------------------------------------

    def coverage(self, var: str) -> int:
        """How many day-of-year buckets hold data - the honest measure of how seasonal the
        normals can be (92 contiguous buckets = one season only)."""
        return sum(1 for b in self.data.get(var, {}).values() if b.get("n", 0) > 0)

    def normal(self, var: str, day: date | datetime) -> Normal:
        buckets = self.data.get(var, {})
        if not buckets:
            return Normal(None, None, None, None, 0, "none")
        idx = doy_of(day)
        span = range(-self.window_days, self.window_days + 1)
        covered = []
        for off in span:
            j = idx + off
            if j < 1:
                j += DAYS_IN_YEAR
            elif j > DAYS_IN_YEAR:
                j -= DAYS_IN_YEAR
            b = buckets.get(j)
            if b and b.get("n", 0) > 0:
                covered.append(b)
        if covered:
            mean, sd, n = pool_buckets(covered)
            mode = "annual" if self.coverage(var) >= 300 else "local"
            p10 = None if sd is None else mean - 1.2816 * sd
            p90 = None if sd is None else mean + 1.2816 * sd
            return Normal(mean, sd, p10, p90, n, mode, len(covered))
        # nothing near this day of year (e.g. a spot set up in May asking about December):
        # fall back to the year-wide mean so factors still work, but flag it - the caller
        # lowers confidence and the card can say "normal from the rest of the year"
        all_buckets = [b for b in buckets.values() if b.get("n", 0) > 0]
        mean, sd, n = pool_buckets(all_buckets)
        p10 = None if sd is None else mean - 1.2816 * sd
        p90 = None if sd is None else mean + 1.2816 * sd
        return Normal(mean, sd, p10, p90, n, "flat", len(all_buckets))

    def anomaly(self, var: str, value: float | None, day: date | datetime) -> float | None:
        """Standardised departure from this spot's seasonal normal, in sigmas.

        Sigma units are what let one response curve serve a Scottish sea loch and an Alpine
        lake: "1.5 sigma warmer than normal for early June" is a comparable statement in both
        places, "22 degC" is not.
        """
        if value is None:
            return None
        nrm = self.normal(var, day)
        if not nrm.usable:
            return None
        return (float(value) - nrm.value) / nrm.sd

    def anomaly_raw(self, var: str, value: float | None, day: date | datetime) -> float | None:
        if value is None:
            return None
        nrm = self.normal(var, day)
        if nrm.value is None:
            return None
        return float(value) - nrm.value

    def percentile_of(self, var: str, value: float | None, day: date | datetime) -> float | None:
        """0..1 position of a value inside this season's distribution (normal-approximated)."""
        if value is None:
            return None
        nrm = self.normal(var, day)
        if not nrm.usable:
            return None
        z = (float(value) - nrm.value) / nrm.sd
        # logistic approximation to the normal CDF, max error ~0.006
        return 1.0 / (1.0 + math.exp(-0.07056 * z**3 - 1.5976 * z))

    def health(self) -> dict:
        """Per-variable coverage, for the diagnostics sensor and onboarding cards."""
        return {var: {"buckets": self.coverage(var), "days": self.normal(var, date.today()).samples} for var in VARIABLES}


def _pick_min(a, b):
    vals = [v for v in (a, b) if v is not None]
    return min(vals) if vals else None


def _pick_max(a, b):
    vals = [v for v in (a, b) if v is not None]
    return max(vals) if vals else None


# --------------------------------------------------------------------------------------
# analog days: "the last time it was like this, what happened?"
# --------------------------------------------------------------------------------------


@dataclass
class AnalogDay:
    date: str
    similarity: float           # 0..1
    values: dict[str, float]
    outcome: float | None = None  # the user's logged result for that day, if any


def analog_days(
    history: list[dict],
    target: dict[str, float],
    keys: list[str],
    top: int = 5,
    min_similarity: float = 0.55,
) -> list[AnalogDay]:
    """Most similar past days by Euclidean distance in normalised factor space.

    Values are expected already normalised (anomalies in sigma, or index01), because a raw
    distance would let the variable with the biggest unit win. This is deliberately humble: it
    shows what the spot did before under similar conditions rather than pretending to predict.
    """
    if not history or not keys or not target:
        return []
    scored: list[AnalogDay] = []
    for row in history:
        vals = row.get("values") or {}
        diffs = []
        for k in keys:
            if k not in target or k not in vals or target[k] is None or vals[k] is None:
                continue
            diffs.append((float(target[k]) - float(vals[k])) ** 2)
        if len(diffs) < max(2, len(keys) // 3):
            continue
        rms = math.sqrt(sum(diffs) / len(diffs))
        sim = 1.0 / (1.0 + rms * rms)
        if sim >= min_similarity:
            scored.append(AnalogDay(str(row.get("date", "")), sim, dict(vals), row.get("outcome")))
    scored.sort(key=lambda a: -a.similarity)
    return scored[:top]
