# Fishing Forecast — architecture

The whole point of this design: **the science lives in a dependency-free, Home-Assistant-free
package** (`custom_components/fishing/model/`). Everything HA-related is thin glue.

That buys three things:

1. the model is unit-testable with stdlib `unittest` (no HA, no pip, no network),
2. the model is runnable outside HA (`scripts/demo.py`) so a human can eyeball a forecast,
3. the HA layer can never corrupt the science (it only feeds it numbers and renders them).

```
                         ┌──────────────────────────────────────────┐
  Open-Meteo             │  model/  (pure python, zero deps)        │
  ── forecast ─────────► │  astronomy.py   sun+moon ephemeris       │
  ── marine (SST/waves)  │                 rise/set/transit, phase  │
  ── flood (discharge)   │  solunar.py     feeding windows          │
  ── air quality (opt)   │  water_temp.py  1-D thermal lag model    │
  ── geocoding (setup)   │  pressure.py    tendency, anomaly, fronts│
                         │  hydro.py       runoff / turbidity / plume│
                         │  climatology.py local normals (92 d)     │
                         │  species.py     22 species profiles      │
                         │  context.py     SpotContext (HA↔model seam)│
                         │  scoring.py     weighted factor engine   │
                         │  advice.py      reasons + tactics        │
                         └───────────────┬──────────────────────────┘
                                         │ Context / Eval results
  ┌──────────────────────────────────────▼──────────────────────────┐
  │ HA glue                                                         │
  │  api/open_meteo.py     aiohttp client, tolerant parsing          │
  │  coordinator.py        FishingCoordinator (30 min)               │
  │                      + ClimatologyCoordinator (12 h, stored)     │
  │                      + 5-min local re-evaluation (no network)    │
  │  sensor / binary_sensor / calendar / select / number / button    │
  │  services.yaml       fishing.predict, fishing.refresh           │
  │  card.py             serves + auto-registers the Lovelace card  │
  │  diagnostics.py, system_health.py                                │
  └─────────────────────────────────────────────────────────────────┘
                                         │
  www/fishing-card.js (inside the integration) ◄── single self-contained file (no Lit, no build step)
```

## Two-phase compute (why the UI is instant and the API budget is small)

`refresh` (network, every 30 min by default):
fetch weather → build `Context` (water temperature series, sun/moon events, solunar
windows, climatology baselines, hourly score table, daily score table, analog days).

`tick` (no network, every 5 min):
`evaluate(context, now, species)` re-derives "the score right now" from cached arrays.
Solunar proximity, golden hour and pressure trend all move continuously, so the score
glides between refreshes instead of stepping once per poll.

## Data sources and graceful degradation

| Source                          | What it adds                        | If it fails |
|---------------------------------|-------------------------------------|-------------|
| `api.open-meteo.com/v1/forecast`| air temp, pressure, wind, clouds, CAPE, UV, precip, sunrise/sunset, 92-day past | entity unavailable, last-good kept, `binary_sensor.fishing_stale_data` on |
| `marine-api.open-meteo.com`     | sea surface temperature, waves (coastal only; inland is `null`) | water temp falls back to the thermal-lag model |
| `flood-api.open-meteo.com`      | river discharge → current/turbidity state | factor dropped, weights renormalised |
| `air-quality-api.open-meteo.com`| PM2.5/dust (optional, off by default) | factor dropped |
| `geocoding-api.open-meteo.com`  | setup-time place search             | manual coordinates step always available |
| **your own HA entities**        | barometer, thermometer, hygro, wind gauge, water temp probe, HA `weather.*` forecast | overrides are optional; each is validated and converted |

Every factor carries a weight; missing factors are dropped and the remaining weights are
renormalised, and `confidence` drops with them. The score is never invented out of thin air.

## Multi-spot

One config entry = one fishing spot = one device. People fish more than one water, and the
card only needs the score sensor: it discovers its siblings through
`attributes["siblings"]`, so a card instance is configured by picking exactly one entity.

---

# Design rationale (the parts that make it a *forecast*, not a report)

## 1. Score is an anomaly, not a bias

The trap in every fishing-app score: "high pressure is bad, low pressure is good". At 06:00
in the Netherlands a falling barometer means a front is arriving (fish feed hard); the same
falling barometer at 300 m altitude in January means katabatic drainage, and "1013 hPa" is
not even the same weather at Amsterdam and at Enkhuizen's polder rim.

So every environmental input is reduced to an **anomaly against this spot's own climatology**
plus a **response curve**, and the *response curve* carries the sign knowledge:

    factor_score(x) = response(x - normal(local, season))     # -1 .. +1
    score           = Σ w_i · factor_i / Σ w_i                # renormalised on missing data

Climatology is built locally (92-day past + 92-day forecast from Open-Meteo, refined over
time into `climatology_<spot>.json`), not imported from a global table, so a user in Kerala
gets Kerala's normals without any lookup service.

## 2. Elevation is a first-class variable, not an afterthought

Sea-level pressure at a spot 300 m up is ~35 hPa lower than the sea-level value everyone
quotes. Two failure modes this kills:

* comparing a user's own sensor reading with a sea-level-reduced forecast value (≈1 hPa per
  8 m: a 300 m error is a *35 hPa* error, i.e. "storm" vs "calm"),
* treating "low pressure" as a universal trigger.

`model/climatology.py` therefore stores **lapse-rate-corrected** baselines
(`-6.5 K/km` for temperature, hypsometric for pressure) and the pressure factor consumes the
*anomaly* only. When the user supplies their own barometer, its raw station pressure is kept
separate from the reduced value, and the two are never mixed.

## 3. Pressure tendency comes from model levels, not from the surface wiggle

The synoptic signal (front approaching) is a column effect; near the ground it is buried
under the diurnal thermal cycle, land/sea breezes and valley drainage. `pressure.py` therefore
computes tendency from the **pressure-level variables** Open-Meteo exposes
(`pressure_1000hPa`, `temperature_850hPa`, …) plus surface pressure, and uses:

* `dP/dt` over 3 h and 24 h (surface, elevation-consistent → offsets cancel),
* thickness/`ΔT` advection proxy (850 hPa temperature tendency) to tell "low pressure because
  a warm front" from "low pressure because a cold low",
* a **front index**: sign flip of `dP/dt` together with a cloud-sheet and wind-direction
  rotation signature → binary "front passage today" with an estimated hour.

## 4. Water temperature is modelled, then corrected

Open-Meteo has no inland water temperature. `water_temp.py` runs a deliberately simple 1-D
Newton-style lag model on the spot's air-temperature history (soil/water thermal inertia,
winter heat storage, ice/albedo suppression when near 0 °C), then fuses, in priority order:

1. an HA sensor the user designates (a probe in the lake) → hard override,
2. Open-Meteo **sea surface temperature** if the spot is coastal (valid for surf fishing),
3. the model value, with `water_temp_source` exposed so the card can say *how* it knows.

Small water bodies get a smaller inertia constant (config entry "water body size"), which is
why a shallow pond reacts to a warm afternoon while a lake does not.

## 5. Light: three different things, not one "day/night" flag

Anglers care about three separate things and the model keeps them separate:

* **golden hour** (sun −6°…+10°: low-angle light, the classic topwater window),
* **moonlight** (`moonshine()` = illumination × altitude × sky darkness — how much light is
  actually on the water at 02:00, which is why a full moon *below the horizon* is worth nothing),
* **ripple/wind state** (chop destroys the visual advantage of low light, but chop *helps*
  sight-feeding fish in bright flat conditions).

The model computes all three, and the species profiles decide which ones they actually use
(pike/perch/take: golden hour + moonlight; carp: sustained warmth + pressure stability;
sea bass: chop + tide state; trout: cold, oxygen, cover).

## 6. Tide: forcing, not forecast

Real high water needs a coastline, a datum and amphidromic charts — none of which belong in a
dependency-free model. `astronomy.tide_state()` instead gives the **astronomical forcing**:
equilibrium semi-diurnal level, spring/neap amplitude, perigee amplification
(`(mean/dist)³`) and approximate extreme instants. It is exposed as
`sensor.fishing_tide_state` + `tide_forcing` and always labelled as forcing. Users near a real
tide gauge point the integration at their HA tide sensor instead, and that becomes the hard
override — same pattern as water temperature.

## 7. Calibration loop (the honest part)

`button.fishing_log_conditions` snapshots the current factor vector; `number.fishing_catch_rate`
(or a `fishing.log_catch` service with species/count/method) attaches an outcome. Weeks of the
user's own log let `model/calibrate.py` re-fit factor weights for *this* spot and species
(ridge-regularised, bounded to ±40 % of the priors so ten rainy Tuesdays can't break it), and
report the change in `sensor.fishing_model_r_squared`. The default model is a prior; the user's
log makes it theirs. Nothing is claimed beyond "this correlated with your catches".

## 8. Verification strategy

External references are generated **into committed fixtures** by dev-only scripts, so the test
suite itself needs no network and no third-party packages:

| fixture | generated by | reference |
|---------|--------------|-----------|
| `tests/fixtures/lunar_reference.json` | `scripts/regen_lunar_series.py` | astral's full Meeus ch. 33 lunar theory, 60 epochs over 1950-2100 |
| `tests/fixtures/events_reference.json` | `scripts/regen_event_fixtures.py` | astral `sunrise/sunset/noon/moonrise/moonset` for 6 sites × 40-75 days |

Why astral: Home Assistant core *itself* computes its sun/moon entities with astral, so
agreement with astral is the definition of "not visibly contradicting the rest of HA".

Accuracy currently achieved (all asserted in `tests/test_astronomy.py`):

* lunar geocentric position: rms **0.97′**, max **2.20′**, distance within ~250 km;
* sunrise/sunset/solar noon: worst **24 s / 20 s / 2 s** at mid latitudes;
* moonrise/moonset: worst **≤2.3 min** (astral's own search is the coarser side);
* plus two checks that don't depend on any reference at all:
  * **altitude identity** — at every event we report, the body's computed altitude equals the
    horizon altitude we solved for (<0.02°), at all six sites including the polar ones;
  * **structural coherence** — events ordered dawn→noon→dusk, positive day length, night
    windows one ordered sequence even when the night wraps past local midnight.

The lunar coefficients in `astronomy.py` are **derived, not hand-transcribed**:
`scripts/regen_lunar_series.py` fits an abridged truncated series (20 longitude / 10 latitude /
12 radius terms) to astral's full theory with orthogonal matching pursuit over canonical
`(D, M_sun, M_moon, F)` arguments, and pastes the surviving coefficients into
`LUNAR_LON_TERMS` / `LUNAR_LAT_TERMS` / `LUNAR_RAD_TERMS`. Hand-copying Meeus's tables is where
this kind of code usually dies; a fit has auditable residuals and a reproducible generator.

## 9. Known limits (stated, not hidden)

* Polar edge cases: near the Arctic/Antarctic circles the Sun can be under water for three
  minutes around local midnight. Our search is graze-aware (`_crossings` re-samples near the
  horizon) and we report the crossings that **bracket solar noon**, which keeps day length
  positive; astral instead sometimes reports a sunset *earlier* than the sunrise of the same
  date. No cross-agreement is asserted at those sites — only the altitude identity.
* Equilibrium tide is forcing, not a forecast (see §6).
* 1-D water temperature is a lag model, not a hydrodynamic simulation: it will not know about
  a thermocline turnover, a power-plant plume or a river flood pulse; that is what the
  override entities are for.
* Open-Meteo caveats found by probing: marine variables need `marine-api.open-meteo.com`
  (the forecast endpoint rejects them), `sea_surface_height` does not exist, cloud variables
  are `cloud_cover_low|mid|high`, inland points return `null` for sea variables, and the flood
  endpoint works inland (so "river discharge today vs its own 30-day baseline" needs no
  national gauge API).

## 10. Status / next steps

* [x] architecture
* [x] model: time, angles, Julian date
* [x] model: solar position, sunrise/sunset/twilight/transit
* [x] model: lunar position/phase (fitted abridged series, regenerated by script)
* [x] model: moonrise/moonset/transit, horizon dip for observer elevation
* [x] model: `day_light()` + `night_windows()` (ordering across midnight, golden-hour fallbacks)
* [x] model: equilibrium tide forcing (spring/neap, perigee, extremes)
* [x] tests: `tests/test_astronomy.py` — 18 tests, stdlib only, offline, ~43 s
* [x] model: climatology/normalisation (seasonal baselines, lapse-rate correction) — tested
* [x] model: `pressure.py` (tendency, anomaly, front index), `water_temp.py`, `hydro.py` — tested
* [x] model: `solunar.py` windows, `species.py` (22 profiles), `series.py`, `context.py` — tested
* [x] tests: series/pressure/water_temp/solunar/hydro/species/context — 218 offline tests in total, ~45 s
* [x] model: `scoring.py` — weighted factor engine, confidence shrinkage, hourly curve, best spans (tests: acceptance cases + 22 species x 6 kinds x temperate/polar sweep)
* [x] model: `advice.py` — condition rules (48 + default), reasons, tactics, headline, timing advice
* [x] `scripts/demo.py` — offline 48 h table and advice; weights reviewed by eye (scores sit mostly in 5-7.5: compressed, not absurd)
* [x] data: `api/open_meteo.py` client + tolerant parsers (tested on recorded real responses), `spot.py`, `snapshot.py`, `refresh.py`
* [~] HA: coordinator, config + options flow (geocoding), sensors/binary sensors/select, `fishing.refresh`, diagnostics — tested in a real Home Assistant test harness (26 tests, `tests/ha`); **not yet loaded on the live instance**
* [x] card: `custom_components/fishing/www/fishing-card.js` — built, tested in Node (43 tests); served and auto-loaded by the integration (`card.py`); **not yet seen in a real browser**

Decisions worth not losing:

* `hydro.py` also models an **estuary/coast/lake plume** (`plume_index`): river outflow colours the
  water near a mouth independently of the gauge's own turbidity proxy, so it only applies to those
  spot kinds.
* `species.MOONLIGHT_EFFECT` is a signed per-species coefficient (wels < 0 < flounder). Moonlight
  is a *light* effect gated to night (`moonshine`), kept separate from the solunar timing prior.
* `SpotContext.tz_offset_min` (fixed offset) takes precedence over `tz_name`, so a spot can be
  evaluated at a true local time with no tz database.
* `series.sum_over` clamps negative values to 0 (rain totals cannot be negative).

* Events are the crossings that **bracket the day's solar noon** (`_jd_bracketing`), not "the
  first inside the calendar day": at 64°N in June the Sun is up 02:54→00:02(+1 d), and a
  midnight-clipped pick makes day length negative. Deep twilight levels may therefore spill a
  couple of hours past midnight — that is "the night that began on this date".
* `night_windows()` is what scoring must use for "tonight": DayLight's per-day fields cannot
  express a night that wraps past local midnight (civil dusk on the 7th, nautical dusk on the
  8th at 52°N in May).
* Tests stay tz-database free by rebuilding the observer's fixed UTC offset from the fixture
  (`utc_offset_min`); `zoneinfo` is only used in the dev-only fixture generators.
