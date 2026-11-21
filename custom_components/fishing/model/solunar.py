"""Solunar theory: the tide-like component of fishing timing, modelled honestly.

The angler's rule of thumb (Knight/Bookbinder) is that fish feed in phases of the Moon: a major
period around the Moon's transit and its anti-transit, a minor period around its rise and set.
What that folklore actually measures is a combination of things that *are* physical:

* the same lunar gravitation that drives the tides, which in coastal and large-lake water moves
  water, oxygen and invertebrates (see `astronomy.tide_state`, which the model computes from the
  same ephemeris rather than trusting the folklore),
* the 12h25m lunar day, which shifts the good hours later by ~50 minutes daily - a pattern real
  catch records do show,
* the moonphase modulation of night light, which is a visual-predation effect, not a gravitation
  one, and which *helps* some species and *hinders* others.

The model therefore treats solunar timing as a *timing prior* (when to be on the water) with a
strength that depends on the moonphase and the distance of the Moon, and it keeps the mechanism
labelled: the card can say "moon overhead" versus "spring tide moving water" instead of blurring
them into mysticism. Windows are computed from real transit/rise/set events, so they are correct
at any latitude, and they degrade to the lunar-day clock where the horizon geometry has no
rise/set (polar).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import astronomy as astro
from . import response as rs

#: classic widths: major ~4 h centred on transit/anti-transit, minor ~2 h on rise/set
MAJOR_HALF_H = 2.0
MINOR_HALF_H = 1.0
#: how far outside a window the influence still reaches (fish do not switch off at a boundary)
MAJOR_TAIL_H = 2.5
MINOR_TAIL_H = 1.5

SOLUNAR_INTENSITY = rs.Curve(
    "solunar_intensity",
    [(0.0, -0.45), (0.25, -0.20), (0.45, 0.05), (0.65, 0.40), (0.85, 0.70), (1.0, 0.90)],
    kind="index01",
    notes="timing prior: being on the water at a peak is worth a lot; being there off-peak is not",
)


@dataclass
class SolunarWindow:
    start: datetime
    end: datetime
    peak: datetime
    kind: str                    # major | minor
    centre: str                  # transit | anti_transit | rise | set
    strength: float              # 0..1.35 (moonphase x distance)
    notes: list[str] = field(default_factory=list)

    @property
    def mid(self) -> datetime:
        return self.peak

    def contains(self, when: datetime) -> bool:
        return self.start <= when <= self.end

    @property
    def label(self) -> str:
        return {
            "transit": "moon overhead",
            "anti_transit": "moon underfoot",
            "rise": "moonrise",
            "set": "moonset",
        }[self.centre]

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "peak": self.peak.isoformat(),
            "kind": self.kind,
            "centre": self.centre,
            "label": self.label,
            "strength": round(self.strength, 3),
        }


def day_strength(when: datetime) -> float:
    """"Power of the day": 1 near new/full, low at the quarters, boosted near perigee."""
    return astro.synodic_strength(when)


def windows(
    day_start: datetime,
    lat: float,
    lon: float,
    *,
    elevation: float = 0.0,
    tz=None,
    strength: float | None = None,
) -> list[SolunarWindow]:
    """The four classic solunar windows belonging to the local day starting at *day_start*.

    Transit and anti-transit always exist (they are meridian crossings, horizon-independent), so
    the major windows are always defined. Rise/set can be missing for whole days at high
    latitudes; when they are, the minor windows are placed on the lunar-day clock from a nearby
    day rather than being dropped, because the *timing* signal is real even where the horizon is
    not.
    """
    strength = day_strength(day_start) if strength is None else strength
    out: list[SolunarWindow] = []
    ev = astro.body_events(day_start, lat, lon, moon=True, elevation=elevation, tz=tz)
    transit = ev.transit
    anti = ev.lower_transit
    lunar_day = timedelta(hours=astro.lunar_day_length_hours())

    if transit is None and anti is not None:
        transit = anti + lunar_day / 2
    if anti is None and transit is not None:
        anti = transit + lunar_day / 2

    for centre, peak, kind in (
        ("transit", transit, "major"),
        ("anti_transit", anti, "major"),
        ("rise", ev.rise, "minor"),
        ("set", ev.set, "minor"),
    ):
        if peak is None:
            continue
        half = MAJOR_HALF_H if kind == "major" else MINOR_HALF_H
        notes: list[str] = []
        if centre in ("rise", "set") and (ev.always_up or ev.always_down):
            notes.append("horizon does not produce a rise/set that day; window from the lunar clock")
        out.append(
            SolunarWindow(
                start=peak - timedelta(hours=half),
                end=peak + timedelta(hours=half),
                peak=peak,
                kind=kind,
                centre=centre,
                strength=strength,
                notes=notes,
            )
        )

    # Fill the missing minor windows from the lunar-day clock. Rise and set sit about a *quarter*
    # lunar day either side of the transit (the Moon climbs for ~6 h to the meridian and descends
    # for ~6 h after it); half a lunar day away is the anti-transit, i.e. another major window.
    have = {w.centre for w in out}
    if transit is not None:
        for centre, offset in (("rise", -0.25), ("set", 0.25)):
            if centre in have:
                continue
            peak = transit + lunar_day * offset
            out.append(
                SolunarWindow(
                    start=peak - timedelta(hours=MINOR_HALF_H),
                    end=peak + timedelta(hours=MINOR_HALF_H),
                    peak=peak,
                    kind="minor",
                    centre=centre,
                    strength=strength,
                    notes=["placed on the 12h25m lunar clock (no horizon event available)"],
                )
            )
    return sorted(out, key=lambda w: w.start)


def multi_day_windows(
    day_start: datetime,
    lat: float,
    lon: float,
    days: int = 2,
    *,
    elevation: float = 0.0,
    tz=None,
) -> list[SolunarWindow]:
    """Windows across several local days, de-duplicated, for the "next 48 h" view."""
    collected: list[SolunarWindow] = []
    for i in range(days):
        day = day_start + timedelta(days=i)
        strength = day_strength(day + timedelta(hours=12))
        for w in windows(day, lat, lon, elevation=elevation, tz=tz, strength=strength):
            if not any(abs((w.peak - other.peak).total_seconds()) < 900 for other in collected):
                collected.append(w)
    return sorted(collected, key=lambda w: w.start)


@dataclass
class SolunarState:
    when: datetime
    intensity: float            # 0..1 combined influence right now
    in_window: str | None       # 'major' | 'minor' | None
    label: str | None
    peak: datetime | None       # the peak being inside (or the nearest one just past)
    next_window: SolunarWindow | None
    prev_window: SolunarWindow | None
    hours_to_next: float | None
    day_strength: float
    windows: list[SolunarWindow] = field(default_factory=list)
    score: float = 0.0

    def as_dict(self) -> dict:
        return {
            "when": self.when.isoformat(),
            "intensity": round(self.intensity, 3),
            "in_window": self.in_window,
            "label": self.label,
            "peak": None if self.peak is None else self.peak.isoformat(),
            "next": None if self.next_window is None else self.next_window.as_dict(),
            "hours_to_next": None if self.hours_to_next is None else round(self.hours_to_next, 2),
            "day_strength": round(self.day_strength, 3),
            "score": self.score,
            "windows": [w.as_dict() for w in self.windows],
        }


def intensity_at(when: datetime, wins: list[SolunarWindow]) -> tuple[float, str | None, str | None, datetime | None]:
    """Blended solunar influence at an instant: peaks overlap, tails decay, nothing snaps.

    Two windows that overlap (major and minor coinciding, which happens near equinox full
    moons) combine to more than either alone - that is the real "exceptional period" claim, and
    it falls out of the geometry instead of needing a special rule.
    """
    best_inside: tuple[str, str, datetime] | None = None
    total = 0.0
    for w in wins:
        half = (MAJOR_HALF_H if w.kind == "major" else MINOR_HALF_H)
        tail = (MAJOR_TAIL_H if w.kind == "major" else MINOR_TAIL_H)
        distance = abs((when - w.peak).total_seconds()) / 3600.0
        if distance <= half:
            value = 1.0
        elif distance <= half + tail:
            value = rs.cosine_bump(distance, half, half + tail)
        else:
            continue
        value *= min(1.2, w.strength)
        total += value
        if distance <= half and (best_inside is None or w.kind == "major"):
            best_inside = (w.kind, w.label, w.peak)
    intensity = max(0.0, min(1.0, total))
    if best_inside is None:
        return intensity, None, None, None
    return intensity, best_inside[0], best_inside[1], best_inside[2]


def analyse(when: datetime, wins: list[SolunarWindow], strength: float | None = None) -> SolunarState:
    """Where the angler stands inside the solunar rhythm, plus the score the model uses."""
    intensity, kind, label, peak = intensity_at(when, wins)
    upcoming = [w for w in wins if w.start > when]
    past = [w for w in wins if w.end <= when]
    nxt = upcoming[0] if upcoming else None
    prv = past[-1] if past else None
    hours = None if nxt is None else (nxt.start - when).total_seconds() / 3600.0
    strength = day_strength(when) if strength is None else strength
    # a strong day lifts the whole curve; a weak (quarter-moon) day flattens the contrast, which
    # is what catch data supports: solunar is least useful as a predictor around the quarters
    scaled = intensity * (0.72 + 0.28 * min(1.2, strength))
    return SolunarState(
        when=when,
        intensity=intensity,
        in_window=kind,
        label=label,
        peak=peak,
        next_window=nxt,
        prev_window=prv,
        hours_to_next=hours,
        day_strength=strength,
        windows=wins,
        score=SOLUNAR_INTENSITY(scaled),
    )


def tide_bonus(tide_amplitude_m: float | None, spring_factor: float | None) -> float:
    """Extra credit for moving water at a solunar peak.

    In tidal water the solunar signal and the tidal signal are the same gravitational signal seen
    twice, and the fishing genuinely is better on a spring tide at a peak. In a landlocked lake
    this returns 0 and the peak is only a timing prior - which is the honest distinction.
    """
    if tide_amplitude_m is None or spring_factor is None:
        return 0.0
    return min(0.35, 0.10 * max(0.0, tide_amplitude_m) + 0.18 * max(0.0, spring_factor - 1.0))
