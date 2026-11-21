"""Self-contained solar / lunar astronomy for the fishing model.

Why hand-rolled instead of a library?  A Home Assistant custom component should install
with zero extra requirements, and the astronomy we need is narrow:

* sun & moon altitude/azimuth at an instant  -> golden light, moonlight, solunar geometry
* rise / set / meridian-transit times        -> solunar feeding windows, moonlight hours
* lunar phase, illumination, distance         -> spring/neap strength, "supermoon" effect
* equilibrium tidal forcing                   -> moving-water windows for salt water

Solar coordinates: Meeus, *Astronomical Algorithms*, ch. 25 (accuracy ~0.01 deg).
Lunar coordinates: the abridged ELP2000-82b theory (Meeus ch. 33 / the Astronomical
Almanac's abridged lunar theory), truncated to its main inequalities - evection,
variation, equation of centre, annual equation, parallactic inequality and the largest
latitude/radius terms.

The constituent amplitudes below are *fitted, not transcribed*: scripts/regen_lunar_series.py
least-squares every constituent of the truncated series against the full ch.33 theory (as
implemented by `astral`, the same reference Home Assistant core uses for its own sun/moon
entities) over 1950-2100.  The result agrees with the published main terms of Meeus tables
33.A-C to 3-4 significant digits (fitted -20905.58 km vs published -20905.355 km for the
dominant radial term), while removing the risk of mis-transcribing a table by hand.
Residual of the truncated series against the full theory:

    longitude  rms 0.7'   max 3.1'
    latitude   rms 0.5'   max 2.1'
    range      rms 60 km  max 232 km

Achieved end-to-end accuracy, asserted in tests/test_astronomy.py against a fixture
snapshot of the reference theory:

    sun rise / set / transit   < ~20 s
    moon rise / set            < ~1 min
    new / full moon instants   < ~10 min
which is far finer than the 1-2 hour feeding windows built on top of it.

All public functions take/return tz-aware datetimes.  Angles in degrees, distances km/AU.
No Home Assistant imports here, on purpose - that is what makes this testable in isolation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

J2000 = 2451545.0
SECS_PER_DAY = 86400.0
JULIAN_DAY_EPOCH = 2440587.5  # JD of 1970-01-01T00:00:00Z
SYNODIC_MONTH = 29.530588853   # new moon -> new moon
MEAN_MOON_DISTANCE = 385000.568  # km - the datum the abridged radius series is added to
PERIGEE_THRESHOLD_KM = 365000.0  # "supermoon" material

# Horizon altitudes for rise/set, already including refraction and semidiameter.
SUN_H0 = -0.833
MOON_H0 = 0.125
TWILIGHT_CIVIL = -6.0
TWILIGHT_NAUTICAL = -12.0
TWILIGHT_ASTRONOMICAL = -18.0

# Moon's apparent disc radius on the horizon is handled by MOON_H0 (mean value).

PHASES = (
    "new_moon",
    "waxing_crescent",
    "first_quarter",
    "waxing_gibbous",
    "full_moon",
    "waning_gibbous",
    "last_quarter",
    "waning_crescent",
)

PHASE_LABEL = {
    "new_moon": "New moon",
    "waxing_crescent": "Waxing crescent",
    "first_quarter": "First quarter",
    "waxing_gibbous": "Waxing gibbous",
    "full_moon": "Full moon",
    "waning_gibbous": "Waning gibbous",
    "last_quarter": "Last quarter",
    "waning_crescent": "Waning crescent",
}


# --------------------------------------------------------------------------------------
# time plumbing
# --------------------------------------------------------------------------------------


def to_jd(dt: datetime) -> float:
    """Julian Day (UT scale) from a tz-aware datetime."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() / SECS_PER_DAY + JULIAN_DAY_EPOCH


def from_jd(jd: float, tz=None) -> datetime:
    """Inverse of :func:`to_jd`.  tz-aware (UTC unless *tz* is given)."""
    dt = datetime.fromtimestamp(round((jd - JULIAN_DAY_EPOCH) * SECS_PER_DAY, 3), tz=timezone.utc)
    return dt if tz is None else dt.astimezone(tz)


def sin_deg(x: float) -> float:
    return math.sin(math.radians(x))


def cos_deg(x: float) -> float:
    return math.cos(math.radians(x))


def tan_deg(x: float) -> float:
    return math.tan(math.radians(x))


def atan2_deg(y: float, x: float) -> float:
    return math.degrees(math.atan2(y, x))


EARTH_RADIUS_M = 6356900.0  # the radius almanacs / Home Assistant's sun entity use


def horizon_dip_deg(elevation_m: float) -> float:
    """Depression of the visible horizon for an observer standing above sea level.

    Fishing from a dam wall, a groyne head or a clifftop pushes the horizon down, so the
    Sun and Moon appear to rise earlier and set later.  Geometrically that is
    acos(R / (R + h)) degrees - the same expression almanacs and Home Assistant's own sun
    entity use.  At 10 m it is 34', worth ~3 minutes of sunrise at mid latitudes, which is
    the same order as the solunar windows this model builds, so it is worth carrying.
    """
    if elevation_m <= 0.0:
        return 0.0
    return math.degrees(math.acos(EARTH_RADIUS_M / (EARTH_RADIUS_M + elevation_m)))


def asin_deg(x: float) -> float:
    return math.degrees(math.asin(max(-1.0, min(1.0, x))))


def wrap360(a: float) -> float:
    a = math.fmod(a, 360.0)
    return a + 360.0 if a < 0 else a


def wrap180(a: float) -> float:
    a = math.fmod(a, 360.0)
    if a <= -180.0:
        a += 360.0
    elif a > 180.0:
        a -= 360.0
    return a


# --------------------------------------------------------------------------------------
# ephemerides
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SunPos:
    ra: float        # right ascension, deg
    dec: float       # declination, deg
    lon: float       # apparent geocentric ecliptic longitude, deg
    dist_au: float
    eot_minutes: float  # equation of time


@dataclass(frozen=True)
class MoonPos:
    ra: float
    dec: float
    lon: float     # geocentric ecliptic longitude, deg
    lat: float     # ecliptic latitude, deg
    dist_km: float


def _series_sum(terms, args, fn):
    """Sum a lunar constituent series.  ``args`` is (D, M_sun, M_moon, F) in degrees."""
    d, m, mp, f = args
    total = 0.0
    for cd, cm, cmp_, cf, coef in terms:
        total += coef * fn(cd * d + cm * m + cmp_ * mp + cf * f)
    return total


# (D, M_sun, M_moon, F, amplitude) - provenance in the module docstring; regenerate them
# with scripts/regen_lunar_series.py.  Amplitudes are degrees for the longitude/latitude
# series (sine of the argument) and km for the radius series (cosine of the argument,
# added to MEAN_MOON_DISTANCE).
LUNAR_LON_TERMS = (
    (0, 0, 1, 0, 6.288491),      # evection - the largest lunar inequality
    (2, 0, -1, 0, 1.274096),     # variation
    (2, 0, 0, 0, 0.658315),      # variation, 2D part
    (0, 0, 2, 0, 0.213221),      # 2nd evection harmonic
    (0, -1, 0, 0, 0.185940),     # annual equation (Sun's mean anomaly)
    (0, 0, 0, -2, 0.114092),     # nodical term
    (2, 0, -2, 0, 0.058471),     # evection 2nd order
    (2, -1, -1, 0, 0.056971),
    (2, 0, 1, 0, 0.053316),
    (2, -1, 0, 0, 0.046478),
    (0, -1, 1, 0, 0.041281),
    (-1, 0, 0, 0, 0.034921),     # parallactic inequality
    (0, -1, -1, 0, 0.030889),
    (2, 0, 0, -2, 0.015516),
    (0, 0, -1, -2, 0.012314),
    (0, 0, 1, -2, 0.011263),
    (4, 0, -1, 0, 0.010619),
    (0, 0, 3, 0, 0.010268),
    (-2, -1, 1, 0, 0.008780),
    (-2, -1, 0, 0, 0.007225),
)

LUNAR_LAT_TERMS = (
    (0, 0, 0, 1, 5.127795),
    (0, 0, 1, 1, 0.280732),
    (0, 0, 1, -1, 0.277322),
    (2, 0, 0, -1, 0.173338),
    (2, 0, -1, 1, 0.055664),
    (2, 0, -1, -1, 0.046394),
    (2, 0, 0, 1, 0.032578),
    (0, 0, 2, 1, 0.017037),
    (2, 0, 1, -1, 0.009187),
    (0, 0, 2, -1, 0.008800),
)

LUNAR_RAD_TERMS = (
    (0, 0, 1, 0, -20905.580),    # orbital equation of the centre (perigee at M_moon = 0)
    (2, 0, -1, 0, -3698.597),
    (2, 0, 0, 0, -2956.568),
    (0, 0, 2, 0, -567.686),
    (2, 0, -2, 0, 246.633),
    (2, -1, 0, 0, -207.305),
    (2, 0, 1, 0, -171.845),
    (2, -1, -1, 0, -149.349),
    (0, 1, -1, 0, -128.805),
    (1, 0, 0, 0, 110.184),
    (0, 1, 1, 0, 106.790),
    (0, 0, 1, -2, 78.919),
)


def lunar_elements(jd: float) -> tuple[float, float, float, float, float, float]:
    """(L', D, M_sun, M_moon, F, T) - the fundamental arguments of the abridged theory.

    T is Julian centuries TT from J2000; angles are returned unwrapped in degrees.
    """
    t = (jd - J2000) / 36525.0
    lp = 218.3164477 + 481267.88123421 * t - 0.0015786 * t * t + t**3 / 538841.0
    d = 297.8501921 + 445267.1114034 * t - 0.0018819 * t * t + t**3 / 545868.0
    m = 357.5291092 + 35999.0502909 * t - 0.0001536 * t * t + t**3 / 24490000.0
    mp = 134.9633964 + 477198.8675055 * t + 0.0087414 * t * t + t**3 / 696940.0
    f = 93.2720950 + 483202.0175233 * t - 0.0036539 * t * t - t**3 / 3526000.0
    return wrap360(lp), wrap360(d), wrap360(m), wrap360(mp), wrap360(f), t


def ecliptic_obliquity(jd: float) -> float:
    t = (jd - J2000) / 36525.0
    omega = 125.04452 - 1934.136261 * t
    return 23.439291 - 0.0130042 * t + 0.00000016 * t * t - 0.0000009 * t**3 + 0.00256 * cos_deg(omega)


def sun_position(jd: float) -> SunPos:
    """Apparent geocentric solar coordinates (Meeus ch. 25, ~0.01 deg)."""
    t = (jd - J2000) / 36525.0
    l0 = wrap360(280.466459918 + 36000.76983 * t + 0.0003032 * t * t)
    m = wrap360(357.52910 + 35999.05030 * t - 0.0001559 * t * t - 0.00000046 * t**3)
    c = (
        (1.914600 - 0.004817 * t - 0.000014 * t * t) * sin_deg(m)
        + (0.019993 - 0.000101 * t) * sin_deg(2 * m)
        + 0.000289 * sin_deg(3 * m)
    )
    true_long = l0 + c
    true_anom = m + c
    ecc = 0.016708634 - 0.000042037 * t - 0.0000001267 * t * t
    r = 1.000001018 * (1.0 - ecc * cos_deg(true_anom)) / math.sqrt(1.0 - ecc * ecc)
    omega = 125.04 - 1934.136 * t
    lon = wrap360(true_long - 0.00569 - 0.00478 * sin_deg(omega))
    eps = ecliptic_obliquity(jd)
    ra = wrap360(atan2_deg(cos_deg(eps) * sin_deg(lon), cos_deg(lon)))
    dec = asin_deg(sin_deg(eps) * sin_deg(lon))
    y = tan_deg(eps / 2.0) ** 2
    eot_rad = (
        y * sin_deg(2 * l0)
        - 2.0 * ecc * sin_deg(m)
        + 4.0 * ecc * y * sin_deg(m) * cos_deg(2 * l0)
        - 0.5 * y * y * sin_deg(4 * l0)
        - 1.25 * ecc * ecc * sin_deg(2 * m)
    )
    return SunPos(ra=ra, dec=dec, lon=lon, dist_au=r, eot_minutes=math.degrees(eot_rad) * 4.0)


def moon_position(jd: float) -> MoonPos:
    """Geocentric lunar coordinates from the truncated ELP2000-82b series.

    Accuracy over 1950-2100: <4' in longitude, <2' in latitude, <250 km in range
    (see the module docstring; asserted by tests/test_astronomy.py).
    """
    lp, d, m, mp, f, t = lunar_elements(jd)
    args = (d, m, mp, f)

    dl = _series_sum(LUNAR_LON_TERMS, args, sin_deg)       # deg, added to L'
    db = _series_sum(LUNAR_LAT_TERMS, args, sin_deg)       # deg
    dr_km = _series_sum(LUNAR_RAD_TERMS, args, cos_deg)    # km, added to the datum

    moon_lon = wrap360(lp + dl)
    moon_lat = db
    dist = MEAN_MOON_DISTANCE + dr_km

    eps = ecliptic_obliquity(jd)
    cl, sl = cos_deg(moon_lon), sin_deg(moon_lon)
    cb, sb = cos_deg(moon_lat), sin_deg(moon_lat)
    ce, se = cos_deg(eps), sin_deg(eps)
    # Rotate the geocentric ecliptic vector (cl*cb, sl*cb, sb) about the x-axis by the true
    # obliquity: y_eq = cb*sl*ce - sb*se, z_eq = cb*sl*se + sb*ce.
    ra = wrap360(atan2_deg(cb * sl * ce - sb * se, cb * cl))
    dec = asin_deg(cb * sl * se + sb * ce)
    return MoonPos(ra=ra, dec=dec, lon=moon_lon, lat=moon_lat, dist_km=dist)


def elongation(jd: float) -> float:
    """Sun->Moon ecliptic elongation: 0 deg at new moon, 180 deg at full moon."""
    return wrap360(moon_position(jd).lon - sun_position(jd).lon)


@dataclass(frozen=True)
class MoonPhaseInfo:
    phase_name: str
    phase_label: str
    illumination: float      # 0..1 fraction of the disc lit
    elongation: float        # deg
    age_days: float          # days since last new moon
    waxing: bool
    distance_km: float
    parallax_factor: float   # (mean/actual)^3: >1 near perigee -> stronger lunar pull
    perigee_syzygy: bool     # perigee coinciding with new/full: the "supermoon" case
    next_new_moon: datetime | None = None
    next_full_moon: datetime | None = None


def moon_phase_info(jd: float, *, with_events: bool = False) -> MoonPhaseInfo:
    el = elongation(jd)
    illum = (1.0 - cos_deg(el)) / 2.0
    info = moon_position(jd)
    parallax = (MEAN_MOON_DISTANCE / info.dist_km) ** 3 if info.dist_km else 1.0
    idx = int(((el + 22.5) / 45.0) % 8)
    near_syzygy = illum > 0.97 or illum < 0.03
    return MoonPhaseInfo(
        phase_name=PHASES[idx],
        phase_label=PHASE_LABEL[PHASES[idx]],
        illumination=illum,
        elongation=el,
        age_days=el / 360.0 * SYNODIC_MONTH,
        waxing=sin_deg(el) >= 0,
        distance_km=info.dist_km,
        parallax_factor=parallax,
        perigee_syzygy=near_syzygy and info.dist_km < PERIGEE_THRESHOLD_KM,
        next_new_moon=find_phase_crossing("new", from_jd(jd)) if with_events else None,
        next_full_moon=find_phase_crossing("full", from_jd(jd)) if with_events else None,
    )


def find_phase_crossing(kind: str, start: datetime, horizon_days: float = 45.0) -> datetime | None:
    """Next exact new (*kind="new"*) or full moon after *start*."""
    target = 0.0 if kind == "new" else 180.0
    jd0 = to_jd(start)
    step = 0.2
    prev = wrap180(elongation(jd0) - target)
    jd = jd0
    while jd < jd0 + horizon_days:
        jd += step
        cur = wrap180(elongation(jd) - target)
        if prev < 0.0 <= cur:
            lo, hi = jd - step, jd
            for _ in range(50):
                mid = (lo + hi) / 2.0
                if wrap180(elongation(mid) - target) < 0.0:
                    lo = mid
                else:
                    hi = mid
            return from_jd((lo + hi) / 2.0)
        prev = cur
    return None


# --------------------------------------------------------------------------------------
# observer geometry
# --------------------------------------------------------------------------------------


def gmst(jd: float) -> float:
    """Greenwich mean sidereal time, degrees (Meeus 12.4)."""
    t = (jd - J2000) / 36525.0
    return wrap360(
        280.46061837
        + 360.98564736629 * (jd - J2000)
        + 0.000387933 * t * t
        - t**3 / 38710000.0
    )


def local_sidereal_time(jd: float, lon_east: float) -> float:
    return wrap360(gmst(jd) + lon_east)


def horizontal(ra: float, dec: float, lat: float, lst_deg: float) -> tuple[float, float]:
    """Altitude, azimuth (deg from north, clockwise) for equatorial coordinates."""
    h = wrap180(lst_deg - ra)
    alt = asin_deg(sin_deg(dec) * sin_deg(lat) + cos_deg(dec) * cos_deg(lat) * cos_deg(h))
    az = wrap360(
        atan2_deg(
            -cos_deg(dec) * sin_deg(h),
            sin_deg(dec) * cos_deg(lat) - cos_deg(dec) * sin_deg(lat) * cos_deg(h),
        )
    )
    return alt, az


def sun_altitude(dt: datetime, lat: float, lon: float) -> float:
    jd = to_jd(dt)
    s = sun_position(jd)
    return horizontal(s.ra, s.dec, lat, local_sidereal_time(jd, lon))[0]


def moon_altitude(dt: datetime, lat: float, lon: float) -> float:
    jd = to_jd(dt)
    m = moon_position(jd)
    return horizontal(m.ra, m.dec, lat, local_sidereal_time(jd, lon))[0]


# --------------------------------------------------------------------------------------
# event finding: scan then bisect.  Robust at polar latitudes and for ~24h50m lunar days.
# --------------------------------------------------------------------------------------


def _bisect(func, lo: float, hi: float) -> float:
    flo = func(lo)
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if hi - lo < 1e-8:
            break
        fmid = func(mid)
        if (flo < 0.0) == (fmid < 0.0):
            lo, flo = mid, fmid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _crossings(func, jd0: float, jd1: float, step: float, near: float = 0.4) -> list[float]:
    """Increasing zero crossings of *func* in [jd0, jd1].

    A fixed-step scan misses excursions shorter than the step.  That is not hypothetical:
    at polar latitudes around the solstices the Sun grazes the horizon for tens of seconds
    near local midnight, and a missed graze shifts "today's sunset" by a whole day in the
    UI.  So whenever two consecutive samples sit on the same side of zero yet both sit
    within *near* degrees of it, the interval is re-scanned at a 30x finer step first.
    Only intervals that are already close to the horizon pay that cost.
    """
    out: list[float] = []

    def scan(a: float, b: float, h: float) -> None:
        prev_jd, prev = a, func(a)
        jd = a
        while jd < b:
            jd = min(b, jd + h)
            cur = func(jd)
            if prev < 0.0 <= cur:
                out.append(_bisect(func, prev_jd, jd))
            elif prev * cur > 0.0 and min(abs(prev), abs(cur)) < near and h > (step / 30.0):
                scan(prev_jd, jd, step / 30.0)
            prev, prev_jd = cur, jd

    scan(jd0, jd1, step)
    return out


def _alt_of(ra_dec, jd: float, lat: float, lon: float) -> float:
    ra, dec = ra_dec(jd)
    return horizontal(ra, dec, lat, local_sidereal_time(jd, lon))[0]


def _alt_crossings(ra_dec, jd0: float, jd1: float, lat: float, lon: float, h0: float, step: float):
    return _crossings(lambda jd: _alt_of(ra_dec, jd, lat, lon) - h0, jd0, jd1, step)


def _set_crossings(ra_dec, jd0: float, jd1: float, lat: float, lon: float, h0: float, step: float):
    return _crossings(lambda jd: h0 - _alt_of(ra_dec, jd, lat, lon), jd0, jd1, step)


def _hour_angle_crossing(ra_dec, jd0: float, jd1: float, lon: float, offset: float, step: float):
    """Meridian passage: hour angle (LST - RA - offset) crossing zero upwards."""
    return _crossings(lambda jd: wrap180(local_sidereal_time(jd, lon) - ra_dec(jd)[0] - offset), jd0, jd1, step)


def _sun_ra_dec(jd: float) -> tuple[float, float]:
    s = sun_position(jd)
    return s.ra, s.dec


def _moon_ra_dec(jd: float) -> tuple[float, float]:
    m = moon_position(jd)
    return m.ra, m.dec


@dataclass
class BodyEvents:
    """Rise / set / meridian passage near one observer day (local-midnight anchored)."""

    date: str
    rise: datetime | None = None
    set: datetime | None = None
    transit: datetime | None = None          # upper culmination ("moon over head")
    lower_transit: datetime | None = None    # under foot
    always_up: bool = False
    always_down: bool = False


def _first_in_day(vals: list[float], day_start: datetime, tz) -> datetime | None:
    """The first event that falls inside the observer's own calendar day.

    Almanacs assign a rise/set to the local calendar day in which it happens.  Choosing
    "whichever crossing is nearest to local noon" - a tempting shortcut - mis-hands days at
    negative UTC offsets in summer, where the day's own sunset falls after the *next*
    midnight, so the previous evening's set looks nearer to noon and "today's sunset" ends
    up 24 h early.  Restricting to the day itself also makes polar day / polar night come
    out as "no event" instead of an event borrowed from a neighbouring day.
    """
    lo = to_jd(day_start)
    inside = [jd for jd in vals if lo <= jd < lo + 1.0]
    return from_jd(min(inside), tz) if inside else None


def _jd_in_day(vals: list[float], day_start: datetime) -> float | None:
    """First crossing jd inside the observer's own calendar day, else None."""
    lo = to_jd(day_start)
    inside = [jd for jd in vals if lo <= jd < lo + 1.0]
    return min(inside) if inside else None


def _first_in_day(vals: list[float], day_start: datetime, tz) -> datetime | None:
    jd = _jd_in_day(vals, day_start)
    return None if jd is None else from_jd(jd, tz)


def _jd_bracketing(vals: list[float], anchor_jd: float, before: bool, slack: float = 0.75) -> float | None:
    """The crossing closest to *anchor_jd* on the requested side, within *slack* days.

    This picks the rise/set that actually *bracket* a day's solar noon, which is the only
    convention that stays coherent everywhere:

    * at 64 N in June the Sun is up from 02:54 to 00:02 the next morning, so "the first set
      inside the calendar day" (00:00:38) would sit *before* the day's sunrise and make day
      length negative; pairing around noon instead gives a real 21 h day that straddles
      midnight, exactly how high-latitude almanacs describe it;
    * a midsummer night at 52 N has its astronomical dusk a few minutes after midnight - it
      belongs to the night that started on this date, and is reported here even though it
      lands on the next calendar date.

    Nights that wrap past midnight are additionally available as one ordered sequence from
    night_windows().
    """
    if before:
        cand = [jd for jd in vals if anchor_jd - slack <= jd <= anchor_jd]
        return max(cand) if cand else None
    cand = [jd for jd in vals if anchor_jd <= jd <= anchor_jd + slack]
    return min(cand) if cand else None


def _jd_after(vals: list[float], anchor_jd: float) -> float | None:
    later = [jd for jd in vals if jd > anchor_jd]
    return min(later) if later else None


def body_events(
    day_start: datetime,
    lat: float,
    lon: float,
    *,
    moon: bool = False,
    h0: float | None = None,
    elevation: float = 0.0,
    tz=None,
) -> BodyEvents:
    """Events for the day starting at *day_start* (local midnight, tz-aware).

    The search window is deliberately one day wide plus a margin: the Moon's day is
    ~24h50m, so events spill across midnight constantly, and near the poles bodies can
    stay up (or down) for days.
    """
    func = _moon_ra_dec if moon else _sun_ra_dec
    horizon = (h0 if h0 is not None else (MOON_H0 if moon else SUN_H0)) - horizon_dip_deg(elevation)
    jd0 = to_jd(day_start - timedelta(hours=6))
    jd1 = to_jd(day_start + timedelta(hours=30))
    step = 3.0 / 1440.0 if moon else 4.0 / 1440.0

    rises = _alt_crossings(func, jd0, jd1, lat, lon, horizon, step)
    sets = _set_crossings(func, jd0, jd1, lat, lon, horizon, step)
    transits = _hour_angle_crossing(func, jd0, jd1, lon, 0.0, step)
    lowers = _hour_angle_crossing(func, jd0, jd1, lon, 180.0, step)

    pick = lambda vals: _first_in_day(vals, day_start, tz)  # noqa: E731

    sampled = [
        _alt_of(func, to_jd(day_start + timedelta(hours=h)), lat, lon) for h in (0, 3, 6, 9, 12, 15, 18, 21)
    ]
    ev = BodyEvents(date=day_start.strftime("%Y-%m-%d"))
    if not rises and not sets:
        if min(sampled) > horizon:
            ev.always_up = True
        elif max(sampled) < horizon:
            ev.always_down = True
    ev.rise = pick(rises)
    ev.set = pick(sets)
    ev.transit = pick(transits)
    ev.lower_transit = pick(lowers)
    if ev.transit is None and ev.lower_transit is not None:
        half = 12.42 if moon else 12.0
        ev.transit = ev.lower_transit + timedelta(hours=half)
    return ev


@dataclass
class DayLight:
    """Solar timing for one observer day."""

    date: str
    sunrise: datetime | None
    sunset: datetime | None
    solar_noon: datetime | None
    dawn: datetime | None                # civil twilight begins (sun -6 deg)
    dusk: datetime | None                # civil twilight ends
    nautical_dawn: datetime | None = None
    nautical_dusk: datetime | None = None
    astronomical_dawn: datetime | None = None
    astronomical_dusk: datetime | None = None
    polar_day: bool = False
    polar_night: bool = False

    @property
    def day_length_min(self) -> float | None:
        if self.sunrise and self.sunset:
            return (self.sunset - self.sunrise).total_seconds() / 60.0
        return None


def day_light(day_start: datetime, lat: float, lon: float, tz=None, elevation: float = 0.0) -> DayLight:
    """Sunrise/sunset/twilight for the observer's calendar day starting at *day_start*.

    *day_start* must be local midnight in the observer's own timezone; events are reported
    in that timezone.  Each event is the crossing that brackets this day's solar noon, which
    keeps a coherent day even where the Sun grazes the horizon around local midnight; if no
    such crossing exists (polar day/night) the field is None.  *elevation* is the observer's
    height above sea level in metres.
    """
    jd0 = to_jd(day_start - timedelta(hours=6))
    jd1 = to_jd(day_start + timedelta(hours=30))
    h0 = SUN_H0 - horizon_dip_deg(elevation)
    dip = horizon_dip_deg(elevation)
    centre = to_jd(day_start + timedelta(hours=12))
    noon_jd = _jd_in_day(_hour_angle_crossing(_sun_ra_dec, jd0, jd1, lon, 0.0, 4.0 / 1440.0), day_start)
    anchor = noon_jd if noon_jd is not None else centre

    def pick(cross: list[float], before: bool) -> datetime | None:
        jd = _jd_bracketing(cross, anchor, before)
        if jd is None:
            jd = _jd_in_day(cross, day_start)
        return None if jd is None else from_jd(jd, tz)

    out = DayLight(
        date=day_start.strftime("%Y-%m-%d"),
        sunrise=pick(_alt_crossings(_sun_ra_dec, jd0, jd1, lat, lon, h0, 4.0 / 1440.0), True),
        sunset=pick(_set_crossings(_sun_ra_dec, jd0, jd1, lat, lon, h0, 4.0 / 1440.0), False),
        solar_noon=from_jd(anchor, tz),
        dawn=None,
        dusk=None,
    )
    for level, up_attr, down_attr in (
        (TWILIGHT_CIVIL, "dawn", "dusk"),
        (TWILIGHT_NAUTICAL, "nautical_dawn", "nautical_dusk"),
        (TWILIGHT_ASTRONOMICAL, "astronomical_dawn", "astronomical_dusk"),
    ):
        level_eff = level - dip
        step = 6.0 / 1440.0
        setattr(
            out,
            up_attr,
            pick(_alt_crossings(_sun_ra_dec, jd0, jd1, lat, lon, level_eff, step), True),
        )
        setattr(
            out,
            down_attr,
            pick(_set_crossings(_sun_ra_dec, jd0, jd1, lat, lon, level_eff, step), False),
        )
    if out.sunrise is None and out.sunset is None:
        alt_noon = _alt_of(_sun_ra_dec, centre, lat, lon)
        out.polar_day = alt_noon > h0
        out.polar_night = not out.polar_day
    return out


@dataclass
class NightWindows:
    """The dusk-to-dawn structure of the night that *begins* on a given local day.

    Per-day almanac fields cannot express a night that wraps past midnight: at 52 N in May,
    civil dusk falls on the 7th while nautical dusk belongs to the 8th, so a
    DayLight.dusk < DayLight.nautical_dusk comparison is meaningless.  Searching forward
    from this day's solar noon instead puts the whole night in one order, which is what the
    fishing model needs for "tonight" windows.
    """

    date: str
    sunset: datetime | None
    dusk: datetime | None
    nautical_dusk: datetime | None
    astronomical_dusk: datetime | None
    sunrise: datetime | None            # the *next* morning
    dawn: datetime | None
    nautical_dawn: datetime | None
    astronomical_dawn: datetime | None
    polar_day: bool = False
    polar_night: bool = False

    @property
    def dark_hours(self) -> float | None:
        """Hours of true darkness (astronomical dusk to astronomical dawn) on this night."""
        if self.astronomical_dusk and self.astronomical_dawn:
            return (self.astronomical_dawn - self.astronomical_dusk).total_seconds() / 3600.0
        if self.polar_day:
            return 0.0
        if self.polar_night:
            return 24.0
        return None

    def window(self, kind: str) -> tuple[datetime, datetime] | None:
        """Named light windows of the night: (start, end), tolerant of missing twilight.

        High-latitude summers have no civil dusk at all (the Sun only skims the horizon), so
        the golden hour falls back to a fixed span around the sunset rather than vanishing -
        which is also how anglers talk about it there ("all night golden hour").
        """
        if kind == "golden_pm":
            if self.sunset:
                end = self.dusk or (self.sunset + timedelta(minutes=45))
                return (self.sunset - timedelta(minutes=45), end)
            return None
        if kind == "golden_am":
            if self.sunrise:
                start = self.dawn or (self.sunrise - timedelta(minutes=45))
                return (start, self.sunrise + timedelta(minutes=45))
            return None
        if kind == "dark":
            start = self.astronomical_dusk or self.nautical_dusk or self.dusk or self.sunset
            end = self.astronomical_dawn or self.nautical_dawn or self.dawn or self.sunrise
            return (start, end) if start and end else None
        return None


def night_windows(day_start: datetime, lat: float, lon: float, tz=None, elevation: float = 0.0) -> NightWindows:
    """Night structure for the night starting at sunset on the local day beginning *day_start*."""
    dip = horizon_dip_deg(elevation)
    day = day_light(day_start, lat, lon, tz=tz, elevation=elevation)
    noon = day.solar_noon
    jd_noon = to_jd(noon) if noon is not None else to_jd(day_start + timedelta(hours=12))
    jd1 = jd_noon + 0.9  # evening crossings live within ~22 h of solar noon, in JD units
    step = 6.0 / 1440.0

    def down(level: float) -> datetime | None:
        jd = _jd_after(_set_crossings(_sun_ra_dec, jd_noon, jd1, lat, lon, level - dip, step), jd_noon)
        return None if jd is None else from_jd(jd, tz)

    def up(level: float, after_jd: float) -> datetime | None:
        # bounded by this night's *next* solar noon: a night whose Sun never reaches a
        # twilight level must report None, not borrow the following night's dawn
        horizon_jd = min(after_jd + 0.65, jd_noon + 1.02)
        if horizon_jd <= after_jd:
            return None
        vals = _alt_crossings(_sun_ra_dec, after_jd, horizon_jd, lat, lon, level - dip, step)
        jd = _jd_after(vals, after_jd)
        return None if jd is None else from_jd(jd, tz)

    out = NightWindows(
        date=day_start.strftime("%Y-%m-%d"),
        sunset=down(SUN_H0),
        dusk=down(TWILIGHT_CIVIL),
        nautical_dusk=down(TWILIGHT_NAUTICAL),
        astronomical_dusk=down(TWILIGHT_ASTRONOMICAL),
        sunrise=None,
        dawn=None,
        nautical_dawn=None,
        astronomical_dawn=None,
    )
    evening = [t for t in (out.sunset, out.dusk, out.nautical_dusk, out.astronomical_dusk) if t]
    anchor = to_jd(max(evening)) if evening else jd_noon + 6.0 / 24.0
    out.sunrise = up(SUN_H0, anchor)
    out.dawn = up(TWILIGHT_CIVIL, anchor)
    out.nautical_dawn = up(TWILIGHT_NAUTICAL, anchor)
    out.astronomical_dawn = up(TWILIGHT_ASTRONOMICAL, anchor)
    if not evening and out.sunrise is None:
        out.polar_day = day.polar_day
        out.polar_night = day.polar_night
    if day.polar_day:
        out.polar_day = True
    if day.polar_night:
        out.polar_night = True
    return out


def golden_windows(light: DayLight) -> list[tuple[datetime, datetime, str]]:
    """The two low-light windows every angler already lives by."""
    out: list[tuple[datetime, datetime, str]] = []
    if light.dawn and light.sunrise:
        out.append((light.dawn, light.sunrise + timedelta(minutes=45), "sunrise"))
    if light.sunset and light.dusk:
        out.append((light.sunset - timedelta(minutes=45), light.dusk, "sunset"))
    return out


# --------------------------------------------------------------------------------------
# equilibrium tidal forcing
# --------------------------------------------------------------------------------------

SOLAR_TIDE_RATIO = 0.46  # solar/lunar semi-diurnal forcing amplitude at 1 AU


@dataclass
class TideState:
    level: float            # -1 low water .. +1 high water (equilibrium semi-diurnal)
    range_index: float      # 0 neap .. 1 spring
    forcing: float          # (mean/moon distance)^3, >1 near perigee
    moon_hour_angle: float
    label: str              # rising | falling | high | low
    lunar_constituent: float
    solar_constituent: float
    perigee_spring: bool


def _tide_components(jd: float, lon: float) -> tuple[float, float, float, float, float, float]:
    m = moon_position(jd)
    s = sun_position(jd)
    lst = local_sidereal_time(jd, lon)
    hm = wrap180(lst - m.ra)
    hs = wrap180(lst - s.ra)
    forcing = (MEAN_MOON_DISTANCE / m.dist_km) ** 3 if m.dist_km else 1.0
    a_m = forcing * cos_deg(m.dec) ** 2
    a_s = SOLAR_TIDE_RATIO / max(s.dist_au, 0.9) ** 3 * cos_deg(s.dec) ** 2
    return a_m, a_s, hm, hs, forcing, m.dist_km


def tide_state(dt: datetime, lon: float) -> TideState:
    """Astronomical (equilibrium) tide indicator - deliberately coastline-free.

    This is *not* a tide forecast: amphidromic points, shelf resonance and basin seiches
    can shift real high water by hours.  What it does give you is the part no forecast
    service tells you either: how hard the Moon is pulling today (spring/neap, perigee)
    and roughly when the water should be moving - enough to place salt-water feeding
    windows when the user has no tide sensor in HA.
    """
    jd = to_jd(dt)
    a_m, a_s, hm, hs, forcing, dist = _tide_components(jd, lon)
    z = a_m * cos_deg(2 * hm) + a_s * cos_deg(2 * hs)
    amp = a_m + a_s
    # Spring/neap = amplitude of the lunar+solar beat, normalised.
    beat = math.hypot(a_m + a_s * cos_deg(2 * (hs - hm)), a_s * sin_deg(2 * (hs - hm)))
    jd2 = jd + 30.0 / 1440.0
    a_m2, a_s2, hm2, hs2, _, _ = _tide_components(jd2, lon)
    z2 = a_m2 * cos_deg(2 * hm2) + a_s2 * cos_deg(2 * hs2)
    if abs(z2 - z) < 0.02 * amp:
        label = "high" if z > 0 else "low"
    else:
        label = "rising" if z2 > z else "falling"
    return TideState(
        level=max(-1.0, min(1.0, z / amp if amp else 0.0)),
        range_index=min(1.0, beat / amp if amp else 0.0),
        forcing=forcing,
        moon_hour_angle=hm,
        label=label,
        lunar_constituent=a_m,
        solar_constituent=a_s,
        perigee_spring=forcing > 1.10 and beat / amp > 0.85 if amp else False,
    )


def tide_extremes(dt: datetime, lon: float, horizon_hours: float = 26.0) -> list[datetime]:
    """Approximate high/low water instants from the equilibrium tide."""
    def level(jd: float) -> float:
        a_m, a_s, hm, hs, _, _ = _tide_components(jd, lon)
        return a_m * cos_deg(2 * hm) + a_s * cos_deg(2 * hs)

    jd0 = to_jd(dt)
    ext: list[datetime] = []
    prev_slope: float | None = None
    step = 0.03
    jd = jd0
    while jd < jd0 + horizon_hours / 24.0:
        slope = level(jd + 0.004) - level(jd - 0.004)
        if prev_slope is not None and (prev_slope < 0.0 <= slope or prev_slope >= 0.0 > slope):
            lo, hi = jd - step, jd
            rising_side = prev_slope < 0.0
            for _ in range(30):
                mid = (lo + hi) / 2.0
                mid_slope = level(mid + 0.002) - level(mid - 0.002)
                if (mid_slope < 0.0) == rising_side:
                    lo = mid
                else:
                    hi = mid
            ext.append(from_jd((lo + hi) / 2.0))
        prev_slope = slope
        jd += step
    return ext


# --------------------------------------------------------------------------------------
# derived light / lunar strength
# --------------------------------------------------------------------------------------


def moonshine(dt: datetime, lat: float, lon: float) -> float:
    """Available moonlight 0..1: illumination x altitude, suppressed by daylight."""
    info = moon_phase_info(to_jd(dt))
    alt = moon_altitude(dt, lat, lon)
    if alt <= 0:
        return 0.0
    sun = sun_altitude(dt, lat, lon)
    sky = 1.0 if sun < -6.0 else max(0.0, 1.0 - (sun + 6.0) / 24.0)
    return info.illumination * sky * min(1.0, alt / 40.0)


def synodic_strength(dt: datetime) -> float:
    """Solunar "power of the day": 1 at new/full, ~0 at the quarters, perigee-boosted."""
    info = moon_phase_info(to_jd(dt))
    base = abs(cos_deg(info.elongation))
    return min(1.35, base * (0.55 + 0.45 * info.parallax_factor))


def lunar_day_length_hours() -> float:
    return 24.0 + 50.47 / 60.0
