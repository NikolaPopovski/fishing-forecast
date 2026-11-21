"""The spot context: one object holding everything the model knows about one fishing mark.

This is the seam between Home Assistant and the science. The integration's job is to fill this
from REST sensors and config entries; the model's job is to reason over it. Keeping the seam
explicit has three concrete benefits:

* the whole model runs offline from a plain dict of series (that is how the test suite works),
* a missing provider does not break the reasoning - the series is empty, the factor is dropped,
  and the confidence drops with it,
* one context = one spot, which matches Home Assistant's "one config entry = one fishing mark"
  rule, so a user with a lake mark and a sea mark gets two independent contexts.

Datetimes are deliberately naive-local or aware-local consistently: every series must use the
*spot's* local wall clock, because "06:00" and "golden hour" mean the spot's morning, not the
server's. The integration converts on the way in and the model never has to guess.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo, timezone
from zoneinfo import ZoneInfo

from . import astronomy as astro
from .climatology import Climatology
from .series import Sample, clean
from .water_temp import WaterBody

SERIES_NAMES = (
    "air_temp",
    "apparent_temp",
    "air_temp_max",
    "air_temp_min",
    "humidity",
    "dewpoint",
    "pressure",
    "wind_speed",
    "wind_gusts",
    "wind_dir",
    "cloud",
    "cloud_low",
    "cloud_mid",
    "cloud_high",
    "precip",
    "precip_prob",
    "rain",
    "showers",
    "uv_index",
    "shortwave",
    "cape",
    "visibility",
    "water_temp",
    "discharge",
    "soil_moisture",
    "pollen_birch",
)

SECTOR_ORDER = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def _sector(deg: float) -> str:
    idx = int(((deg + 22.5) % 360.0) // 45.0)
    return SECTOR_ORDER[idx % 8]


@dataclass
class ShelterGeometry:
    """Per-direction fetch/exposure of the bank, entered once at setup.

    "Which wind is bad here?" is the most local question in fishing: a south-westerly over a
    3 km fetch is a different spot from a lee shore with a dike in front of it. Asking the user
    for a rough exposure per compass sector (0 = completely sheltered, 1 = full fetch) keeps
    the model honest and the answer surprisingly easy for an angler to give.
    """

    exposure: dict[str, float] = field(default_factory=lambda: {s: 0.5 for s in SECTOR_ORDER})
    bank_aspect_deg: float | None = None   # the direction the bank faces, if the user knows it

    def shelteredness(self, wind_dir_deg: float | None) -> float | None:
        """0 = the wind is hammering this exact mark, 1 = it is fully leeward."""
        if wind_dir_deg is None:
            return None
        d = float(wind_dir_deg) % 360.0
        primary = self.exposure.get(_sector(d), 0.5)
        # a wind 45 deg off the nose still matters: blend with the neighbouring sectors
        left = self.exposure.get(_sector(d - 45.0), 0.5)
        right = self.exposure.get(_sector(d + 45.0), 0.5)
        exposure = 0.6 * primary + 0.2 * left + 0.2 * right
        if self.bank_aspect_deg is not None:
            # an offshore-facing bank with the wind on the face gets no protection at all
            off = abs(((d - float(self.bank_aspect_deg) + 180.0) % 360.0) - 180.0)
            if off < 90.0:
                exposure = max(exposure, 0.55 + 0.45 * (1.0 - off / 90.0))
        return max(0.0, min(1.0, 1.0 - exposure))

    def as_dict(self) -> dict:
        return {"exposure": dict(self.exposure), "bank_aspect_deg": self.bank_aspect_deg}

    @classmethod
    def from_dict(cls, payload: dict | None) -> "ShelterGeometry":
        if not payload:
            return cls()
        raw = payload.get("exposure") or {}
        exposure = {s: float(raw.get(s, 0.5)) for s in SECTOR_ORDER}
        aspect = payload.get("bank_aspect_deg")
        return cls(exposure=exposure, bank_aspect_deg=None if aspect is None else float(aspect))


@dataclass
class DayAstronomy:
    """Everything astronomical for one local day, computed once and reused by every hour."""

    day_start: datetime
    light: astro.DayLight
    night: astro.NightWindows
    golden: list[tuple[datetime, datetime, str]]
    moon_events: astro.BodyEvents
    solunar: list[astro.BodyEvents] = field(default_factory=list)

    def sun_altitude(self, when: datetime, lat: float, lon: float) -> float:
        return astro.sun_altitude(when, lat, lon)


@dataclass
class SpotContext:
    name: str
    lat: float
    lon: float
    elevation_m: float = 0.0
    tz_name: str = "UTC"
    spot_kind: str = "lake"                 # lake | reservoir | river | stream | estuary | coast
    tidal: bool = False
    tide_range_m: float = 0.0
    water_body: WaterBody = field(default_factory=WaterBody)
    catchment_response: float = 0.6
    pressure_source: str = "mslp"           # 'station' only for a real local barometer
    shelter: ShelterGeometry = field(default_factory=ShelterGeometry)
    clim: Climatology | None = None
    series: dict[str, list[Sample]] = field(default_factory=dict)
    diurnal_pressure_cycle: list[float | None] = field(default_factory=list)
    water_sensor: float | None = None
    water_sensor_age_hours: float = 0.0
    sea_surface_temp: float | None = None
    tz_offset_min: int | None = None        # fixed UTC offset; lets a spot run at a real local time with no tz db
    _clean: dict[str, list[Sample]] = field(default_factory=dict, repr=False)
    _astro: dict[str, DayAstronomy] = field(default_factory=dict, repr=False)
    _solunar: dict[tuple, list] = field(default_factory=dict, repr=False)
    #: scratch space for derived, expensive-to-build objects (e.g. the water-temperature model
    #: series). Cleared by invalidate(), so a refresh can never score against stale derivations.
    memo: dict[str, object] = field(default_factory=dict, repr=False)

    # -- series access ----------------------------------------------------------------

    def get(self, name: str) -> list[Sample]:
        """Cleaned, sorted series (cached). Missing -> empty list, never KeyError."""
        if name not in self._clean:
            self._clean[name] = clean(self.series.get(name, []))
        return self._clean[name]

    def invalidate(self) -> None:
        """Drop the caches after the integration replaces any series."""
        self._clean.clear()
        self._astro.clear()
        self._solunar.clear()
        self.memo.clear()

    @property
    def tz(self) -> tzinfo:
        if self.tz_offset_min is not None:      # explicit offset wins: deterministic, needs no tz database
            return timezone(timedelta(minutes=self.tz_offset_min))
        try:
            return ZoneInfo(self.tz_name)
        except Exception:                       # UTC fallback keeps the model usable with no tz db
            return timezone.utc

    def local_midnight(self, on: date | datetime) -> datetime:
        """Local midnight for the spot on the given day, tz-aware where possible."""
        if isinstance(on, datetime):
            on = on.date()
        naive = datetime.combine(on, time.min)
        tz = self.tz
        try:
            return naive.replace(tzinfo=tz)
        except Exception:                       # pragma: no cover - pathological zoneinfo
            return naive.replace(tzinfo=timezone.utc)

    # -- astronomy cache --------------------------------------------------------------

    def astronomy_for(self, when: datetime) -> DayAstronomy:
        """Day-light/night/solunar data for the local day containing *when* (computed once).

        The ephemeris work is the most expensive part of a scoring pass, and every hour of the
        forecast curve shares a day, so this cache is what makes a 48-hour curve cheap enough to
        compute on a refresh interval.
        """
        day = when.date()
        # the night belonging to a late-evening hour starts the previous evening, so the cache is
        # keyed on the calendar day and the night windows search forward from noon internally
        key = day.isoformat()
        cached = self._astro.get(key)
        if cached is not None:
            return cached
        # Always anchor on the spot's *local* midnight, whether or not the caller's `when` is naive:
        # series are in local wall-clock time, but astronomy reads a naive datetime as UTC, so a
        # naive anchor would silently shift "the day" by the UTC offset and pick the wrong
        # moonrise/moonset (first-event-in-the-calendar-day semantics).
        reference = self.local_midnight(day)
        light = astro.day_light(reference, self.lat, self.lon, tz=self.tz, elevation=self.elevation_m)
        night = astro.night_windows(reference, self.lat, self.lon, tz=self.tz, elevation=self.elevation_m)
        golden = astro.golden_windows(light)
        moon = astro.body_events(reference, self.lat, self.lon, moon=True, elevation=self.elevation_m, tz=self.tz)
        day_astro = DayAstronomy(day_start=reference, light=light, night=night, golden=golden, moon_events=moon)
        self._astro[key] = day_astro
        return day_astro

    def solunar_windows(self, when: datetime, days: int = 2) -> list:
        from . import solunar

        cache_key = (when.date().isoformat(), days)
        if cache_key in self._solunar:
            return self._solunar[cache_key]
        collected = []
        start = self.local_midnight(when.date())
        for i in range(days):
            day = start + timedelta(days=i)
            strength = astro.synodic_strength(day + timedelta(hours=12))
            for w in solunar.windows(day, self.lat, self.lon, elevation=self.elevation_m, strength=strength):
                if not any(abs((w.peak - other.peak).total_seconds()) < 900 for other in collected):
                    collected.append(w)
        result = sorted(collected, key=lambda w: w.start)
        self._solunar[cache_key] = result
        return result

    # -- summary ---------------------------------------------------------------------

    def summary(self) -> dict:
        """What the diagnostics sensor reports about the spot's data quality."""
        return {
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "elevation_m": self.elevation_m,
            "spot_kind": self.spot_kind,
            "water_body": self.water_body.kind,
            "tidal": self.tidal,
            "pressure_source": self.pressure_source,
            "series_days": {
                name: (len(vals) // 24 if vals else 0) for name, vals in ((n, self.get(n)) for n in SERIES_NAMES) if vals
            },
            "climatology": None if self.clim is None else self.clim.health(),
            "diurnal_pressure_cycle": bool(self.diurnal_pressure_cycle),
            "water_sensor": self.water_sensor,
            "sea_surface_temp": self.sea_surface_temp,
        }
