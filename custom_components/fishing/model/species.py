"""Species profiles: where angler knowledge enters the model as *data*, not as physics.

Every factor module reports a value ("water 3.2 degC above normal for June", "wind 6 m/s from
the north-west"). What makes a profile a *species* profile is which of those values it cares
about, in what proportion, and with what temperature optimum and seasonal propensity. Those
numbers come from the well-replicated parts of the angling literature and field guides:

* temperature optimum/breadth (the best-replicated effect in fish ecology),
* photoperiod/crepuscular habit (pike and zander are visual predators with different light
  windows; wels catfish are essentially nocturnal in clear water),
* turbidity tolerance (zander's tapetum lucidum lets it hunt stained water where a trout cannot),
* seasonal propensity, including the well-known feeding gap around the spawn,
* tidal dependence for marine and estuary species (a flounder's fishing is tide-driven, not
  weather-driven; a bass's is both).

They are declared as *weights over factors* plus response-curve offsets, so a calibration pass
(the user's own logged catches) can retune a profile without touching the physics modules - and
so the card can show the angler why a score came out the way it did.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

#: the canonical factor names used by scoring.py; a profile may only use these
FACTORS = (
    "water_temp",     # suitability vs the species optimum + 7-day change
    "pressure",       # barometric state: anomaly + tendency + stability
    "front",          # a frontal passage arriving/under way
    "wind",           # wind speed response
    "shelter",        # how protected the bank is from that wind
    "cloud",          # cloud cover / diffuse light
    "uv",             # clear-sky brightness (sight predators)
    "precip",         # rain right now
    "hydro",          # water state: level, turbidity, runoff
    "solunar",        # lunar timing windows
    "time_of_day",    # golden hours / photoperiod preference
    "moonlight",      # night light (helps some hunters, hinders others)
    "tide",           # tidal movement (only meaningful for tidal water)
)

#: a sensible default when the user picks "mixed / I don't know yet"
GENERIC_WEIGHTS: dict[str, float] = {
    "water_temp": 0.20,
    "pressure": 0.11,
    "front": 0.07,
    "wind": 0.08,
    "shelter": 0.03,
    "cloud": 0.06,
    "uv": 0.05,
    "precip": 0.04,
    "hydro": 0.10,
    "solunar": 0.10,
    "time_of_day": 0.08,
    "moonlight": 0.03,
    "tide": 0.05,
}


@dataclass(frozen=True)
class SpeciesProfile:
    id: str
    name: str
    group: str                                  # predator | cyprinid | salmonid | marine | catfish
    water_temp_optimum_c: float
    water_temp_width_c: float                   # response-curve width: tolerance, not a cliff
    weights: dict[str, float]
    #: seasonal propensity by month (1-12): >1 better than average, <1 worse. Reflects feeding
    #: season and the spawn gap, not legal catchability - see `legal_note`.
    season: tuple[float, ...]
    light_window: str = "crepuscular"           # diurnal | crepuscular | nocturnal | opportunistic
    turbidity_pref: float = 0.35                # peak of this species' turbidity response, 0..1
    #: sign of the response to bright moonlight at night, -1 (hates it) .. +1 (hunts by it).
    #: This is where the real divergence between a wels catfish and a night-hunting flounder lives,
    #: and it is why "the moon is full" is not a forecast on its own.
    moonlight_effect: float = 0.0
    pressure_sensitivity: float = 1.0           # multiplier on the pressure/front response
    cold_tolerant: bool = False
    tactics: dict[str, str] = field(default_factory=dict)
    legal_note: str = ""
    notes: str = ""

    # -- profile arithmetic ------------------------------------------------------------

    def weight_map(self) -> dict[str, float]:
        total = sum(self.weights.values()) or 1.0
        return {k: v / total for k, v in self.weights.items()}

    def season_factor(self, on: date) -> float:
        return self.season[(on.month - 1) % 12]

    def adjusted(self, **overrides) -> "SpeciesProfile":
        """A copy with tweaked weights/optimum - what the calibration pass uses."""
        return replace(self, **overrides)

    def tactic_for(self, state: str, default: str = "") -> str:
        return self.tactics.get(state, default)


def _w(**pairs: float) -> dict[str, float]:
    """Weights on top of the generic set: a profile only lists what it changes."""
    out = dict(GENERIC_WEIGHTS)
    for key, value in pairs.items():
        if value is None or value <= 0:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def _season(*months: float) -> tuple[float, ...]:
    assert len(months) == 12, "a seasonal profile needs one factor per month"
    return tuple(float(m) for m in months)


# --------------------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------------------

PROFILE_LIST: tuple[SpeciesProfile, ...] = (
    SpeciesProfile(
        id="perch",
        name="European perch",
        group="predator",
        water_temp_optimum_c=18.0,
        water_temp_width_c=6.0,
        weights=_w(water_temp=0.20, cloud=0.07, uv=0.09, time_of_day=0.12, wind=0.09, solunar=0.08, hydro=0.06, pressure=0.09, front=0.06, moonlight=0.02),
        season=_season(0.5, 0.5, 0.7, 0.9, 1.0, 1.1, 1.15, 1.1, 1.05, 1.1, 0.8, 0.55),
        light_window="diurnal",
        turbidity_pref=0.30,
        tactics={
            "warm_clear_bright": "go deeper or fish the shaded edges: perch drop off the shallows once the light is hard",
            "coloured": "work the shallows with bright lures - perch hunt by lateral line as much as by eye",
            "cold": "slow it right down, small jig, hold it in front of the fish",
            "windy": "find the wind-blown bank: baitfish get pushed there and perch follow",
        },
        notes=" shoaling; the school moves with temperature more than with anything else",
    ),
    SpeciesProfile(
        id="pike",
        name="Northern pike",
        group="predator",
        water_temp_optimum_c=16.0,
        water_temp_width_c=7.5,
        weights=_w(water_temp=0.22, time_of_day=0.13, wind=0.09, hydro=0.11, pressure=0.10, front=0.09, solunar=0.09, uv=0.05, cloud=0.05, moonlight=0.03),
        season=_season(1.15, 1.05, 1.0, 1.05, 0.85, 0.7, 0.75, 0.9, 1.15, 1.3, 1.3, 1.2),
        light_window="crepuscular",
        turbidity_pref=0.45,
        moonlight_effect=0.45,
        pressure_sensitivity=1.2,
        tactics={
            "front": "the hour before and during a front is the best window of the day - move with it",
            "warm_clear_bright": "weed edges and deep drop-offs; big slow presentations",
            "coloured": "large baits, close to cover, noise and vibration",
            "cold": "the shallows on a warm afternoon beat the deep water",
        },
        notes="the classic barometer fish: strongest pressure-tendency response of the predators",
    ),
    SpeciesProfile(
        id="zander",
        name="Zander (pikeperch)",
        group="predator",
        water_temp_optimum_c=18.5,
        water_temp_width_c=6.0,
        weights=_w(water_temp=0.20, moonlight=0.09, time_of_day=0.15, hydro=0.14, uv=0.09, solunar=0.09, wind=0.06, pressure=0.08, front=0.05),
        season=_season(0.7, 0.75, 0.9, 1.0, 1.1, 1.15, 1.2, 1.2, 1.1, 1.0, 0.85, 0.7),
        light_window="nocturnal",
        turbidity_pref=0.60,
        moonlight_effect=-0.65,
        tactics={
            "bright_clear": "wait for dark: zander's tapetum gives it the night shift",
            "coloured": "the stained water is the reason it is there - fish the whole water column",
            "moonlit": "bright moon on still water: go deeper or move to the coloured bank",
        },
        notes="tapetum lucidum: sees in conditions a pike cannot, and is negatively lit above ~1 lux",
    ),
    SpeciesProfile(
        id="wels_catfish",
        name="Wels catfish",
        group="catfish",
        water_temp_optimum_c=22.0,
        water_temp_width_c=5.0,
        weights=_w(water_temp=0.26, moonlight=0.12, hydro=0.14, time_of_day=0.13, pressure=0.08, front=0.06, solunar=0.08, wind=0.05, uv=0.05, cloud=0.04),
        season=_season(0.15, 0.2, 0.5, 0.85, 1.1, 1.25, 1.3, 1.3, 1.15, 0.85, 0.4, 0.2),
        light_window="nocturnal",
        turbidity_pref=0.55,
        moonlight_effect=-0.75,
        tactics={
            "warm": "the warmest nights of the year are the peak: hold on the deep channel edge",
            "cold": "it stops feeding below ~12 degC; do not bother",
            "moonlit": "bright moon and clear water pushes it deep - fish the dark banks instead",
            "rising_warm": "a warm rise in a big river is the best catfishing condition there is",
        },
        notes="temperature-driven: the season is short and the response to a warm spell is dramatic",
    ),
    SpeciesProfile(
        id="carp",
        name="Common carp",
        group="cyprinid",
        water_temp_optimum_c=21.0,
        water_temp_width_c=5.5,
        weights=_w(water_temp=0.24, pressure=0.14, cloud=0.09, wind=0.07, hydro=0.11, precip=0.06, time_of_day=0.07, solunar=0.09, front=0.04, moonlight=0.03),
        season=_season(0.2, 0.25, 0.55, 0.85, 1.1, 1.25, 1.3, 1.3, 1.2, 1.0, 0.5, 0.25),
        light_window="opportunistic",
        turbidity_pref=0.5,
        moonlight_effect=-0.25,
        pressure_sensitivity=0.8,
        tactics={
            "stable_warm": "a settled warm spell with a steady barometer: feed heavily, fish one spot",
            "cold_snap": "back off completely - a 5 degC drop in a warm spell kills the bite",
            "turbid": "carp feed by bottom contact: a slightly stained lake is normal for them",
            "pressure_falling": "the fish go sideways and stop feeding: move to the deepest, steadiest area",
        },
        notes="the species most sensitive to *change* rather than to absolute conditions",
    ),
    SpeciesProfile(
        id="tench",
        name="Tench",
        group="cyprinid",
        water_temp_optimum_c=20.0,
        water_temp_width_c=4.5,
        weights=_w(water_temp=0.24, pressure=0.12, cloud=0.11, wind=0.05, hydro=0.10, precip=0.08, time_of_day=0.08, solunar=0.07),
        season=_season(0.15, 0.2, 0.5, 0.85, 1.1, 1.3, 1.3, 1.25, 1.1, 0.8, 0.35, 0.15),
        light_window="crepuscular",
        turbidity_pref=0.6,
        tactics={
            "muggy_overcast": "the classic tench day: still, humid, overcast, thunderstorms brewing",
            "windy": "the wind-ward lily edge is where they hold",
        },
        notes="narrow temperature window; the summer species of weedy, static water",
    ),
    SpeciesProfile(
        id="bream",
        name="Common bream",
        group="cyprinid",
        water_temp_optimum_c=18.0,
        water_temp_width_c=6.0,
        weights=_w(water_temp=0.20, pressure=0.12, hydro=0.12, cloud=0.08, wind=0.08, solunar=0.11, time_of_day=0.09, precip=0.05),
        season=_season(0.3, 0.4, 0.7, 1.0, 1.15, 1.2, 1.15, 1.1, 1.05, 0.9, 0.5, 0.3),
        light_window="opportunistic",
        turbidity_pref=0.62,
        tactics={
            "cloudy_night": "night fishing on a warm, cloudy night is the single most reliable bream method",
            "cold": "slow, small, on the bottom, near the bar",
        },
        notes="the solunar response is unusually clean because bream shoals feed on a rhythm",
    ),
    SpeciesProfile(
        id="roach_dace",
        name="Roach / dace",
        group="cyprinid",
        water_temp_optimum_c=16.0,
        water_temp_width_c=6.5,
        weights=_w(water_temp=0.18, hydro=0.16, wind=0.10, pressure=0.10, cloud=0.08, precip=0.07, time_of_day=0.10, solunar=0.07),
        season=_season(0.6, 0.7, 0.95, 1.1, 1.1, 1.0, 0.95, 1.0, 1.05, 1.05, 0.85, 0.65),
        light_window="diurnal",
        turbidity_pref=0.45,
        tactics={
            "colour_rising": "a little colour and a steady rise: the shallow of a river comes alive",
            "clear_cold": "long line, small hook, groundbait, and patience near the slack",
        },
    ),
    SpeciesProfile(
        id="barbel",
        name="Barbel",
        group="cyprinid",
        water_temp_optimum_c=19.0,
        water_temp_width_c=5.0,
        weights=_w(water_temp=0.22, hydro=0.18, wind=0.08, pressure=0.10, solunar=0.08, time_of_day=0.09, cloud=0.06),
        season=_season(0.1, 0.15, 0.5, 0.9, 1.15, 1.3, 1.3, 1.25, 1.1, 0.8, 0.3, 0.1),
        light_window="opportunistic",
        turbidity_pref=0.5,
        legal_note="in much of western Europe the closed season is mid-June to mid-June; check local rules",
        tactics={
            "fast_clean": "the strongest current you can hold bottom in is the best water",
            "warm_stable": "late-summer stable weeks are the peak of the year",
        },
        notes="needs oxygen-rich flowing water; a stagnation event in a lock keeps them off the feed",
    ),
    SpeciesProfile(
        id="brown_trout_sea_trout",
        name="Brown trout / sea trout",
        group="salmonid",
        water_temp_optimum_c=14.0,
        water_temp_width_c=4.5,
        weights=_w(water_temp=0.24, hydro=0.18, precip=0.11, cloud=0.10, uv=0.10, pressure=0.08, front=0.08, wind=0.06, time_of_day=0.10, solunar=0.05, tide=0.04),
        season=_season(0.7, 0.8, 1.05, 1.15, 1.1, 0.95, 0.8, 0.85, 1.15, 1.2, 0.95, 0.7),
        light_window="opportunistic",
        turbidity_pref=0.42,
        cold_tolerant=True,
        tactics={
            "bright_clear_warm": "dry-fly at the very edges of the day, or a nymph deep in the glide",
            "colour_rising": "the classic wet-fly / spinner condition - take advantage before it clouds over",
            "very_high": " coloured and pushing hard: fish the loops and the banks, expect few fish",
            "warm_dry": "low, warm and clear is the worst state; fish the spring-fed tail of a lock",
        },
        notes="oxygen-limited: warm + slow + clear is a bad triple; a little rise with colour is the trigger",
    ),
    SpeciesProfile(
        id="rainbow_brook_trout",
        name="Rainbow / brook trout",
        group="salmonid",
        water_temp_optimum_c=15.0,
        water_temp_width_c=4.0,
        weights=_w(water_temp=0.26, hydro=0.14, cloud=0.11, uv=0.10, precip=0.09, time_of_day=0.11, pressure=0.08, solunar=0.06),
        season=_season(0.8, 0.9, 1.1, 1.15, 1.05, 0.9, 0.75, 0.8, 1.05, 1.15, 1.0, 0.85),
        light_window="opportunistic",
        turbidity_pref=0.35,
        cold_tolerant=True,
        tactics={
            "warm": "above ~20 degC a rainbow stops feeding in still water; find the inlet or the shade",
            "overcast": "the whole day is fishable on a grey, mild day",
        },
        notes="the least heat-tolerant of the common stocked trout - the water column, not the air, decides",
    ),
    SpeciesProfile(
        id="atlantic_salmon",
        name="Atlantic salmon",
        group="salmonid",
        water_temp_optimum_c=13.0,
        water_temp_width_c=3.5,
        weights=_w(water_temp=0.22, hydro=0.26, pressure=0.09, front=0.09, cloud=0.09, wind=0.06, time_of_day=0.10, solunar=0.07, uv=0.07),
        season=_season(0.2, 0.3, 0.8, 1.1, 1.25, 1.2, 1.1, 1.0, 0.9, 0.7, 0.4, 0.2),
        light_window="opportunistic",
        turbidity_pref=0.5,
        cold_tolerant=True,
        legal_note="salmon fisheries are heavily regulated (catch-and-release, tags, rod limits): the model does not replace local bylaws",
        tactics={
            "slight_rise_clean": "the perfect state: a foot of clean rise with a mild barometer fall",
            "high_coloured": "fish the pools and the tail of the pool, big fly, deep and slow",
            "low_clear_warm": "a locked-down river: a very small fly, very deep, at the very edge of the day",
        },
        notes="the hydro factor matters most here: salmon hold in pools and feed on the drifting food a rise brings",
    ),
    SpeciesProfile(
        id="walleye",
        name="Walleye",
        group="predator",
        water_temp_optimum_c=18.0,
        water_temp_width_c=6.0,
        weights=_w(water_temp=0.21, time_of_day=0.15, uv=0.11, hydro=0.13, moonlight=0.08, solunar=0.09, wind=0.08, pressure=0.08, front=0.06),
        season=_season(0.6, 0.65, 0.85, 1.05, 1.15, 1.2, 1.2, 1.15, 1.1, 1.0, 0.8, 0.65),
        light_window="crepuscular",
        turbidity_pref=0.62,
        tactics={
            "bright_clear": "the light is the enemy - move to deeper structure or wait for the low sun",
            "choppy_wind": "wind on a rocky point is the best walleye water on the lake",
            "coloured": "shallower and slower than you think, right on the bottom",
        },
        notes="the tapetum lucidum makes turbidity an advantage, not a drawback",
    ),
    SpeciesProfile(
        id="largemouth_bass",
        name="Largemouth bass",
        group="predator",
        water_temp_optimum_c=21.0,
        water_temp_width_c=6.0,
        weights=_w(water_temp=0.20, pressure=0.12, front=0.10, wind=0.09, shelter=0.05, hydro=0.09, time_of_day=0.11, solunar=0.10, cloud=0.06, uv=0.06),
        season=_season(0.4, 0.5, 0.8, 1.05, 1.15, 1.2, 1.2, 1.15, 1.15, 1.05, 0.65, 0.4),
        light_window="opportunistic",
        turbidity_pref=0.42,
        pressure_sensitivity=1.15,
        tactics={
            "post_front": "the first hour after a front passes and the wind drops is the classic bass window",
            "bright_calm": "topwater at first light, then go to structure and drop-shot",
            "cold_front": "slow, small, near the bottom: bass become lethargic below ~12 degC",
        },
    ),
    SpeciesProfile(
        id="smallmouth_spotted_bass",
        name="Smallmouth / spotted bass",
        group="predator",
        water_temp_optimum_c=19.0,
        water_temp_width_c=5.5,
        weights=_w(water_temp=0.21, pressure=0.11, front=0.09, wind=0.07, hydro=0.11, time_of_day=0.10, solunar=0.09, cloud=0.07, uv=0.07),
        season=_season(0.45, 0.55, 0.85, 1.05, 1.2, 1.2, 1.15, 1.1, 1.1, 1.0, 0.65, 0.45),
        light_window="diurnal",
        turbidity_pref=0.35,
        tactics={
            "clear_cool_river": "current seams and boulder gardens: clearer and cooler than bass water",
            "bright": "the clearer and brighter it is, the deeper and more precise the presentation",
        },
    ),
    SpeciesProfile(
        id="muskellunge",
        name="Musky / tiger musky",
        group="predator",
        water_temp_optimum_c=16.0,
        water_temp_width_c=7.0,
        weights=_w(water_temp=0.20, pressure=0.11, front=0.12, time_of_day=0.12, solunar=0.12, wind=0.08, hydro=0.10, uv=0.05),
        season=_season(0.6, 0.6, 0.85, 1.0, 1.1, 1.2, 1.15, 1.1, 1.25, 1.35, 1.0, 0.7),
        light_window="crepuscular",
        turbidity_pref=0.48,
        pressure_sensitivity=1.25,
        tactics={
            "falling_barometer": "the front is the whole game: a deep, steady fall is the best musky weather",
            "bright_warm": "weeds and shallow bays on a warm, bright day - the opposite of what feels right",
        },
    ),
    SpeciesProfile(
        id="channel_catfish",
        name="Channel catfish",
        group="catfish",
        water_temp_optimum_c=24.0,
        water_temp_width_c=5.5,
        weights=_w(water_temp=0.24, moonlight=0.11, hydro=0.13, time_of_day=0.12, solunar=0.09, pressure=0.08, front=0.06, wind=0.06),
        season=_season(0.25, 0.3, 0.6, 0.9, 1.15, 1.25, 1.3, 1.25, 1.15, 0.85, 0.45, 0.25),
        light_window="nocturnal",
        turbidity_pref=0.6,
        tactics={
            "warm_night": "the hottest nights are the peak; fish the channel edge and the tail of a pool",
            "rising_falling": "a falling barometer over a warm day triggers a long, hard feed",
            "cold": "it shuts down almost completely below ~10 degC",
        },
    ),
    SpeciesProfile(
        id="European_bass",
        name="European bass (sea bass)",
        group="marine",
        water_temp_optimum_c=15.0,
        water_temp_width_c=5.5,
        weights=_w(tide=0.16, water_temp=0.19, wind=0.12, shelter=0.07, hydro=0.12, time_of_day=0.11, pressure=0.09, solunar=0.09, front=0.08, cloud=0.06, moonlight=0.04),
        season=_season(0.45, 0.5, 0.75, 0.95, 1.1, 1.2, 1.25, 1.2, 1.1, 1.05, 0.7, 0.5),
        light_window="opportunistic",
        turbidity_pref=0.55,
        moonlight_effect=0.5,
        tactics={
            "chop_on_the_bank": "a fresh breeze building a chop over a sandbank is the textbook bass day",
            "flat_calm_clear": "gin-clear flat calm is the hardest fishing of the year - go small, go long, go at night",
            "fresh_runoff": "the freshwater/plume edge at a groyne or outfall holds fish in summer",
            "falling_into_flood": "the last two hours of the flood tide as the wind backs",
        },
        notes="tidal and stratification-driven: the tide factor is weighted heavier than for any other species",
    ),
    SpeciesProfile(
        id="flounder_plaice",
        name="Flounder / plaice",
        group="marine",
        water_temp_optimum_c=11.0,
        water_temp_width_c=6.5,
        weights=_w(tide=0.26, water_temp=0.15, hydro=0.14, moonlight=0.09, wind=0.10, time_of_day=0.09, pressure=0.08, solunar=0.11, front=0.05),
        season=_season(1.05, 1.0, 0.95, 0.85, 0.8, 0.75, 0.8, 0.9, 1.05, 1.15, 1.1, 1.05),
        light_window="opportunistic",
        turbidity_pref=0.75,
        moonlight_effect=0.6,
        tactics={
            "night_flood": "night flood tide on a muddy bank: the single most reliable flatfish pattern",
            "gale_aftermath": "the calm after a gale, when the water is still cloudy but the tide is running clean",
            "clear_spring_tide": "clear water plus a big tide: move to the deep channels and the groyne tips",
        },
        notes="tide-driven, turbidity-tolerant and cold-loving: the anti-weather fish",
    ),
    SpeciesProfile(
        id="coastal_cod",
        name="Cod (inshore / spring)",
        group="marine",
        water_temp_optimum_c=8.0,
        water_temp_width_c=5.5,
        weights=_w(tide=0.22, water_temp=0.20, moonlight=0.10, wind=0.11, solunar=0.10, time_of_day=0.09, pressure=0.07, front=0.05, hydro=0.06),
        season=_season(1.25, 1.2, 1.1, 0.9, 0.6, 0.35, 0.25, 0.3, 0.6, 0.9, 1.1, 1.25),
        light_window="nocturnal",
        turbidity_pref=0.65,
        moonlight_effect=0.55,
        cold_tolerant=True,
        tactics={
            "winter_storm_edge": "the edges of a moving low, just before the wind veers: the classic winter cod window",
            "warm_spike": "a warm salt intrusion (a 'red tide' surge) shuts the fishery down completely",
            "strong_breeze_southwesterly": "sheltered marks only: the water gets dirty and the bite dies",
        },
        notes="temperature and salt intrusion are the dominant effects on a coastal cod fishery",
    ),
    SpeciesProfile(
        id="mackerel_horsey_mackerel",
        name="Mackerel / horsey mackerel",
        group="marine",
        water_temp_optimum_c=13.0,
        water_temp_width_c=5.0,
        weights=_w(tide=0.14, water_temp=0.21, wind=0.13, cloud=0.08, time_of_day=0.10, hydro=0.10, pressure=0.07, solunar=0.07, uv=0.06),
        season=_season(0.3, 0.4, 0.7, 1.0, 1.2, 1.3, 1.25, 1.2, 1.1, 0.85, 0.5, 0.3),
        light_window="diurnal",
        turbidity_pref=0.5,
        tactics={
            "bright_choppy": "sun on a choppy surface is the condition: the plankton and the baitfish are up",
            "onshore_breeze": "an onshore breeze pushing coloured water off a bank is where they are",
        },
        notes="the thermocline's edge and a temperature anomaly matter more than any barometric signal",
    ),
    SpeciesProfile(
        id="mixed_general",
        name="Mixed / general (opportunistic)",
        group="general",
        water_temp_optimum_c=16.5,
        water_temp_width_c=8.0,
        weights=dict(GENERIC_WEIGHTS),
        season=_season(1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1),
        light_window="opportunistic",
        turbidity_pref=0.45,
        tactics={
            "default": "cover the transition periods: dawn, dusk and the hour around a front",
        },
        notes="the safe profile when a spot holds several species - the generic response curves",
    ),
)

#: Response to bright moonlight at night, -1 (hates it) .. +1 (hunts by it), kept in one table
#: because it is the cleanest single divider in the whole registry: the species that hunt at night
#: *by sight* (flounder on a flat, cod on a shoal, pike and bass on a moonlit shallow) are helped
#: by a full moon, while the ones that hunt at night by chemoreception and lateral line (wels
#: catfish, channel catfish, zander in clear water) are hindered by exactly the same moon.
#: Anglers already know this as "the moon was too bright for the cats"; the model states it as a
#: signed number so it can be calibrated against logged catches instead of argued about.
MOONLIGHT_EFFECT: dict[str, float] = {
    "perch": 0.25,
    "pike": 0.45,
    "zander": -0.65,
    "wels_catfish": -0.75,
    "carp": -0.25,
    "tench": 0.0,
    "bream": 0.10,
    "roach_dace": -0.10,
    "barbel": -0.20,
    "brown_trout_sea_trout": 0.15,
    "rainbow_brook_trout": 0.0,
    "atlantic_salmon": 0.10,
    "walleye": 0.35,
    "largemouth_bass": 0.35,
    "smallmouth_spotted_bass": 0.20,
    "muskellunge": 0.30,
    "channel_catfish": -0.55,
    "European_bass": 0.50,
    "flounder_plaice": 0.60,
    "coastal_cod": 0.55,
    "mackerel_horsey_mackerel": 0.0,
    "mixed_general": 0.0,
}

PROFILE_LIST = tuple(
    p if p.moonlight_effect else replace(p, moonlight_effect=MOONLIGHT_EFFECT.get(p.id, 0.0))
    for p in PROFILE_LIST
)

PROFILES: dict[str, SpeciesProfile] = {p.id: p for p in PROFILE_LIST}
_BY_LOWER_ID: dict[str, SpeciesProfile] = {p.id.lower(): p for p in PROFILE_LIST}


def profile(species_id: str | None) -> SpeciesProfile:
    """Look up a profile, falling back to the mixed profile rather than failing."""
    if not species_id:
        return PROFILES["mixed_general"]
    key = str(species_id).strip().lower().replace(" ", "_")
    # match case-insensitively: ids such as "European_bass" contain capitals, so a plain
    # lowercase dict lookup would silently hand the user the mixed profile instead
    return _BY_LOWER_ID.get(key, PROFILES["mixed_general"])


def species_ids() -> list[str]:
    return [p.id for p in PROFILE_LIST]


def species_names() -> dict[str, str]:
    return {p.id: p.name for p in PROFILE_LIST}
