"""Reasons and tactics: turn an Evaluation into things an angler can read and act on.

`scoring.py` answers "how good, and how sure?". This module answers "why, and what do I do about
it?" - the two things a bare number never tells you. It deliberately stays small and boring:

* **Reasons** are the one-sentence `why` of the factors that moved the score most, with their
  numbers in them, so the card can say "water is 14.8 degC, 1.2 below the pike optimum" instead of
  "score 6.1".
* **Tactics** come from each species profile's own vocabulary (`SpeciesProfile.tactics`, keyed by a
  named *condition* such as `falling_barometer` or `coloured`). The profile says what to do when a
  condition holds; this module decides which conditions hold right now.
* Every string is plain ASCII, at most `MAX_LEN` characters, and never carries more precision than
  the data has (one decimal at most), because the card renders them verbatim.

The condition rules live in one table (`RULES`). Each rule is a small predicate over a flat
`Situation` of numbers derived from the evaluation, so every rule can be tested with crafted
values, and a rule that needs a number it does not have simply does not match - it never guesses.
A test pins the table to exactly the keys used in `species.py`, so vocabulary and rules cannot
drift apart.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from . import astronomy as astro
from . import species as sp
from .context import SpotContext
from .scoring import Evaluation, local_event, to_aware
from .series import extrema

MAX_LEN = 120

# --------------------------------------------------------------------------------------
# thresholds (each one names the angling observation behind it)
# --------------------------------------------------------------------------------------

#: water this far (degC) below / above a species' optimum is "cold" / "warm" *for that species*:
#: the same 12 degC is a cold-water death for a wels and a feast for a trout
COLD_OFFSET = -5.0
WARM_OFFSET = 3.5
#: sun altitude (deg) marks of the day
DAY_ALT = 0.0
BRIGHT_ALT = 20.0
NIGHT_ALT = -6.0
#: sky: percent cloud
CLEAR_SKY = 35.0
SUNNY_SKY = 30.0
GREY_SKY = 80.0
#: water colour (turbidity index 0..1; see hydro.fishing_water_clarity_words)
GIN_CLEAR = 0.20
CLEAR = 0.34
STAINED = 0.34
MURKY = 0.55
FLOOD_COLOURED = 0.75
#: wind (m/s)
CALM = 2.0
FLAT = 1.5
BREEZE = 4.5
WINDY = 6.0
GALE = 12.0
#: barometer (hPa over 3 h / 24 h)
FALLING = -1.0
FALLING_DEEP_3H = -1.2
FALLING_DEEP_24H = -4.0
RISING = 0.5
STEADY = 0.5
STABLE_24H = 3.0
#: river level, in log-discharge sigma (see hydro): rising, high, flood
RISING_LEVEL = 0.4
HIGH_LEVEL = 1.0
VERY_HIGH_LEVEL = 1.8
LOW_LEVEL = -0.5
BRIGHT_MOON = 0.5


# --------------------------------------------------------------------------------------
# the situation: flat numbers derived from an Evaluation and its spot
# --------------------------------------------------------------------------------------


@dataclass
class Situation:
    """What the rules look at. Every field is None when it cannot be known."""

    sun_alt: float | None = None
    moonshine: float | None = None
    cloud: float | None = None                  # percent
    wind: float | None = None                   # m/s
    wind_dir: float | None = None               # degrees, direction the wind comes FROM
    wind_max_24h: float | None = None
    humidity: float | None = None               # percent
    cape: float | None = None                   # J/kg
    air_temp: float | None = None
    water_offset: float | None = None           # degC relative to the species optimum
    water_change_24h: float | None = None
    water_change_7d: float | None = None
    tend3: float | None = None                  # tide-corrected hPa / 3 h
    tend24: float | None = None
    stability24: float | None = None            # hPa range over 24 h
    front_index: float | None = None
    front_eta: float | None = None              # hours
    temp_jump: float | None = None              # degC air change over ~6 h
    turbidity: float | None = None              # 0..1
    discharge_z: float | None = None
    level_trend_z: float | None = None
    plume: float | None = None
    rain_24: float | None = None
    rain_72: float | None = None
    running_water: bool = False                 # river or stream
    coastal: bool = False                       # coast or estuary
    tidal: bool = False
    tide_rising: bool | None = None
    hours_to_slack: float | None = None
    tide_range: float | None = None             # 0 neap .. 1 spring
    onshore: bool | None = None                 # wind blowing from the water onto the bank

    # -- tiny comparison helpers: a missing number never satisfies a condition ------------
    @staticmethod
    def ge(x: float | None, v: float) -> bool:
        return x is not None and x >= v

    @staticmethod
    def le(x: float | None, v: float) -> bool:
        return x is not None and x <= v

    @staticmethod
    def lt(x: float | None, v: float) -> bool:
        return x is not None and x < v

    @staticmethod
    def gt(x: float | None, v: float) -> bool:
        return x is not None and x > v

    # -- composite notions the rules share ------------------------------------------------
    @property
    def day(self) -> bool:
        return self.gt(self.sun_alt, DAY_ALT)

    @property
    def night(self) -> bool:
        return self.lt(self.sun_alt, NIGHT_ALT)

    @property
    def sunny(self) -> bool:
        return self.gt(self.sun_alt, BRIGHT_ALT) and self.lt(self.cloud, CLEAR_SKY)

    @property
    def clear_water(self) -> bool:
        return self.lt(self.turbidity, CLEAR)

    @property
    def dry(self) -> bool:
        """Low river, or - with no gauge - barely any rain for days."""
        if self.discharge_z is not None:
            return self.discharge_z <= LOW_LEVEL
        return self.rain_72 is not None and self.rain_72 <= 1.0

    @property
    def settled(self) -> bool:
        """A steady barometer over a calm day."""
        return (
            self.tend3 is not None
            and abs(self.tend3) < STEADY
            and self.lt(self.stability24, STABLE_24H)
        )


@dataclass(frozen=True)
class Rule:
    key: str
    test: Callable[[Situation], bool]
    priority: int                       # higher = more specific / more urgent, shown first
    meaning: str                        # the observation behind it, for the next reader


def _rules() -> tuple[Rule, ...]:
    S = Situation
    return (
        # -- timing: the most actionable, so they win ties -------------------------------
        Rule("front", lambda s: S.ge(s.front_index, 0.5) and S.le(s.front_eta, 6.0), 95,
             "a front is likely inside six hours: the feeding window before and during it"),
        Rule("post_front", lambda s: S.ge(s.tend3, RISING) and S.ge(s.stability24, 4.0) and S.lt(s.wind, 5.0), 92,
             "the barometer fell hard and is climbing again while the wind drops"),
        Rule("falling_barometer", lambda s: S.le(s.tend3, FALLING_DEEP_3H) and S.le(s.tend24, FALLING_DEEP_24H), 90,
             "a deep, steady fall over a day, not just the last few hours"),
        Rule("winter_storm_edge", lambda s: S.ge(s.front_index, 0.35) and S.le(s.tend3, FALLING) and S.le(s.air_temp, 8.0) and S.ge(s.wind, 7.0), 88,
             "a cold, windy, deepening low: the edge of the storm"),
        Rule("night_flood", lambda s: s.night and s.tidal and s.tide_rising is True, 86,
             "a rising tide after dark"),
        Rule("falling_into_flood", lambda s: s.tidal and s.tide_rising is True and S.le(s.hours_to_slack, 2.0), 85,
             "the last two hours of the flood, just before high water"),
        Rule("gale_aftermath", lambda s: S.ge(s.wind_max_24h, GALE) and S.lt(s.wind, 6.0) and S.ge(s.turbidity, STAINED), 84,
             "it blew a gale in the last day, has dropped, and the water is still cloudy"),
        Rule("pressure_falling", lambda s: S.le(s.tend3, FALLING), 80,
             "the barometer is falling now"),
        Rule("rising_falling", lambda s: S.le(s.tend3, -0.8) and s.day and S.ge(s.air_temp, 20.0), 79,
             "a falling barometer over a warm day"),
        Rule("cold_front", lambda s: S.le(s.temp_jump, -2.5) and S.ge(s.tend3, 0.0), 78,
             "the airmass has just turned colder with the barometer steady or rising"),
        Rule("cold_snap", lambda s: S.le(s.water_change_7d, -2.0) or S.le(s.temp_jump, -4.0), 77,
             "a sharp drop in temperature, in the water over the week or the air over hours"),
        Rule("warm_spike", lambda s: S.ge(s.water_change_24h, 2.5) or S.ge(s.water_change_7d, 3.0), 76,
             "the water jumped warmer faster than the fish are used to"),
        # -- the river: level and colour --------------------------------------------------
        Rule("very_high", lambda s: s.running_water and S.ge(s.discharge_z, VERY_HIGH_LEVEL), 70,
             "the river is far above its normal level"),
        Rule("high_coloured", lambda s: s.running_water and S.ge(s.discharge_z, HIGH_LEVEL) and S.ge(s.turbidity, 0.4), 69,
             "high and coloured"),
        Rule("rising_warm", lambda s: s.running_water and S.ge(s.level_trend_z, RISING_LEVEL) and S.ge(s.water_offset, 0.0), 68,
             "the river is rising and the water is at or above the species optimum"),
        Rule("slight_rise_clean", lambda s: S.ge(s.level_trend_z, 0.3) and S.le(s.level_trend_z, 1.5) and s.clear_water and S.le(s.tend3, -0.3), 67,
             "a small rise of clean water with a mild barometer fall"),
        Rule("colour_rising", lambda s: S.ge(s.level_trend_z, RISING_LEVEL) and S.ge(s.turbidity, 0.2) and S.lt(s.turbidity, MURKY), 66,
             "a steady rise with a little colour in the water"),
        Rule("fast_clean", lambda s: s.running_water and s.clear_water and S.ge(s.discharge_z, 0.2) and S.lt(s.discharge_z, HIGH_LEVEL), 65,
             "clean water running a little above normal"),
        Rule("low_clear_warm", lambda s: s.running_water and S.le(s.discharge_z, -0.8) and S.lt(s.turbidity, GIN_CLEAR) and S.ge(s.water_offset, 1.5), 64,
             "a low, gin-clear, warm river"),
        Rule("warm_dry", lambda s: s.running_water and s.dry and S.ge(s.water_offset, 2.0) and S.lt(s.turbidity, 0.25), 63,
             "a low, warm, clear river after a dry spell (a river state: the trout advice talks about a lock tail)"),
        Rule("clear_cool_river", lambda s: s.running_water and S.lt(s.turbidity, GIN_CLEAR) and S.le(s.water_offset, -1.0), 62,
             "clear water running cooler than the species optimum"),
        Rule("fresh_runoff", lambda s: s.coastal and (S.ge(s.plume, 0.3) or S.ge(s.rain_24, 5.0)), 61,
             "freshwater is pushing into salt or brackish water"),
        # -- water colour -----------------------------------------------------------------
        Rule("turbid", lambda s: S.ge(s.turbidity, MURKY), 55,
             "murky water"),
        Rule("coloured", lambda s: S.ge(s.turbidity, STAINED) and S.lt(s.turbidity, FLOOD_COLOURED), 54,
             "stained water"),
        Rule("clear_cold", lambda s: s.clear_water and S.lt(s.turbidity, GIN_CLEAR) and S.le(s.water_offset, COLD_OFFSET), 53,
             "gin-clear and cold"),
        Rule("clear_spring_tide", lambda s: s.tidal and S.ge(s.tide_range, 0.75) and s.clear_water, 52,
             "clear water on a big tide"),
        # -- temperature ------------------------------------------------------------------
        Rule("warm_night", lambda s: s.night and S.ge(s.air_temp, 17.0), 50,
             "a warm night"),
        Rule("cloudy_night", lambda s: s.night and S.ge(s.cloud, 70.0) and S.ge(s.air_temp, 12.0), 49,
             "a mild night under cloud"),
        Rule("warm_stable", lambda s: s.settled and S.ge(s.water_offset, 0.0), 48,
             "a settled barometer and water at or above the optimum"),
        Rule("stable_warm", lambda s: s.settled and S.ge(s.water_offset, -1.5), 47,
             "a settled barometer in water close to the optimum"),
        Rule("warm", lambda s: S.ge(s.water_offset, WARM_OFFSET), 46,
             "water well above the species optimum"),
        Rule("cold", lambda s: S.le(s.water_offset, COLD_OFFSET), 45,
             "water well below the species optimum"),
        # -- light ------------------------------------------------------------------------
        Rule("moonlit", lambda s: S.ge(s.moonshine, BRIGHT_MOON) and S.lt(s.sun_alt, DAY_ALT), 44,
             "a bright moon above the horizon"),
        Rule("bright_clear_warm", lambda s: s.sunny and s.clear_water and S.ge(s.water_offset, 2.0), 43,
             "hard sun, clear water, warm"),
        Rule("warm_clear_bright", lambda s: s.sunny and s.clear_water and S.ge(s.water_offset, 0.0), 42,
             "hard sun, clear water, at or above the optimum"),
        Rule("bright_warm", lambda s: s.sunny and S.ge(s.water_offset, 2.0), 41,
             "hard sun and warm water"),
        Rule("bright_clear", lambda s: s.sunny and s.clear_water, 40,
             "hard sun and clear water"),
        Rule("bright_choppy", lambda s: s.sunny and S.ge(s.wind, BREEZE), 39,
             "sun on a rippled surface"),
        Rule("bright_calm", lambda s: s.day and S.lt(s.cloud, 40.0) and S.lt(s.wind, CALM), 38,
             "sunny and calm"),
        Rule("bright", lambda s: S.gt(s.sun_alt, 25.0) and S.lt(s.cloud, SUNNY_SKY), 37,
             "a bright, sunny day"),
        Rule("muggy_overcast", lambda s: S.ge(s.cloud, 75.0) and S.ge(s.humidity, 70.0) and S.lt(s.wind, 3.5) and S.ge(s.air_temp, 15.0) and (s.cape is None or s.cape >= 200.0), 36,
             "still, humid, overcast and warm with energy in the air"),
        Rule("overcast", lambda s: s.day and S.ge(s.cloud, GREY_SKY) and S.ge(s.air_temp, 8.0), 35,
             "a grey, mild day"),
        # -- wind -------------------------------------------------------------------------
        Rule("strong_breeze_southwesterly", lambda s: S.ge(s.wind, 10.0) and S.ge(s.wind_dir, 180.0) and S.le(s.wind_dir, 270.0), 34,
             "a strong south-westerly"),
        Rule("onshore_breeze", lambda s: s.onshore is True and S.ge(s.wind, 3.0) and S.le(s.wind, 9.0), 33,
             "an onshore breeze"),
        Rule("chop_on_the_bank", lambda s: s.coastal and S.ge(s.wind, BREEZE) and S.lt(s.wind, 10.0), 32,
             "a fresh breeze building a chop at the shore"),
        Rule("choppy_wind", lambda s: S.ge(s.wind, BREEZE), 31,
             "enough wind to put a chop on the water"),
        Rule("windy", lambda s: S.ge(s.wind, WINDY), 30,
             "a windy day"),
        Rule("flat_calm_clear", lambda s: S.lt(s.wind, FLAT) and S.lt(s.turbidity, GIN_CLEAR), 29,
             "flat calm over gin-clear water"),
    )


RULES: dict[str, Rule] = {r.key: r for r in _rules()}


# --------------------------------------------------------------------------------------
# deriving the Situation
# --------------------------------------------------------------------------------------


def _tide_turning(ctx: SpotContext, aware: datetime) -> tuple[bool | None, float | None]:
    """(is the tide rising?, hours until it turns) from the equilibrium tide."""
    if not ctx.tidal:
        return None, None
    now = astro.tide_state(aware, ctx.lon).level
    soon = astro.tide_state(aware + timedelta(minutes=20), ctx.lon).level
    rising = soon > now
    previous = now
    for step in range(1, 33):                                   # up to 8 h in 15-minute steps
        level = astro.tide_state(aware + timedelta(minutes=15 * step), ctx.lon).level
        if (level > previous) != rising:
            return rising, step * 0.25
        previous = level
    return rising, None


def _onshore(ctx: SpotContext, wind_dir: float | None) -> bool | None:
    """Wind blowing from the water onto the bank, when the bank's aspect is known."""
    aspect = ctx.shelter.bank_aspect_deg
    if wind_dir is None or aspect is None:
        return None
    return abs(((wind_dir - aspect + 180.0) % 360.0) - 180.0) <= 60.0


def situation(ctx: SpotContext, ev: Evaluation) -> Situation:
    """Collect the numbers the rules need; anything unknown stays None."""
    o, pa, hs, water = ev.obs, ev.pressure, ev.hydro, ev.water
    aware = to_aware(ctx, ev.when)
    rising, slack = _tide_turning(ctx, aware)
    tide = astro.tide_state(aware, ctx.lon) if ctx.tidal else None
    _, wind_hi = extrema(ctx.get("wind_speed"), ev.when, 24.0)
    return Situation(
        sun_alt=ev.sun_altitude,
        moonshine=ev.moonshine,
        cloud=o.get("cloud"),
        wind=o.get("wind_speed"),
        wind_dir=o.get("wind_dir"),
        wind_max_24h=wind_hi,
        humidity=o.get("humidity"),
        cape=o.get("cape"),
        air_temp=o.get("air_temp"),
        water_offset=None if water is None else water.species_offset_c,
        water_change_24h=None if water is None else water.change_24h,
        water_change_7d=None if water is None else water.change_7d,
        tend3=None if pa is None else pa.tendency_3h,
        tend24=None if pa is None else pa.tendency_24h,
        stability24=None if pa is None else pa.stability_24h,
        front_index=None if pa is None else pa.front_index,
        front_eta=None if pa is None else pa.front_eta_hours,
        temp_jump=None if pa is None else pa.temp_jump_c,
        turbidity=None if hs is None else hs.turbidity_index,
        discharge_z=None if hs is None else hs.discharge_z,
        level_trend_z=None if hs is None else hs.level_trend_z,
        plume=None if hs is None else hs.plume_index,
        rain_24=None if hs is None else hs.rain_24h_mm,
        rain_72=None if hs is None else hs.rain_72h_mm,
        running_water=ctx.spot_kind in ("river", "stream"),
        coastal=ctx.spot_kind in ("coast", "estuary"),
        tidal=ctx.tidal,
        tide_rising=rising,
        hours_to_slack=slack,
        tide_range=None if tide is None else tide.range_index,
        onshore=_onshore(ctx, o.get("wind_dir")),
    )


def matching_keys(s: Situation) -> set[str]:
    """Every condition that holds in a Situation (pure; the rules do the deciding)."""
    return {key for key, rule in RULES.items() if rule.test(s)}


def state_keys(ctx: SpotContext, ev: Evaluation) -> set[str]:
    """Every named condition that holds right now at this spot."""
    return matching_keys(situation(ctx, ev))


# --------------------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------------------


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def clip(text: str, limit: int = MAX_LEN) -> str:
    """At most `limit` characters, cut at a word boundary, ASCII only."""
    text = " ".join(_ascii(text).split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 3].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (cut or text[: limit - 3]) + "..."


def reasons(ev: Evaluation) -> list[str]:
    """The two factors helping most and the two hurting most, each with its number."""
    used = [f for f in ev.factors if f.source != "unavailable"]
    helping = sorted((f for f in used if f.contribution > 0.01), key=lambda f: -f.contribution)[:2]
    hurting = sorted((f for f in used if f.contribution < -0.01), key=lambda f: f.contribution)[:2]
    out = [clip(f"Helps: {f.why}") for f in helping] + [clip(f"Hurts: {f.why}") for f in hurting]
    if not out:
        out.append("Conditions are close to average and nothing stands out." if used else "Not enough data to say what is driving the score.")
    return out


def tactics(ctx: SpotContext, ev: Evaluation, prof: sp.SpeciesProfile, limit: int = 2) -> list[str]:
    """What this species' profile suggests for the conditions holding now (most specific first)."""
    keys = [k for k in state_keys(ctx, ev) if k in prof.tactics]
    keys.sort(key=lambda k: -RULES[k].priority)
    out = [clip(prof.tactics[k]) for k in keys[:limit]]
    if not out:
        out.append(clip(prof.tactics.get("default") or "Fish the transition periods: dawn, dusk and the hour around a change in the weather."))
    return out


def headline(ev: Evaluation, prof: sp.SpeciesProfile) -> str:
    """One line for the card's title row."""
    text = f"{ev.band.capitalize()} fishing for {prof.name} ({ev.score:.1f}/10)"
    sol = ev.solunar
    if sol is not None and sol.in_window == "major":
        text += ", in a major solunar window"
    if ev.confidence < 0.5:
        text += ", low confidence"
    return clip(text)


def _hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def window_advice(ctx: SpotContext, ev: Evaluation) -> str:
    """When to be on the water: the next major solunar window and the golden hour."""
    parts: list[str] = []
    when = ev.when
    sol = ev.solunar
    if sol is not None:
        upcoming = [w for w in sol.windows if w.kind == "major" and local_event(ctx, w.end) > when]
        if upcoming:
            w = min(upcoming, key=lambda w: w.start)
            a, b = local_event(ctx, w.start), local_event(ctx, w.end)
            parts.append(f"Major solunar window now until {_hhmm(b)}" if a <= when else f"Next major solunar window {_hhmm(a)}-{_hhmm(b)}")
    for start, end, kind in ctx.astronomy_for(when).golden:
        a, b = local_event(ctx, start), local_event(ctx, end)
        if a <= when <= b:
            parts.append(f"Golden hour ends {_hhmm(b)}")
            break
        if when < a <= when + timedelta(hours=6):
            parts.append(f"Golden hour ({kind}) starts {_hhmm(a)}")
            break
    return clip("; ".join(parts)) if parts else "No strong timing window in the next few hours."


def as_dict(ctx: SpotContext, ev: Evaluation, prof: sp.SpeciesProfile) -> dict:
    """Everything the sensors and card need, ready for state attributes."""
    keys = sorted(state_keys(ctx, ev), key=lambda k: -RULES[k].priority)
    return {
        "headline": headline(ev, prof),
        "reasons": reasons(ev),
        "tactics": tactics(ctx, ev, prof),
        "window": window_advice(ctx, ev),
        "conditions": keys,
        "notes": [clip(n) for n in ev.notes],
    }


__all__ = [
    "MAX_LEN", "RULES", "Rule", "Situation", "as_dict", "clip", "headline", "matching_keys",
    "reasons", "situation", "state_keys", "tactics", "window_advice",
]
