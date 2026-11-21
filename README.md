# Fishing Forecast

A Home Assistant integration that tells you **how good the fishing is right now and over the next 48
hours at one specific mark, and why** - with a Lovelace card to show it.

It looks at the weather, barometric pressure and approaching fronts, water temperature, the sun and
moon, tide, river level and water colour, and how unusual all of that is for *your* spot at this time of
year. It then explains the score in plain language ("water is 6.4 degC, 9.6 below the pike optimum",
"a front is likely, passing in about 3 h") and suggests what to do about it.

> **Status: early (0.1.0).** The model and the integration are covered by automated tests, but it has had
> little use on real water yet. Treat the scores as a well-reasoned starting point, not gospel, and see
> [Limits](#limits).

## What you get

One config entry is one fishing spot, shown as one device with these entities:

| Entity | What it shows |
|---|---|
| **Score** (sensor) | 0-10, with the band (terrible / poor / fair / good / excellent), a headline, the reasons, what to do, and every factor as attributes |
| **Confidence** (sensor) | How much of the usual data the score is built on, in percent |
| **Water temperature** (sensor) | Probe reading if you give it one, else sea-surface temperature, else a heat-budget model; says which |
| **Pressure tendency** (sensor) | hPa per 3 h, corrected for the daily pressure tide, with the trend and any front |
| **Wind speed** (sensor) | Wind at the spot, in your unit system (the direction is in its attributes) |
| **Wind direction** (sensor) | The bearing the wind blows *from*, in degrees, with the compass point as an attribute |
| **Forecast peak** (sensor) | The best score in the next 48 h; the hourly curve is in its attributes |
| **Next best window** (sensor) | When the next good spell starts (a timestamp) |
| **Feeding time** (binary sensor) | On inside a solunar window or golden hour when conditions are at least fair |
| **Stale data** (binary sensor) | On when the forecast being scored is older than it should be |
| **Target species** (select) | Pick what you are fishing for; the whole spot re-scores for it |

It also provides the `fishing.refresh` service (fetch fresh weather now) and a redacted diagnostics download.

### Species

Scored per species, each with its own temperature optimum, season, light preference and moonlight response:

- **Predators:** European perch, northern pike, zander, walleye, largemouth bass, smallmouth / spotted bass, musky
- **Catfish:** wels catfish, channel catfish
- **Cyprinids:** common carp, tench, common bream, roach / dace, barbel
- **Salmonids:** brown trout / sea trout, rainbow / brook trout, Atlantic salmon
- **Marine:** European bass (sea bass), flounder / plaice, cod, mackerel
- **General:** a mixed / opportunistic profile for when you do not know yet

## Install

### With HACS (recommended)

1. In Home Assistant open **HACS** (on 2026.11 and later this is the built-in Marketplace).
2. Menu -> **Custom repositories** -> add `https://github.com/NikolaPopovski/fishing-forecast`, category **Integration**.
3. Download **Fishing Forecast**, then **restart Home Assistant**.
4. **Settings -> Devices & services -> Add integration -> Fishing Forecast.**

### Manually

Copy the `custom_components/fishing` folder into `/config/custom_components/` and restart. (`python
scripts/package.py` builds `dist/fishing.zip` containing exactly that folder.)

## Set up a spot

1. **Where?** Type a place name (searched with Open-Meteo's geocoder) or leave it empty and enter coordinates.
2. **What water?** Kind of water, size, tidal or not, the land around it, elevation, and optionally a real
   water-temperature sensor. The defaults suit a small lake.
3. **What fish?** The target species (changeable later from the select entity).

Later, **Configure** on the integration lets you change all of that and describe how **sheltered the bank is
from each wind direction** and which way it faces. Add the integration again for each additional mark.

## The card

The integration serves the card and loads it on every dashboard, so there is nothing to install. Add it from
the card picker ("Fishing Forecast") or in YAML:

```yaml
type: custom:fishing-card
entity: sensor.my_lake_score      # the spot's "Score" sensor
```

Options:

| Option | Default | |
|---|---|---|
| `view` | `default` | `default`, `compact` (gauge and headline only) or `kiosk` (large, no controls) |
| `hours` | `48` | Length of the forecast sparkline, 6-48 |
| `name` | spot name | Title override |
| `show_forecast`, `show_factors`, `show_advice`, `show_water`, `show_wind`, `show_species_selector`, `show_details` | `true` | Switch sections off |

The card shows the score and band word, confidence, water temperature with its source, the wind (speed, compass
point and degrees, with an arrow pointing the way it is blowing), the next best window
(with a countdown), a sparkline with the best windows shaded, the top factors with their reasons, advice, and a
"Why this score" table. It says so when data is stale or confidence is low, and lists what is missing.

## How it works, briefly

- **Weather, tides and rivers** come from [Open-Meteo](https://open-meteo.com/) (forecast, marine, flood and
  archive APIs), fetched every 30 minutes; the score is recomputed every 5 minutes from the cached data.
- **Light and timing come from the sun's altitude**, never the clock hour, so it works at any latitude.
- Each of up to 13 factors (water temperature, pressure, fronts, wind, shelter, cloud, brightness, rain,
  water level and colour, solunar timing, light, moonlight, tide) returns a score from -1 to +1; the species
  profile weights them.
- **Missing data is dropped, never invented.** The remaining weights are renormalised, confidence goes down,
  and the card says what was missing. The shown score is pulled toward "average" as confidence falls.
- **Local normals:** it builds normals for your spot from years of archive data, so it can say "warmer than
  normal for November" rather than just a number.
- **Solunar timing** is used as a timing prior with a labelled mechanism (moon transit, rise and set, the lunar
  clock), not as a prediction on its own.

## Limits

- **It predicts from weather and astronomy, not from catches.** There is no feedback from your results yet, so
  it cannot learn your water. The scores are mostly between 5 and 7.5; use them to compare hours and days at
  one spot rather than as absolute truth.
- **Water temperature is a model** unless you give it a real probe; small ponds and streams are hardest.
- Some advice thresholds (what counts as cold, warm or a gale for a species) are reasoned estimates.
- English only. The card has no visual editor (YAML only).
- Tested against Home Assistant 2026.10.4. Anything newer should work but has not been run.

## License

[MIT](LICENSE). Issues and ideas: https://github.com/NikolaPopovski/fishing-forecast/issues

## Data and credit

Weather data: [Open-Meteo.com](https://open-meteo.com/), licensed
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Open-Meteo's free service is for non-commercial
use; please keep that in mind and do not hammer it (this integration refreshes every 30 minutes).

## Development

```bash
python -m unittest discover -s tests            # the model and the pure integration code: stdlib only, offline
node --test "tests/card/*.test.mjs"             # the Lovelace card (Node 22, no npm)
python -m pytest tests/ha                       # needs Home Assistant installed (see HANDOVER.md)
python scripts/demo.py                          # prints a 48 h forecast table for three synthetic spots
```

`HANDOVER.md` and `docs/ARCHITECTURE.md` describe the design, what is verified, and the gotchas.
