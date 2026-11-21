// Tests for custom_components/fishing/www/fishing-card.js. Run with:  node --test "tests/card/*.test.mjs"
// No npm, no jsdom: the card is loaded into a sandbox with a minimal fake DOM, and its output (an HTML
// string) is asserted on. Real browser rendering is NOT covered here.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SOURCE = fs.readFileSync(path.join(HERE, "..", "..", "custom_components", "fishing", "www", "fishing-card.js"), "utf8");

function makeRoot() {
  const root = {
    writes: 0,
    _html: "",
    handlers: {},
    addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); },
    fire(type, event) { (this.handlers[type] || []).forEach((fn) => fn(event)); },
  };
  Object.defineProperty(root, "innerHTML", { get() { return this._html; }, set(v) { this._html = v; this.writes += 1; } });
  return root;
}

function load() {
  const defined = {};
  const timers = { set: [], cleared: [] };
  class FakeHTMLElement { attachShadow() { this.shadowRoot = makeRoot(); return this.shadowRoot; } }
  const sandbox = {
    HTMLElement: FakeHTMLElement,
    customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } },
    window: {},
    console: { info() {} },
    Intl, Date, Math, Map, Object, Array, Number, String, JSON, Error, RegExp, Infinity,
    setInterval: (fn, ms) => { timers.set.push({ fn, ms }); return timers.set.length; },
    clearInterval: (id) => { timers.cleared.push(id); },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox, { filename: "fishing-card.js" });
  return { Card: sandbox.__FishingCard, logic: sandbox.__FishingCard.logic, sandbox, defined, timers };
}

const { Card, logic, sandbox, defined } = load();

// ---- data shaped like the real integration's entities (see tests/ha) -------------------------

const FACTORS = [
  { name: "water_temp", label: "Water temperature", value: 6.4, unit: "degC", score: -0.3, weight: 0.22, contribution: -0.066, why: "Water is 6.4 degC, 9.6 degC below the Northern pike optimum.", source: "model" },
  { name: "front", label: "Weather front", value: 0.67, unit: "index", score: 0.75, weight: 0.09, contribution: 0.0675, why: "A front is likely, passing in about 3 h.", source: "open-meteo" },
  { name: "wind", label: "Wind", value: 2.3, unit: "m/s", score: 0.5, weight: 0.1, contribution: 0.05, why: "Wind is 2.3 m/s.", source: "open-meteo" },
  { name: "hydro", label: "Water clarity & level", value: null, unit: "", score: 0, weight: 0, contribution: 0, why: "no rainfall or river-level data", source: "unavailable" },
  { name: "cloud", label: "Cloud cover", value: 100, unit: "%", score: 0.05, weight: 0.05, contribution: 0.0025, why: "Cloud cover is 100%.", source: "open-meteo" },
];
const POINTS = Array.from({ length: 48 }, (_, i) => {
  const t = new Date(Date.UTC(2026, 10, 21, 13 + i, 0));
  return { time: t.toISOString().slice(0, 19), score: 5 + Math.sin(i / 5) * 1.5 };
});

function stateOf(state, attributes = {}, extra = {}) {
  return { state: String(state), attributes, last_updated: "2026-11-21T12:00:00+00:00", ...extra };
}

function makeHass(overrides = {}) {
  const states = {
    "sensor.test_mark_score": stateOf(5.8, {
      friendly_name: "Test mark Score", band: "good", headline: "Good fishing for Northern pike (5.8/10)", species: "pike", species_name: "Northern pike",
      reasons: ["Helps: A front is likely, passing in about 3 h.", "Hurts: Water is 6.4 degC, 9.6 degC below the Northern pike optimum."],
      tactics: ["the hour before and during a front is the best window of the day - move with it"], window: "Next major solunar window 19:50-23:50; Golden hour (sunset) starts 15:52",
      conditions: ["front", "pressure_falling"], notes: [], factors: FACTORS,
    }),
    "sensor.test_mark_confidence": stateOf(75, { coverage: 1.0, unit_of_measurement: "%" }),
    "sensor.test_mark_water_temperature": stateOf(6.4, { source: "model", trend: "steady", change_24h: -1.1, unit_of_measurement: "°C" }),
    "sensor.test_mark_pressure_tendency": stateOf(-1.01, { trend: "falling", unit_of_measurement: "hPa/3h" }),
    "sensor.test_mark_forecast_peak": stateOf(6.1, { forecast: POINTS, best_windows: [{ start: POINTS[20].time, end: POINTS[23].time, peak_score: 6.1, start_utc: "2026-11-22T08:00:00+00:00", end_utc: "2026-11-22T09:00:00+00:00" }] }),
    "sensor.test_mark_next_best_window": stateOf("2026-11-22T08:00:00+00:00", { end_utc: "2026-11-22T09:00:00+00:00", start: POINTS[20].time }),
    "binary_sensor.test_mark_feeding_time": stateOf("off", {}),
    "binary_sensor.test_mark_stale_data": stateOf("off", { state: "fresh" }),
    "select.test_mark_target_species": stateOf("Northern pike", { options: ["Common carp", "Northern pike", "Zander (pikeperch)"] }),
  };
  Object.assign(states, overrides.states || {});
  for (const [k, v] of Object.entries(overrides.remove || {})) delete states[k];
  const calls = [];
  return { states, locale: { language: "en-GB" }, config: { time_zone: "UTC" }, entities: overrides.entities, callService: (d, s, data) => calls.push([d, s, data]), calls };
}

const NOW = Date.parse("2026-11-21T12:00:00+00:00");
// objects made inside the vm sandbox come from another realm: compare them as plain data
const plain = (x) => JSON.parse(JSON.stringify(x));
const cfg = (extra = {}) => logic.normaliseConfig({ entity: "sensor.test_mark_score", ...extra });

// ---- registration -----------------------------------------------------------------------

test("registers the element and lists itself in the card picker", () => {
  assert.equal(defined["fishing-card"], Card);
  assert.equal(sandbox.window.customCards.length, 1);
  assert.equal(sandbox.window.customCards[0].type, "fishing-card");
  assert.ok(sandbox.window.customCards[0].description);
});

test("loading the file twice does not register twice", () => {
  const again = load();
  assert.equal(again.sandbox.window.customCards.filter((c) => c.type === "fishing-card").length, 1);
});

test("the file is dependency-free: no imports, no external URLs, no eval", () => {
  assert.ok(!/^\s*import\s/m.test(SOURCE) && !/\brequire\(/.test(SOURCE));
  assert.ok(!/https?:\/\//.test(SOURCE.replace(/\/\*[\s\S]*?\*\//g, "")), "no network URLs in code");
  assert.ok(!/\beval\(|new Function\(/.test(SOURCE));
});

// ---- pure helpers -----------------------------------------------------------------------

test("esc escapes every HTML-significant character and tolerates null", () => {
  assert.equal(logic.esc(`<img src=x onerror="a('b')">&`), "&lt;img src=x onerror=&quot;a(&#39;b&#39;)&quot;&gt;&amp;");
  assert.equal(logic.esc(null), "");
  assert.equal(logic.esc(undefined), "");
  assert.equal(logic.esc(0), "0");
});

test("bands follow the integration's thresholds; a band word from the integration wins", () => {
  const word = (s) => logic.bandFor(s).id;
  assert.deepEqual(plain([0, 2.4, 2.5, 3.9, 4, 5.4, 5.5, 6.9, 7, 10].map(word)), ["terrible", "terrible", "poor", "poor", "fair", "fair", "good", "good", "excellent", "excellent"]);
  assert.equal(logic.bandFor(9, "poor").id, "poor");
  assert.equal(logic.bandFor(null), null);
  assert.equal(logic.bandFor(3, "nonsense").id, "poor");
});

test("gauge fill is proportional, clamped, and zero without a score", () => {
  assert.equal(logic.gaugeFill(0), 0);
  assert.ok(Math.abs(logic.gaugeFill(5) - logic.GAUGE.length / 2) < 1e-9);
  assert.equal(logic.gaugeFill(10), logic.gaugeFill(99));
  assert.equal(logic.gaugeFill(-4), 0);
  assert.equal(logic.gaugeFill(null), 0);
  assert.equal(logic.gaugeFill(NaN), 0);
});

test("wall-clock strings keep their digits whatever the viewer's timezone", () => {
  const d = logic.parseWallClock("2026-11-21T13:00:00");
  assert.equal(logic.formatClock(d, "en-GB"), "13:00");
  assert.equal(logic.parseWallClock("garbage"), null);
  assert.equal(logic.parseWallClock(null), null);
  assert.equal(logic.formatClock(null, "en-GB"), "");
});

test("durations read naturally", () => {
  const f = logic.formatDuration;
  assert.equal(f(20000), "under a minute");
  assert.equal(f(45 * 60000), "45 min");
  assert.equal(f(3 * 3600000 + 12 * 60000), "3 h 12 min");
  assert.equal(f(2 * 3600000), "2 h");
  assert.equal(f(72 * 3600000), "3 days");
  assert.equal(f(-5), "");
  assert.equal(f(NaN), "");
});

test("numbers are formatted for the locale with at most one decimal", () => {
  assert.equal(logic.formatNumber(5.84, "en-GB"), "5.8");
  assert.equal(logic.formatNumber(5.84, "nl-NL"), "5,8");
  assert.equal(logic.formatNumber(6, "en-GB"), "6");
  assert.equal(logic.formatNumber(null), "-");
});

test("countdown: soon, now, or none", () => {
  const attrs = { end_utc: "2026-11-22T09:00:00+00:00" };
  const soon = logic.countdown("2026-11-22T08:00:00+00:00", attrs, NOW, "en-GB", "UTC");
  assert.equal(soon.kind, "soon");
  assert.match(soon.text, /^Next best window in 20 h \(08:00–09:00\)$/);
  const now = logic.countdown("2026-11-22T08:00:00+00:00", attrs, Date.parse("2026-11-22T08:30:00+00:00"), "en-GB", "UTC");
  assert.equal(now.kind, "now");
  assert.match(now.text, /until 09:00/);
  assert.equal(logic.countdown("2026-11-22T08:00:00+00:00", attrs, Date.parse("2026-11-22T10:00:00+00:00"), "en-GB", "UTC").kind, "none");
  assert.equal(logic.countdown("unknown", {}, NOW).kind, "none");
  assert.equal(logic.countdown("unavailable", null, NOW).kind, "none");
});

test("top factors are the biggest movers, positive or negative, never unavailable ones", () => {
  const top = logic.topFactors(FACTORS, 3);
  assert.deepEqual(plain(top.map((f) => f.name)), ["front", "water_temp", "wind"]);
  assert.deepEqual(plain(logic.topFactors(null)), []);
  assert.deepEqual(plain(logic.topFactors([{ source: "unavailable", contribution: 9 }, null, { contribution: "x" }])), []);
});

test("sparkline geometry", () => {
  assert.equal(logic.sparkline([], []), null);
  assert.equal(logic.sparkline([{ time: "t", score: 5 }], []), null);
  const s = logic.sparkline([{ time: "a", score: 0 }, { time: "b", score: 10 }, { time: "c", score: 5 }], [{ start: "b", end: "c" }, { start: "zzz", end: "c" }], 200, 100);
  assert.deepEqual(plain(s.coords.map((c) => [c.x, c.y])), [[0, 100], [100, 0], [200, 50]]);
  assert.equal(s.line, "M 0 100 L 100 0 L 200 50");
  assert.ok(s.area.endsWith("Z"));
  assert.deepEqual(plain(s.spans), [{ x: 100, width: 100 }]);           // the window naming an unknown time is ignored
  assert.equal(s.good, 40);
  assert.equal(s.best.time, "b");
  const clamped = logic.sparkline([{ time: "a", score: -5 }, { time: "b", score: 15 }], []);
  assert.deepEqual(plain(clamped.coords.map((c) => c.y)), [64, 0]);
});

test("a window that runs past the end of the forecast is clipped to it", () => {
  const s = logic.sparkline(POINTS, [{ start: POINTS[45].time, end: "2099-01-01T00:00:00" }], 300, 64);
  assert.equal(s.spans.length, 1);
  assert.ok(s.spans[0].x + s.spans[0].width <= 300 + 1e-6 + (300 / 47));
});

// ---- entity discovery & config ---------------------------------------------------------------

test("siblings are found through the device and the translation key", () => {
  const hass = makeHass({
    entities: {
      "sensor.test_mark_score": { device_id: "d1", translation_key: "score" },
      "sensor.renamed_by_user": { device_id: "d1", translation_key: "confidence" },
      "select.pick": { device_id: "d1", translation_key: "species" },
      "sensor.other_device": { device_id: "d2", translation_key: "confidence" },
    },
  });
  const ids = logic.findEntities(hass, "sensor.test_mark_score");
  assert.equal(ids.confidence, "sensor.renamed_by_user");
  assert.equal(ids.species, "select.pick");
});

test("without a registry the naming convention is used, and explicit overrides win", () => {
  const ids = logic.findEntities(makeHass(), "sensor.test_mark_score", { confidence: "sensor.mine" });
  assert.equal(ids.water_temperature, "sensor.test_mark_water_temperature");
  assert.equal(ids.best_window, "sensor.test_mark_next_best_window");
  assert.equal(ids.feeding_time, "binary_sensor.test_mark_feeding_time");
  assert.equal(ids.species, "select.test_mark_target_species");
  assert.equal(ids.confidence, "sensor.mine");
  assert.deepEqual(plain(Object.keys(logic.findEntities({ states: {} }, "weird_id"))), ["score"]);
});

test("config validation and view presets", () => {
  for (const bad of [null, {}, { entity: 3 }, { entity: "noDot" }]) assert.throws(() => logic.normaliseConfig(bad), /entity/);
  assert.equal(cfg().view, "default");
  assert.equal(cfg({ view: "bogus" }).view, "default");
  assert.equal(cfg({ hours: 3 }).hours, 6);
  assert.equal(cfg({ hours: 999 }).hours, 48);
  assert.equal(cfg({ hours: "24" }).hours, 24);
  const compact = cfg({ view: "compact" });
  assert.deepEqual(plain([compact.show_forecast, compact.show_factors, compact.show_advice, compact.show_details]), [false, false, false, false]);
  const kiosk = cfg({ view: "kiosk" });
  assert.deepEqual(plain([kiosk.show_species_selector, kiosk.show_details, kiosk.show_forecast]), [false, false, true]);
  assert.equal(cfg({ show_water: false }).show_water, false);
});

// ---- view model -------------------------------------------------------------------------

test("the view model reads the integration's entities", () => {
  const vm = logic.viewModel(makeHass(), cfg(), NOW);
  assert.equal(vm.title, "Test mark");
  assert.equal(vm.score, 5.8);
  assert.equal(vm.band.id, "good");
  assert.equal(vm.confidence, 75);
  assert.equal(vm.degraded, false);
  assert.equal(vm.water.value, 6.4);
  assert.equal(vm.water.sourceLabel, "model estimate");
  assert.equal(vm.pressure.tendency, -1.01);
  assert.equal(vm.spark.coords.length, 48);
  assert.ok(vm.peak > 5);
  assert.equal(vm.countdown.kind, "soon");
  assert.equal(vm.feeding, false);
  assert.equal(vm.stale, false);
  assert.deepEqual(plain(vm.species.options), ["Common carp", "Northern pike", "Zander (pikeperch)"]);
  assert.equal(logic.viewModel(makeHass(), cfg({ hours: 12 }), NOW).spark.coords.length, 12);
  assert.equal(logic.viewModel(makeHass(), cfg({ name: "My title" }), NOW).title, "My title");
});

test("a missing score entity is reported, not thrown", () => {
  const vm = logic.viewModel(makeHass({ remove: { "sensor.test_mark_score": 1 } }), cfg(), NOW);
  assert.match(vm.error, /was not found/);
});

test("unavailable and unknown states degrade gracefully", () => {
  const hass = makeHass({
    states: {
      "sensor.test_mark_score": stateOf("unavailable", { friendly_name: "Test mark Score" }),
      "sensor.test_mark_confidence": stateOf("unknown"),
      "sensor.test_mark_water_temperature": stateOf("unavailable"),
      "sensor.test_mark_next_best_window": stateOf("unknown"),
      "select.test_mark_target_species": stateOf("unavailable"),
    },
  });
  const vm = logic.viewModel(hass, cfg(), NOW);
  assert.equal(vm.unavailable, true);
  assert.equal(vm.score, null);
  assert.equal(vm.band, null);
  assert.equal(vm.confidence, null);
  assert.equal(vm.water, null);
  assert.equal(vm.species, null);
  assert.equal(vm.countdown.kind, "none");
  const html = logic.render(vm, cfg(), {});
  assert.match(html, /unavailable/);
});

test("the view model survives a hass object with nothing in it", () => {
  assert.match(logic.viewModel({}, cfg(), NOW).error, /not found/);
  assert.match(logic.viewModel(null, cfg(), NOW).error, /not found/);
  assert.match(logic.viewModel({ states: { "sensor.test_mark_score": { state: "5", attributes: null } } }, cfg(), NOW).title, /Fishing/);
});

test("low confidence marks the card degraded and carries the notes", () => {
  const hass = makeHass({ states: { "sensor.test_mark_confidence": stateOf(40) } });
  hass.states["sensor.test_mark_score"].attributes.notes = ["No discharge data: river state inferred from rainfall only"];
  const vm = logic.viewModel(hass, cfg(), NOW);
  assert.equal(vm.degraded, true);
  assert.deepEqual(plain(vm.notes), ["No discharge data: river state inferred from rainfall only"]);
});

test("feeding time and stale data come from the binary sensors", () => {
  const hass = makeHass({ states: { "binary_sensor.test_mark_feeding_time": stateOf("on"), "binary_sensor.test_mark_stale_data": stateOf("on", { error: "down" }) } });
  const vm = logic.viewModel(hass, cfg(), NOW);
  assert.equal(vm.feeding, true);
  assert.equal(vm.stale, true);
  assert.equal(vm.staleError, "down");
});

// ---- rendering --------------------------------------------------------------------------

const html = (hass = makeHass(), extra = {}) => { const c = cfg(extra); return logic.render(logic.viewModel(hass, c, NOW), c, {}); };

test("the default view shows the score, band, advice, forecast, factors, details and selector", () => {
  const out = html();
  assert.match(out, /Good fishing for Northern pike \(5\.8\/10\)/);
  assert.match(out, /aria-label="Fishing score 5\.8 out of 10, good"/);
  assert.match(out, /class="bandword">Good</);
  assert.match(out, /band-good/);
  assert.match(out, /stroke-dasharray="\d/);
  assert.match(out, /Confidence 75 percent/);
  assert.match(out, /6\.4 °C/);
  assert.match(out, /model estimate/);
  assert.match(out, /falling -1 hPa\/3h/);
  assert.match(out, /Next best window in 20 h/);
  assert.match(out, /<svg class="spark"/);
  assert.match(out, /Next 48 hours/);
  assert.match(out, /What is moving the score/);
  assert.match(out, /A front is likely/);
  assert.match(out, /What to do/);
  assert.match(out, /move with it/);
  assert.match(out, /Why this score/);
  assert.match(out, /<option value="Northern pike" selected>/);
});

test("only the top three factors are listed in the summary, but all of them are in the details table", () => {
  const out = html();
  const summary = out.split("What is moving the score")[1].split("</section>")[0];
  assert.equal((summary.match(/<li>/g) || []).length, 3);
  const table = out.split("<tbody>")[1];
  assert.equal((table.match(/<tr>/g) || []).length, FACTORS.length);
  assert.match(table, /no rainfall or river-level data/);
});

test("the band is conveyed by text and bar length, not by colour alone", () => {
  const out = html();
  assert.match(out, />Good</);                                             // the word
  assert.match(out, /role="img" aria-label="Fishing score/);                // the number, for screen readers
  assert.match(out, /aria-label="helps"/);                                  // the factor direction, for screen readers
  assert.match(out, /aria-hidden="true">\+<\/span>/);                       // and a sign beside the colour
});

test("compact view is just the headline and the gauge", () => {
  const out = html(makeHass(), { view: "compact" });
  assert.match(out, /class="compact /);
  assert.doesNotMatch(out, /class="spark"|What is moving|What to do|Why this score/);
  assert.match(out, /Good fishing for/);
});

test("kiosk view has no controls", () => {
  const out = html(makeHass(), { view: "kiosk" });
  assert.match(out, /class="kiosk /);
  assert.doesNotMatch(out, /<select|<details/);
  assert.match(out, /<svg class="spark"/);
});

test("each section can be switched off", () => {
  const out = html(makeHass(), { show_forecast: false, show_factors: false, show_advice: false, show_water: false, show_species_selector: false, show_details: false });
  assert.doesNotMatch(out, /class="spark"|What is moving|What to do|Why this score|<select|6\.4 °C/);
});

test("feeding time replaces the countdown", () => {
  const out = html(makeHass({ states: { "binary_sensor.test_mark_feeding_time": stateOf("on") } }));
  assert.match(out, /Feeding time now/);
  assert.doesNotMatch(out, /Next best window in/);
});

test("a window in progress is announced", () => {
  const hass = makeHass();
  const c = cfg();
  const out = logic.render(logic.viewModel(hass, c, Date.parse("2026-11-22T08:30:00+00:00")), c, {});
  assert.match(out, /next now">Best window now, until 09:00/);
});

test("degraded confidence shows a banner with the reasons it gives", () => {
  const hass = makeHass({ states: { "sensor.test_mark_confidence": stateOf(40) } });
  hass.states["sensor.test_mark_score"].attributes.notes = ["No wind data: wind effects are ignored.", "No cloud data: light effects are ignored."];
  const out = html(hass);
  assert.match(out, /Less certain than usual/);
  assert.match(out, /confidence 40%/);
  assert.match(out, /No wind data: wind effects are ignored\./);
  assert.doesNotMatch(html(), /Less certain than usual/);
});

test("stale data shows its own banner", () => {
  const out = html(makeHass({ states: { "binary_sensor.test_mark_stale_data": stateOf("on", { error: "HTTP 503" }) } }));
  assert.match(out, /Forecast data is out of date/);
  assert.match(out, /HTTP 503/);
});

test("a missing entity renders an error card instead of throwing", () => {
  const out = html(makeHass({ remove: { "sensor.test_mark_score": 1 } }));
  assert.match(out, /role="alert"/);
  assert.match(out, /sensor\.test_mark_score was not found/);
});

test("nothing from Home Assistant can inject markup", () => {
  const evil = `<img src=x onerror=alert(1)>`;
  const hass = makeHass();
  const a = hass.states["sensor.test_mark_score"].attributes;
  a.headline = evil; a.reasons = [evil]; a.tactics = [evil]; a.window = evil; a.notes = [evil]; a.conditions = [evil]; a.friendly_name = evil;
  a.factors = [{ name: "x", label: evil, value: evil, unit: evil, score: 1, contribution: 0.5, why: evil, source: evil }];
  hass.states["sensor.test_mark_confidence"] = stateOf(10);
  hass.states["select.test_mark_target_species"] = stateOf(evil, { options: [evil, `"><script>1</script>`] });
  hass.states["sensor.test_mark_water_temperature"] = stateOf(6.4, { source: evil, trend: evil, unit_of_measurement: evil });
  hass.states["sensor.test_mark_pressure_tendency"] = stateOf(-1, { trend: evil });
  hass.states["binary_sensor.test_mark_stale_data"] = stateOf("on", { error: evil });
  const out = html(hass);
  assert.doesNotMatch(out, /<img|<script|onerror=alert\(1\)>(?!&)/);
  assert.match(out, /&lt;img src=x onerror=alert\(1\)&gt;/);
});

test("rendering never throws on hostile or partial data", () => {
  const junk = [null, undefined, 0, "x", [], {}];
  for (const attr of junk) {
    const hass = makeHass();
    hass.states["sensor.test_mark_score"].attributes = { factors: attr, reasons: attr, tactics: attr, notes: attr, conditions: attr, band: attr, headline: attr };
    hass.states["sensor.test_mark_forecast_peak"].attributes = { forecast: attr, best_windows: attr };
    hass.states["select.test_mark_target_species"].attributes = { options: attr };
    assert.doesNotThrow(() => html(hass), `attr=${JSON.stringify(attr)}`);
  }
  const weird = makeHass();
  weird.states["sensor.test_mark_forecast_peak"].attributes.forecast = [null, { time: 1, score: "x" }, { time: "t", score: NaN }];
  assert.doesNotThrow(() => html(weird));
});

// ---- the element ------------------------------------------------------------------------

function mount(hass = makeHass(), config = { entity: "sensor.test_mark_score" }) {
  const el = new Card();
  el.setConfig(config);
  el.hass = hass;
  return el;
}

test("setConfig rejects a config without an entity", () => {
  assert.throws(() => new Card().setConfig({}), /entity/);
});

test("the element renders into its shadow root once hass and config are set", () => {
  const el = new Card();
  el.setConfig({ entity: "sensor.test_mark_score" });
  assert.equal(el.shadowRoot, undefined);            // nothing happens before there is a hass
  el.hass = makeHass();
  assert.match(el.shadowRoot.innerHTML, /Good fishing for Northern pike/);
});

test("unchanged states do not re-render (so an open dropdown is not destroyed)", () => {
  const el = mount();
  const writes = el.shadowRoot.writes;
  el.hass = makeHass();                               // identical states
  el.hass = makeHass();
  assert.equal(el.shadowRoot.writes, writes);
  const changed = makeHass({ states: { "sensor.test_mark_score": stateOf(7.2, { ...makeHass().states["sensor.test_mark_score"].attributes, band: "excellent" }) } });
  el.hass = changed;
  assert.equal(el.shadowRoot.writes, writes + 1);
  assert.match(el.shadowRoot.innerHTML, /out of 10, excellent/);
});

test("choosing a species calls the select service for the right entity", () => {
  const hass = makeHass();
  const el = mount(hass);
  el.shadowRoot.fire("change", { target: { dataset: { action: "species" }, value: "Zander (pikeperch)" } });
  assert.deepEqual(plain(hass.calls), [["select", "select_option", { entity_id: "select.test_mark_target_species", option: "Zander (pikeperch)" }]]);
  el.shadowRoot.fire("change", { target: { dataset: { action: "other" }, value: "x" } });
  el.shadowRoot.fire("change", { target: { dataset: {}, value: "x" } });
  el.shadowRoot.fire("change", {});
  assert.equal(hass.calls.length, 1);
});

test("the details panel stays open across a re-render", () => {
  const el = mount();
  assert.doesNotMatch(el.shadowRoot.innerHTML, /data-details open/);
  el.shadowRoot.fire("toggle", { target: { dataset: { details: "" }, open: true } });
  el.hass = makeHass({ states: { "sensor.test_mark_score": stateOf(6.0, makeHass().states["sensor.test_mark_score"].attributes) } });
  assert.match(el.shadowRoot.innerHTML, /data-details open/);
  el.shadowRoot.fire("toggle", { target: { dataset: { details: "" }, open: false } });
  el.hass = makeHass({ states: { "sensor.test_mark_score": stateOf(6.1, makeHass().states["sensor.test_mark_score"].attributes) } });
  assert.doesNotMatch(el.shadowRoot.innerHTML, /data-details open/);
});

test("a broken hass object produces an error card, never an exception", () => {
  const el = new Card();
  el.setConfig({ entity: "sensor.test_mark_score" });
  assert.doesNotThrow(() => { el.hass = { states: { "sensor.test_mark_score": { state: "5", attributes: { factors: { not: "an array" } }, get last_updated() { throw new Error("boom"); } } } }; });
  assert.match(el.shadowRoot.innerHTML, /could not render: boom/);
});

test("the countdown refreshes on a timer, which is cleaned up on removal", () => {
  const fresh = load();
  const el = new fresh.Card();
  el.setConfig({ entity: "sensor.test_mark_score" });
  el.hass = makeHass();
  el.connectedCallback();
  assert.equal(fresh.timers.set.length, 1);
  assert.equal(fresh.timers.set[0].ms, 60000);
  const writes = el.shadowRoot.writes;
  fresh.timers.set[0].fn();                           // a minute passes: the signature is reset, so it re-renders
  assert.ok(el.shadowRoot.writes >= writes);
  el.disconnectedCallback();
  assert.equal(fresh.timers.cleared.length, 1);
});

test("card size, grid options and the stub config", () => {
  const el = mount();
  assert.equal(el.getCardSize(), 6);
  assert.equal(el.getGridOptions().columns, 12);
  const compact = mount(makeHass(), { entity: "sensor.test_mark_score", view: "compact" });
  assert.equal(compact.getCardSize(), 3);
  assert.equal(compact.getGridOptions().rows, 2);
  assert.equal(Card.getStubConfig(makeHass()).entity, "sensor.test_mark_score");
  assert.equal(Card.getStubConfig({ states: {} }).entity, "sensor.my_spot_score");
  assert.equal(Card.getStubConfig(null).entity, "sensor.my_spot_score");
});
