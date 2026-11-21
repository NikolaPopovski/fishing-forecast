/*
 * fishing-card.js - Lovelace card for the "Fishing Forecast" integration.
 *
 * One self-contained file: no framework, no imports, no build step. Install by dropping it in
 * /config/www/ and adding /local/fishing-card.js as a dashboard resource (JavaScript module), or let
 * the integration serve and register it for you.
 *
 *   type: custom:fishing-card
 *   entity: sensor.my_lake_score          # required: the "Score" sensor of the spot
 *   view: default                         # default | compact | kiosk
 *   hours: 48                             # forecast sparkline length (6..48)
 *   name: Optional title override
 *   show_forecast: true
 *   show_factors: true
 *   show_advice: true
 *   show_water: true
 *   show_wind: true                     # wind speed and the direction it blows from
 *   show_species_selector: true
 *   show_details: true
 *
 * How it works: everything the card shows comes from the integration's entities, found through the
 * score sensor's device. The card does no fishing arithmetic of its own. It must never throw because
 * an entity is missing or unavailable: it shows what it has and says what it lacks.
 */
(() => {
  "use strict";

  const VERSION = "0.1.1";
  const TAG = "fishing-card";

  // ---------------------------------------------------------------------------------------
  // pure helpers (everything below is testable without a browser)
  // ---------------------------------------------------------------------------------------

  /** Escape text for innerHTML. Every dynamic string goes through this. */
  const esc = (value) =>
    String(value === null || value === undefined ? "" : value).replace(
      /[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
    );

  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const toNum = (v) => {
    if (v === null || v === undefined || v === "" || v === "unknown" || v === "unavailable") return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const asArray = (v) => (Array.isArray(v) ? v : []);

  /** The same words the integration uses; thresholds mirror scoring.band() on the 0..10 scale. */
  const BANDS = [
    { id: "terrible", below: 2.5, label: "Terrible" },
    { id: "poor", below: 4.0, label: "Poor" },
    { id: "fair", below: 5.5, label: "Fair" },
    { id: "good", below: 7.0, label: "Good" },
    { id: "excellent", below: Infinity, label: "Excellent" },
  ];

  function bandFor(score, given) {
    const found = BANDS.find((b) => b.id === given);
    if (found) return found;
    if (!isNum(score)) return null;
    return BANDS.find((b) => score < b.below);
  }

  /** Gauge geometry: a half circle of radius R whose stroke grows with the score. */
  const GAUGE = { r: 80, cx: 100, cy: 100 };
  GAUGE.length = Math.PI * GAUGE.r;
  GAUGE.path = `M ${GAUGE.cx - GAUGE.r} ${GAUGE.cy} A ${GAUGE.r} ${GAUGE.r} 0 0 1 ${GAUGE.cx + GAUGE.r} ${GAUGE.cy}`;
  const gaugeFill = (score) => (isNum(score) ? (clamp(score, 0, 10) / 10) * GAUGE.length : 0);

  /** 16-point compass; a bearing is the direction the wind blows FROM, as in every weather report. */
  const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  const COMPASS_WORDS = [
    "north", "north-northeast", "northeast", "east-northeast", "east", "east-southeast", "southeast", "south-southeast",
    "south", "south-southwest", "southwest", "west-southwest", "west", "west-northwest", "northwest", "north-northwest",
  ];
  const compassIndex = (deg) => (isNum(deg) ? Math.floor((((deg % 360) + 360) % 360 + 11.25) / 22.5) % 16 : null);
  const compass = (deg) => (compassIndex(deg) === null ? null : COMPASS[compassIndex(deg)]);
  const compassWords = (deg) => (compassIndex(deg) === null ? null : COMPASS_WORDS[compassIndex(deg)]);

  /** "2026-11-21T13:00:00" (the spot's wall clock, no zone) as a Date that prints the same digits in UTC. */
  function parseWallClock(text) {
    if (typeof text !== "string") return null;
    const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(text);
    if (!m) return null;
    return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]));
  }

  function formatClock(date, locale) {
    if (!date) return "";
    try {
      return new Intl.DateTimeFormat(locale || undefined, { hour: "2-digit", minute: "2-digit", timeZone: "UTC" }).format(date);
    } catch (e) {
      return date.toISOString().slice(11, 16);
    }
  }

  function formatInstant(ms, locale, timeZone) {
    if (!isNum(ms)) return "";
    try {
      return new Intl.DateTimeFormat(locale || undefined, { hour: "2-digit", minute: "2-digit", timeZone: timeZone || undefined }).format(new Date(ms));
    } catch (e) {
      return new Date(ms).toISOString().slice(11, 16);
    }
  }

  function formatNumber(n, locale, digits = 1) {
    if (!isNum(n)) return "-";
    try {
      return new Intl.NumberFormat(locale || undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 }).format(n);
    } catch (e) {
      return String(Math.round(n * 10 ** digits) / 10 ** digits);
    }
  }

  /** "in 3 h 12 min", "in 45 min", "in under a minute". */
  function formatDuration(ms) {
    if (!isNum(ms) || ms < 0) return "";
    const minutes = Math.round(ms / 60000);
    if (minutes < 1) return "under a minute";
    if (minutes < 60) return `${minutes} min`;
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    if (h >= 48) return `${Math.round(h / 24)} days`;
    return m ? `${h} h ${m} min` : `${h} h`;
  }

  /** The window the user should care about: now, soon, or none. */
  function countdown(bestState, attrs, nowMs, locale, timeZone) {
    const start = Date.parse(bestState);
    const endRaw = attrs && attrs.end_utc ? Date.parse(attrs.end_utc) : NaN;
    if (!Number.isFinite(start)) return { kind: "none", text: "No strong window in the next 48 hours" };
    const end = Number.isFinite(endRaw) ? endRaw : start + 3600000;
    const range = `${formatInstant(start, locale, timeZone)}–${formatInstant(end, locale, timeZone)}`;
    if (nowMs >= start && nowMs < end) return { kind: "now", text: `Best window now, until ${formatInstant(end, locale, timeZone)}`, range };
    if (nowMs < start) return { kind: "soon", text: `Next best window in ${formatDuration(start - nowMs)} (${range})`, range, ms: start - nowMs };
    return { kind: "none", text: "No strong window in the next 48 hours" };
  }

  /** The factors that moved the score most, biggest first (unavailable ones never count). */
  function topFactors(factors, n = 3) {
    return asArray(factors)
      .filter((f) => f && f.source !== "unavailable" && isNum(f.contribution))
      .sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution))
      .slice(0, n);
  }

  /**
   * Sparkline geometry in a W x H box. `points` are {time, score}; scores run 0..10, bottom to top.
   * Best windows (start/end as the same wall-clock strings as the points) become shaded spans.
   */
  function sparkline(points, windows, w = 300, h = 64) {
    const pts = asArray(points).filter((p) => p && isNum(p.score));
    if (pts.length < 2) return null;
    const step = w / (pts.length - 1);
    const y = (s) => +(h - (clamp(s, 0, 10) / 10) * h).toFixed(1);
    const coords = pts.map((p, i) => ({ x: +(i * step).toFixed(1), y: y(p.score), time: p.time, score: p.score }));
    const line = coords.map((c, i) => `${i ? "L" : "M"} ${c.x} ${c.y}`).join(" ");
    const area = `${line} L ${coords[coords.length - 1].x} ${h} L 0 ${h} Z`;
    const index = new Map(pts.map((p, i) => [p.time, i]));
    const spans = [];
    for (const win of asArray(windows)) {
      if (!win) continue;
      const a = index.get(win.start);
      if (a === undefined) continue;
      let b = index.get(win.end);
      if (b === undefined) b = pts.length - 1;
      spans.push({ x: +(a * step).toFixed(1), width: +(Math.max(1, b - a) * step).toFixed(1) });
    }
    return { w, h, line, area, coords, spans, good: y(6), best: coords.reduce((m, c) => (c.score > m.score ? c : m), coords[0]) };
  }

  /** The integration's entities, found through the score sensor's device (with a naming fallback). */
  const KEYS = ["score", "confidence", "water_temperature", "pressure_tendency", "wind_speed", "wind_direction", "forecast", "best_window", "feeding_time", "stale_data", "species"];
  const SUFFIXES = {
    confidence: ["sensor", "confidence"],
    water_temperature: ["sensor", "water_temperature"],
    pressure_tendency: ["sensor", "pressure_tendency"],
    wind_speed: ["sensor", "wind_speed"],
    wind_direction: ["sensor", "wind_direction"],
    forecast: ["sensor", "forecast_peak"],
    best_window: ["sensor", "next_best_window"],
    feeding_time: ["binary_sensor", "feeding_time"],
    stale_data: ["binary_sensor", "stale_data"],
    species: ["select", "target_species"],
  };

  function findEntities(hass, scoreId, overrides) {
    const found = { score: scoreId };
    const registry = (hass && hass.entities) || {};
    const me = registry[scoreId];
    if (me && me.device_id) {
      for (const [id, ent] of Object.entries(registry)) {
        if (ent && ent.device_id === me.device_id && ent.translation_key && KEYS.includes(ent.translation_key) && !found[ent.translation_key]) {
          found[ent.translation_key] = id;
        }
      }
    }
    const states = (hass && hass.states) || {};
    const base = /^sensor\.(.+)_score$/.exec(scoreId);
    if (base) {
      for (const key of Object.keys(SUFFIXES)) {
        const guess = `${SUFFIXES[key][0]}.${base[1]}_${SUFFIXES[key][1]}`;
        if (!found[key] && states[guess]) found[key] = guess;
      }
    }
    return Object.assign(found, overrides || {});
  }

  const DEFAULTS = {
    view: "default",
    hours: 48,
    show_forecast: true,
    show_factors: true,
    show_advice: true,
    show_water: true,
    show_wind: true,
    show_species_selector: true,
    show_details: true,
  };

  function normaliseConfig(config) {
    if (!config || typeof config.entity !== "string" || !config.entity.includes(".")) {
      throw new Error("fishing-card: set `entity` to the spot's Score sensor (for example sensor.my_lake_score)");
    }
    const cfg = Object.assign({}, DEFAULTS, config);
    if (!["default", "compact", "kiosk"].includes(cfg.view)) cfg.view = "default";
    cfg.hours = clamp(Math.round(toNum(cfg.hours) || 48), 6, 48);
    if (cfg.view === "compact") {
      Object.assign(cfg, { show_forecast: false, show_factors: false, show_advice: false, show_details: false });
    } else if (cfg.view === "kiosk") {
      Object.assign(cfg, { show_species_selector: false, show_details: false });
    }
    return cfg;
  }

  const SOURCE_LABELS = {
    sensor: "probe",
    sst: "sea satellite",
    model: "model estimate",
    sensor_stale: "old probe reading",
    none: "unknown",
  };

  /** Everything the renderer needs, computed from hass. Never throws. */
  function viewModel(hass, config, nowMs) {
    const locale = (hass && hass.locale && hass.locale.language) || undefined;
    const timeZone = (hass && hass.config && hass.config.time_zone) || undefined;
    const states = (hass && hass.states) || {};
    const ids = findEntities(hass, config.entity, config.entities);
    const st = (key) => (ids[key] && states[ids[key]]) || null;
    const scoreState = st("score");
    if (!scoreState) return { error: `Entity ${config.entity} was not found. Is the Fishing Forecast integration loaded?`, ids };

    const a = scoreState.attributes || {};
    const score = toNum(scoreState.state);
    const band = bandFor(score, a.band);
    const confState = st("confidence");
    const confidence = confState ? toNum(confState.state) : null;
    const notes = asArray(a.notes);
    const waterState = st("water_temperature");
    const water = waterState && toNum(waterState.state) !== null ? waterState : null;
    const waterAttrs = (waterState && waterState.attributes) || {};
    const pressureState = st("pressure_tendency");
    const windSpeedState = st("wind_speed");
    const windDirState = st("wind_direction");
    const windSpeed = windSpeedState ? toNum(windSpeedState.state) : null;
    // the direction sensor wins; the speed sensor carries a copy as an attribute for installs that only have that one
    const windDir = (windDirState && toNum(windDirState.state)) ?? (windSpeedState ? toNum((windSpeedState.attributes || {}).direction) : null);
    const forecastState = st("forecast");
    const fa = (forecastState && forecastState.attributes) || {};
    const bestState = st("best_window");
    const feeding = st("feeding_time");
    const stale = st("stale_data");
    const speciesState = st("species");
    const points = asArray(fa.forecast).slice(0, config.hours);
    const forecastScores = points.filter((p) => p && isNum(p.score));
    const staleAttrs = (stale && stale.attributes) || {};

    return {
      ids,
      locale,
      timeZone,
      title: config.name || (scoreState.attributes && scoreState.attributes.friendly_name ? String(scoreState.attributes.friendly_name).replace(/\s+Score$/i, "") : "Fishing"),
      unavailable: score === null,
      score,
      band,
      headline: a.headline || "",
      speciesName: a.species_name || "",
      confidence,                                                       // percent 0..100 or null
      degraded: (confidence !== null && confidence < 60) || (score === null),
      notes,
      reasons: asArray(a.reasons),
      tactics: asArray(a.tactics),
      window: a.window || "",
      factors: asArray(a.factors),
      topFactors: topFactors(a.factors, 3),
      conditions: asArray(a.conditions),
      water: water && {
        value: toNum(water.state),
        unit: (water.attributes && water.attributes.unit_of_measurement) || "°C",
        source: waterAttrs.source || "model",
        sourceLabel: SOURCE_LABELS[waterAttrs.source] || SOURCE_LABELS.model,
        trend: waterAttrs.trend || "",
        change24h: toNum(waterAttrs.change_24h),
      },
      wind: windSpeed === null && windDir === null ? null : {
        speed: windSpeed,
        unit: (windSpeedState && windSpeedState.attributes && windSpeedState.attributes.unit_of_measurement) || "m/s",
        direction: windDir,
        cardinal: compass(windDir),
        words: compassWords(windDir),
      },
      pressure: pressureState && toNum(pressureState.state) !== null ? { tendency: toNum(pressureState.state), trend: (pressureState.attributes || {}).trend || "" } : null,
      spark: sparkline(points, asArray(fa.best_windows), 300, 64),
      peak: forecastScores.length ? Math.max(...forecastScores.map((p) => p.score)) : null,
      countdown: bestState ? countdown(bestState.state, bestState.attributes, nowMs, locale, timeZone) : null,
      feeding: !!feeding && feeding.state === "on",
      stale: !!stale && stale.state === "on",
      staleError: staleAttrs.error || "",
      staleSince: staleAttrs.last_success || "",
      species: speciesState && speciesState.state !== "unavailable"
        ? { entity: ids.species, current: speciesState.state, options: asArray((speciesState.attributes || {}).options) }
        : null,
    };
  }

  // ---------------------------------------------------------------------------------------
  // rendering (a string of HTML: every dynamic value is escaped)
  // ---------------------------------------------------------------------------------------

  const STYLE = `
    :host { display: block; }
    ha-card { padding: 16px; --fc-terrible: #b3261e; --fc-poor: #d9730d; --fc-fair: #b58900; --fc-good: #2e7d32; --fc-excellent: #0b6b3a; }
    .band-terrible { --band: var(--fc-terrible); } .band-poor { --band: var(--fc-poor); } .band-fair { --band: var(--fc-fair); }
    .band-good { --band: var(--fc-good); } .band-excellent { --band: var(--fc-excellent); } .band-none { --band: var(--secondary-text-color, #727272); }
    header { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
    h2 { margin: 0; font-size: 1.1em; font-weight: 500; color: var(--primary-text-color); }
    select { max-width: 55%; font: inherit; color: var(--primary-text-color); background: var(--card-background-color, #fff); border: 1px solid var(--divider-color, #ccc); border-radius: 6px; padding: 4px 6px; }
    .banner { border-radius: 8px; padding: 8px 12px; margin: 8px 0; font-size: .9em; border-left: 4px solid var(--fc-fair); background: color-mix(in srgb, var(--fc-fair) 12%, transparent); color: var(--primary-text-color); }
    .banner.error { border-left-color: var(--fc-terrible); background: color-mix(in srgb, var(--fc-terrible) 12%, transparent); }
    .banner ul { margin: 4px 0 0; padding-left: 18px; }
    .main { display: grid; grid-template-columns: minmax(120px, 190px) 1fr; gap: 12px 16px; align-items: center; }
    .gauge { text-align: center; }
    .gauge svg { width: 100%; height: auto; display: block; }
    .gauge .track { fill: none; stroke: var(--divider-color, #ddd); stroke-width: 14; stroke-linecap: round; }
    .gauge .fill { fill: none; stroke: var(--band); stroke-width: 14; stroke-linecap: round; }
    .gauge .num { font-size: 40px; font-weight: 600; fill: var(--primary-text-color); text-anchor: middle; }
    .gauge .of { font-size: 12px; fill: var(--secondary-text-color); text-anchor: middle; }
    .bandword { font-weight: 600; color: var(--band); letter-spacing: .02em; }
    .headline { font-size: 1.05em; margin: 0 0 6px; color: var(--primary-text-color); }
    .meter { height: 6px; border-radius: 3px; background: var(--divider-color, #ddd); overflow: hidden; margin: 2px 0 8px; }
    .meter > span { display: block; height: 100%; background: var(--band); }
    .meta { display: flex; flex-wrap: wrap; gap: 6px 14px; color: var(--secondary-text-color); font-size: .9em; }
    .chip { display: inline-flex; align-items: center; gap: 4px; }
    .windarrow { flex: none; }
    .badge { font-size: .75em; border: 1px solid var(--divider-color, #ccc); border-radius: 10px; padding: 0 6px; color: var(--secondary-text-color); }
    .next { margin-top: 8px; font-weight: 500; color: var(--primary-text-color); }
    .next.now { color: var(--band); }
    section { margin-top: 14px; }
    h3 { margin: 0 0 6px; font-size: .85em; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; color: var(--secondary-text-color); }
    .spark { width: 100%; height: auto; display: block; overflow: visible; }
    .spark .area { fill: var(--band); opacity: .14; } .spark .line { fill: none; stroke: var(--band); stroke-width: 2; stroke-linejoin: round; }
    .spark .win { fill: var(--band); opacity: .18; } .spark .goodline { stroke: var(--divider-color, #bbb); stroke-dasharray: 4 3; stroke-width: 1; }
    .spark text { font-size: 9px; fill: var(--secondary-text-color); }
    ul.factors, ul.plain { list-style: none; margin: 0; padding: 0; }
    ul.factors li { display: grid; grid-template-columns: 1fr 64px; gap: 4px 10px; align-items: center; padding: 4px 0; font-size: .92em; color: var(--primary-text-color); }
    .bar { height: 6px; border-radius: 3px; background: var(--divider-color, #ddd); position: relative; }
    .bar > i { position: absolute; top: 0; height: 100%; border-radius: 3px; }
    .bar > i.pos { left: 50%; background: var(--fc-good); } .bar > i.neg { right: 50%; background: var(--fc-poor); }
    .sign { font-weight: 700; margin-right: 4px; }
    ul.plain li { padding: 2px 0; font-size: .92em; color: var(--primary-text-color); }
    details summary { cursor: pointer; color: var(--secondary-text-color); font-size: .9em; }
    table { width: 100%; border-collapse: collapse; font-size: .85em; margin-top: 6px; }
    th, td { text-align: left; padding: 3px 4px; border-bottom: 1px solid var(--divider-color, #eee); color: var(--primary-text-color); }
    td.n { text-align: right; font-variant-numeric: tabular-nums; }
    .chips span { display: inline-block; margin: 2px 4px 2px 0; }
    .compact .main { grid-template-columns: 90px 1fr; } .compact .gauge .num { font-size: 46px; }
    .kiosk .main { grid-template-columns: minmax(180px, 280px) 1fr; } .kiosk .headline { font-size: 1.5em; } .kiosk .next, .kiosk .meta { font-size: 1.15em; }
    @media (max-width: 420px) { .main { grid-template-columns: 1fr; } }
  `;

  const FACTOR_ICON = { pos: "+", neg: "−" };

  function renderGauge(vm) {
    const label = vm.unavailable ? "Fishing score unavailable" : `Fishing score ${formatNumber(vm.score, vm.locale)} out of 10, ${vm.band ? vm.band.label.toLowerCase() : ""}`;
    const shown = vm.unavailable ? "–" : formatNumber(vm.score, vm.locale);
    return `<div class="gauge" role="img" aria-label="${esc(label)}">
      <svg viewBox="0 0 200 118" aria-hidden="true">
        <path class="track" d="${GAUGE.path}"/>
        <path class="fill" d="${GAUGE.path}" stroke-dasharray="${gaugeFill(vm.score).toFixed(1)} ${GAUGE.length.toFixed(1)}"/>
        <text class="num" x="100" y="92">${esc(shown)}</text>
        <text class="of" x="100" y="112">out of 10</text>
      </svg>
      <div class="bandword">${esc(vm.band ? vm.band.label : "No data")}</div>
    </div>`;
  }

  /**
   * Wind chip: an arrow pointing the way the wind is BLOWING (a bearing is where it comes from, so the arrow
   * is turned 180 degrees from it), then the speed, then the compass name and the bearing in degrees. The
   * words are for screen readers, which read "SW" badly.
   */
  function renderWind(vm) {
    const w = vm.wind;
    const hasSpeed = isNum(w.speed);
    const hasDir = isNum(w.direction);
    const speedText = hasSpeed ? `${formatNumber(w.speed, vm.locale)} ${w.unit}` : "";
    const dirText = hasDir ? `${w.cardinal} ${formatNumber(w.direction, vm.locale, 0)}°` : "";
    const spoken = `Wind${hasSpeed ? ` ${speedText}` : ""}${hasDir ? ` from the ${w.words}, ${formatNumber(w.direction, vm.locale, 0)} degrees` : ""}`;
    const arrow = hasDir
      ? `<svg class="windarrow" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" style="transform:rotate(${(Math.round(w.direction) + 180) % 360}deg)"><path d="M12 2 L19 20 L12 16 L5 20 Z" fill="currentColor"/></svg>`
      : "&#127788;&#65039;";
    return `<span class="chip" title="Wind" role="img" aria-label="${esc(spoken)}">${arrow} <strong>${esc(speedText || dirText)}</strong>${hasSpeed && hasDir ? ` ${esc(dirText)}` : ""}</span>`;
  }

  function renderSpark(vm, config) {
    const s = vm.spark;
    if (!s) return "";
    const labelAt = (i) => formatClock(parseWallClock(s.coords[i].time), vm.locale);
    const ticks = [0, Math.floor((s.coords.length - 1) / 2), s.coords.length - 1];
    const best = s.best;
    return `<section><h3>Next ${s.coords.length} hours</h3>
      <svg class="spark" viewBox="0 0 ${s.w} ${s.h + 14}" role="img" aria-label="Forecast score for the next ${s.coords.length} hours, peaking at ${esc(formatNumber(best.score, vm.locale))} around ${esc(formatClock(parseWallClock(best.time), vm.locale))}">
        ${s.spans.map((w) => `<rect class="win" x="${w.x}" y="0" width="${w.width}" height="${s.h}"/>`).join("")}
        <line class="goodline" x1="0" x2="${s.w}" y1="${s.good}" y2="${s.good}"/>
        <path class="area" d="${s.area}"/><path class="line" d="${s.line}"/>
        <circle cx="${s.coords[0].x}" cy="${s.coords[0].y}" r="3.5" fill="var(--band)"/>
        <circle cx="${best.x}" cy="${best.y}" r="3" fill="var(--band)" stroke="var(--card-background-color, #fff)" stroke-width="1.5"/>
        ${ticks.map((i, k) => `<text x="${s.coords[i].x}" y="${s.h + 11}" text-anchor="${k === 0 ? "start" : k === 2 ? "end" : "middle"}">${esc(labelAt(i))}</text>`).join("")}
      </svg></section>`;
  }

  function renderFactors(vm) {
    if (!vm.topFactors.length) return "";
    const items = vm.topFactors
      .map((f) => {
        const pos = f.contribution >= 0;
        const width = clamp(Math.abs(f.contribution) / 0.15, 0.08, 1) * 50;
        return `<li><span><span class="sign" aria-hidden="true">${pos ? FACTOR_ICON.pos : FACTOR_ICON.neg}</span><strong>${esc(f.label)}</strong>: ${esc(f.why)}</span>
          <span class="bar" role="img" aria-label="${pos ? "helps" : "hurts"}"><i class="${pos ? "pos" : "neg"}" style="width:${width.toFixed(0)}%"></i></span></li>`;
      })
      .join("");
    return `<section><h3>What is moving the score</h3><ul class="factors">${items}</ul></section>`;
  }

  function renderAdvice(vm) {
    const tactics = vm.tactics.map((t) => `<li>${esc(t)}</li>`).join("");
    if (!tactics && !vm.window) return "";
    return `<section>${tactics ? `<h3>What to do</h3><ul class="plain">${tactics}</ul>` : ""}${vm.window ? `<p class="next" style="font-weight:400">${esc(vm.window)}</p>` : ""}</section>`;
  }

  function renderDetails(vm, open) {
    if (!vm.factors.length) return "";
    const rows = vm.factors
      .map((f) => {
        const n = (v, d) => (isNum(v) ? formatNumber(v, vm.locale, d) : "–");
        return `<tr><td>${esc(f.label)}</td><td class="n">${f.source === "unavailable" ? "–" : esc(n(f.value, 1) + (f.unit ? " " + f.unit : ""))}</td><td class="n">${f.source === "unavailable" ? "–" : esc(n(f.contribution * 10, 1))}</td><td>${esc(f.why)}</td></tr>`;
      })
      .join("");
    const conditions = vm.conditions.length ? `<div class="chips">${vm.conditions.map((c) => `<span class="badge">${esc(c.replace(/_/g, " "))}</span>`).join("")}</div>` : "";
    return `<section><details data-details${open ? " open" : ""}><summary>Why this score</summary>
      <ul class="plain" style="margin-top:6px">${vm.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>${conditions}
      <table><thead><tr><th>Factor</th><th class="n">Value</th><th class="n">Effect</th><th>Why</th></tr></thead><tbody>${rows}</tbody></table>
    </details></section>`;
  }

  function renderBanners(vm) {
    let out = "";
    if (vm.stale) {
      out += `<div class="banner error" role="status"><strong>Forecast data is out of date.</strong> ${esc(vm.staleError || "The last refresh failed; scores come from the last forecast received.")}</div>`;
    }
    if (vm.unavailable) {
      out += `<div class="banner error" role="status">The score is unavailable right now.</div>`;
    } else if (vm.degraded) {
      const list = vm.notes.map((n) => `<li>${esc(n)}</li>`).join("");
      out += `<div class="banner" role="status"><strong>Less certain than usual</strong>${vm.confidence !== null ? ` (confidence ${esc(formatNumber(vm.confidence, vm.locale, 0))}%)` : ""}.${list ? `<ul>${list}</ul>` : ""}</div>`;
    }
    return out;
  }

  function renderSelector(vm, config) {
    if (!config.show_species_selector || !vm.species || !vm.species.options.length) return "";
    const opts = vm.species.options.map((o) => `<option value="${esc(o)}"${o === vm.species.current ? " selected" : ""}>${esc(o)}</option>`).join("");
    return `<select data-action="species" aria-label="Target species">${opts}</select>`;
  }

  function render(vm, config, state) {
    if (vm.error) {
      return `<style>${STYLE}</style><ha-card><div class="banner error" role="alert">${esc(vm.error)}</div></ha-card>`;
    }
    const bandClass = `band-${vm.band ? vm.band.id : "none"}`;
    const conf = vm.confidence === null ? "" : `<div class="meter" role="img" aria-label="Confidence ${esc(formatNumber(vm.confidence, vm.locale, 0))} percent"><span style="width:${clamp(vm.confidence, 0, 100)}%"></span></div>`;
    const water = config.show_water && vm.water
      ? `<span class="chip" title="Water temperature">&#127754; <strong>${esc(formatNumber(vm.water.value, vm.locale))} ${esc(vm.water.unit)}</strong> <span class="badge">${esc(vm.water.sourceLabel)}</span></span>`
      : "";
    const pressure = vm.pressure ? `<span class="chip" title="Barometer">&#127744; ${esc(vm.pressure.trend || "")} ${esc(formatNumber(vm.pressure.tendency, vm.locale))} hPa/3h</span>` : "";
    const next = vm.feeding
      ? `<div class="next now">Feeding time now</div>`
      : vm.countdown ? `<div class="next ${vm.countdown.kind === "now" ? "now" : ""}">${esc(vm.countdown.text)}</div>` : "";
    return `<style>${STYLE}</style>
      <ha-card class="${esc(config.view)} ${bandClass}">
        <header><h2>${esc(vm.title)}</h2>${renderSelector(vm, config)}</header>
        ${renderBanners(vm)}
        <div class="main">
          ${renderGauge(vm)}
          <div>
            <p class="headline">${esc(vm.headline || (vm.speciesName ? `Fishing for ${vm.speciesName}` : ""))}</p>
            ${conf}
            <div class="meta">${water}${pressure}${config.show_wind && vm.wind ? renderWind(vm) : ""}</div>
            ${next}
          </div>
        </div>
        ${config.show_forecast ? renderSpark(vm, config) : ""}
        ${config.show_factors ? renderFactors(vm) : ""}
        ${config.show_advice ? renderAdvice(vm) : ""}
        ${config.show_details ? renderDetails(vm, state && state.detailsOpen) : ""}
      </ha-card>`;
  }

  // ---------------------------------------------------------------------------------------
  // the custom element
  // ---------------------------------------------------------------------------------------

  const BaseElement = typeof HTMLElement === "undefined" ? class {} : HTMLElement;

  class FishingCard extends BaseElement {
    constructor() {
      super();
      this._config = null;
      this._hass = null;
      this._signature = "";
      this._detailsOpen = false;
      this._timer = null;
      this._root = null;
    }

    setConfig(config) {
      this._config = normaliseConfig(config);
      this._signature = "";
      this._render();
    }

    set hass(hass) {
      this._hass = hass;
      this._render();
    }

    get hass() {
      return this._hass;
    }

    connectedCallback() {
      this._ensureRoot();
      // the countdown moves while nothing in Home Assistant changes
      this._timer = setInterval(() => {
        this._signature = "";
        this._render();
      }, 60000);
      this._render();
    }

    disconnectedCallback() {
      if (this._timer) clearInterval(this._timer);
      this._timer = null;
    }

    getCardSize() {
      const view = this._config && this._config.view;
      return view === "compact" ? 3 : view === "kiosk" ? 6 : 6;
    }

    getGridOptions() {
      const view = this._config && this._config.view;
      return view === "compact" ? { columns: 6, rows: 2, min_columns: 4 } : { columns: 12, rows: 5, min_columns: 6 };
    }

    static getStubConfig(hass) {
      const first = Object.keys((hass && hass.states) || {}).find((id) => /^sensor\..+_score$/.test(id) && (hass.states[id].attributes || {}).species);
      return { entity: first || "sensor.my_spot_score" };
    }

    _ensureRoot() {
      if (this._root) return this._root;
      this._root = this.shadowRoot || this.attachShadow({ mode: "open" });
      this._root.addEventListener("change", (e) => this._onChange(e));
      this._root.addEventListener("toggle", (e) => this._onToggle(e), true);
      return this._root;
    }

    _onChange(e) {
      const t = e && e.target;
      if (!t || !t.dataset || t.dataset.action !== "species" || !this._hass) return;
      const vm = this._vm;
      if (vm && vm.species && t.value) {
        this._hass.callService("select", "select_option", { entity_id: vm.species.entity, option: t.value });
      }
    }

    _onToggle(e) {
      const t = e && e.target;
      if (t && t.dataset && "details" in t.dataset) this._detailsOpen = !!t.open;
    }

    /** What identifies "nothing relevant has changed": the states of exactly the entities this card reads. */
    _signatureOf(hass, vm) {
      const ids = vm && vm.ids ? Object.values(vm.ids) : [this._config.entity];
      const states = hass.states || {};
      return ids.map((id) => (states[id] ? `${id}:${states[id].last_updated}:${states[id].state}` : id)).join("|");
    }

    _render() {
      if (!this._config || !this._hass) return;
      const root = this._ensureRoot();
      try {
        const vm = viewModel(this._hass, this._config, Date.now());
        const signature = `${this._signatureOf(this._hass, vm)}#${vm.countdown ? vm.countdown.text : ""}#${this._detailsOpen}`;
        if (signature === this._signature) return;
        this._vm = vm;
        this._signature = signature;
        root.innerHTML = render(vm, this._config, { detailsOpen: this._detailsOpen });
      } catch (err) {
        // a card that throws takes the whole dashboard column with it: show the problem instead
        root.innerHTML = `<style>${STYLE}</style><ha-card><div class="banner error" role="alert">fishing-card could not render: ${esc(err && err.message)}</div></ha-card>`;
      }
    }
  }

  FishingCard.logic = {
    VERSION, BANDS, GAUGE, esc, bandFor, gaugeFill, parseWallClock, formatClock, formatInstant, formatNumber, formatDuration,
    countdown, topFactors, sparkline, findEntities, normaliseConfig, viewModel, render, toNum,
    COMPASS, compass, compassWords, renderWind,
  };

  if (typeof customElements !== "undefined" && !customElements.get(TAG)) customElements.define(TAG, FishingCard);

  if (typeof window !== "undefined") {
    window.customCards = window.customCards || [];
    if (!window.customCards.some((c) => c.type === TAG)) {
      window.customCards.push({
        type: TAG,
        name: "Fishing Forecast",
        description: "How good is the fishing right now, why, and when to go (needs the Fishing Forecast integration).",
        preview: false,
      });
    }
  }
  if (typeof console !== "undefined" && console.info) console.info(`%c FISHING-CARD %c ${VERSION}`, "color:#fff;background:#0b6b3a;font-weight:700", "color:#0b6b3a");

  // the test harness (Node) picks the class up from here; in a browser this is just a harmless export
  if (typeof globalThis !== "undefined") globalThis.__FishingCard = FishingCard;
})();
