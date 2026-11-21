# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Start every session by reading **`HANDOVER.md`** (current state, verified APIs, what to build next)
and then **`docs/ARCHITECTURE.md`** (design rationale). `HANDOVER.md` is authoritative for status;
do not re-derive facts listed there (Open-Meteo quirks, astronomy semantics, test commands).

## Hard rules

* **Never put `custom_components/fishing` at the *front* of `sys.path`** (`sys.path.append` only): it contains
  `select.py`, which would shadow the standard library's `select` and break `socket`/`asyncio`.
* `custom_components/fishing/model/` imports **stdlib only** — no `homeassistant`, `aiohttp`,
  `numpy`, `astral`. HA is thin glue around it.
* Tests: stdlib `unittest`, offline, no network, no third-party imports, no tz database.
  ```bash
  python -m unittest discover -s tests           # gate
  python -m unittest tests.test_response tests.test_climatology
  scratch/ha314/Scripts/python.exe -m pytest tests/ha -q   # 26 Home Assistant-level tests (real hass; Windows shims live in that venv)
  node --test "tests/card/*.test.mjs"            # the card: 43 tests, Node 22, no npm (quote the glob)
  python -m unittest tests.test_astronomy        # ~21 s (every 2nd fixture day); FISHING_FULL_ASTRO=1 for every day (~42 s)
  python -m unittest tests.test_response.SomeClass.test_name   # single test
  ```
  Dev-only fixture regeneration (needs the venv; never run with system Python):
  ```bash
  scratch/venv/Scripts/python.exe scripts/regen_lunar_series.py
  scratch/venv/Scripts/python.exe scripts/regen_event_fixtures.py   # also needs a tz db
  ```
  There is no linter/build step. Use `python` (not `python3`) and forward-slash paths on this Windows box.
* The **system Python has no IANA tz database** (`ZoneInfo` fails for every key). `model/` must
  fall back to `timezone.utc`; tests use fixed offsets. `astral`/`numpy` live only in
  `scratch/venv/` and `scripts/regen_*.py` (dev-only fixture regeneration).
* One config entry = one fishing spot = one device. Missing data ⇒ drop the factor, renormalise
  weights, lower `confidence`, explain it in `notes`. Never invent a score.
* Light/timing comes from **sun altitude**, never clock hour. Lunar coefficients in
  `astronomy.py` are **fitted** — re-derive with `scripts/regen_lunar_series.py`, never hand-edit.
* Card must stay one self-contained `custom_components/fishing/www/fishing-card.js` (no Lit, no npm, no build step);
  it lives inside the integration so a HACS install carries it.

## Architecture in brief

Data flows `HA coordinator → SpotContext (model/context.py) → per-factor analysers
(pressure, water_temp, hydro, solunar, astronomy) → scoring → advice → sensors/card`.
`SpotContext` is the only HA↔model seam. Every time series everywhere is `Sample = (datetime, float)`;
all factor curves live in `response.py` and are combined by `weighted_score`, with per-species
weights from `species.py` (13 allowed factor keys). Status: `model/` science modules exist and every
one has offline tests, including `scoring.py` (factor engine) and `advice.py` (condition rules, reasons,
tactics); `python scripts/demo.py` runs the whole model offline and prints a 48 h table. The HA glue is written
(`api/`, `spot.py`, `snapshot.py`, `refresh.py` are pure and tested against recorded real Open-Meteo responses;
`coordinator.py`, `config_flow.py` and the entity platforms import Home Assistant and are tested in a real one:
`tests/ha/`, run with the Python 3.14 env in `scratch/ha314`, NOT part of the stdlib gate).
`custom_components/fishing/www/fishing-card.js` (the Lovelace card, one file, no framework) is tested in Node with a fake DOM; real-browser
rendering is unverified. `python scripts/package.py` builds `dist/fishing.zip` (integration + the card inside it). Shared synthetic weather
history lives in `tests/support.py`.

Landmines that span files:
* `astronomy` returns **aware-UTC** datetimes even for naive input; series are in the spot's local wall
  clock. Never compare them directly — normalise to all-naive or all-aware within one evaluation.
* Pressure tendency must use the diurnal-tide-corrected value, or the semi-diurnal tide fakes a falling barometer.
* Day/night events: `day_light()` brackets solar noon, `body_events()` takes first-in-calendar-day,
  `night_windows()` handles nights that wrap midnight. Don't "simplify" these.
* Polar sites: never assert agreement with `astral` (it is unreliable there); assert altitude identities instead.
* `scoring` takes a **naive local** `when` (aware is converted) and calls astronomy with an aware one; always use
  `scoring.to_local/to_aware/local_event` rather than comparing astronomy datetimes with series times.
* Series units: cloud and humidity are **percent (0-100)**; the flux functions in `water_temp` take fractions,
  so `model_series` converts. `series` window helpers assume a *sorted* (cleaned) series.

## Style

Scores are −1..+1 inside the model, 0–10 only at the UI. Factors return `None` when uncomputable.
Docstrings explain *why* in plain language, usually naming the angling observation that motivated
the term — keep that voice, it is what makes the science reviewable.
