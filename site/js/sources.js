// Data sources behind one interface, so the same UI can show the simulation, a recorded run
// exported from the real lab, or the live local lab. Recorded and live modes only ever show
// data that came from the lab; they never fall back to invented values.

import { CONFIG } from "./config.js";
import { Prometheus } from "./prom.js";
import { catalogue, diagnose, diagnoseWith } from "./rules.js";
import { Lab } from "./sim.js";

export class SimulationSource {
  kind = "simulation";
  interactive = true;

  constructor(config = CONFIG) {
    this.config = config;
    this.lab = new Lab(config);
    this.prom = new Prometheus(this.lab, config);
    this.cat = catalogue(this.prom);
  }

  reset() {
    this.lab.reset();
    this.prom.reset();
  }

  advance(simDt) {
    let left = simDt;
    while (left > 1e-9) {
      const dt = Math.min(0.05, left);
      this.lab.tick(dt);
      this.prom.advance(this.lab.t);
      left -= dt;
    }
  }

  now() {
    return this.lab.t;
  }

  window() {
    const t1 = this.lab.t;
    return { t0: t1 - this.config.ui.chartWindowS, t1, step: this.config.prometheus.scrapeIntervalS, endLabel: "now" };
  }

  query(id, t) {
    const v = this.cat[id][0](t);
    return v === null || !Number.isFinite(v) ? null : v;
  }

  bands() {
    return this.lab.groundTruth.map((g) => ({ start: g.start, end: g.endedAt, kind: g.kind }));
  }

  alerts() {
    return [...this.prom.alerts.values()];
  }

  alertLog() {
    return this.prom.alertLog;
  }

  events() {
    return [];
  }

  diagnose() {
    return diagnose(this.prom, this.lab.t, this.config);
  }

  state() {
    const t = this.lab.t;
    return {
      fault: this.lab.fault,
      up: Object.fromEntries(["api", "worker", "impairment-proxy"].map((j) => [j, this.lab.isUp(j)])),
      workerCpu: this.prom.cpu("worker", t),
      firing: this.prom.firing().length,
      load: this.lab.loadStats,
    };
  }
}

const NO_RECORDED =
  "No recorded runs are published yet. Real runs exported from the lab will appear here; nothing is shown until they exist.";

// Recorded runs are listed in data/recorded/index.json (["<name>.json", ...]). Until that file
// exists, the Recorded tab stays disabled.
export async function loadRecorded(base = "data/recorded/") {
  try {
    const index = await fetch(`${base}index.json`, { cache: "no-store" });
    if (!index.ok) return { available: false, files: [], reason: NO_RECORDED };
    const files = [];
    for (const name of await index.json()) {
      const r = await fetch(`${base}${name}`, { cache: "no-store" });
      if (!r.ok) continue;
      const data = await r.json();
      if (data.source === "recorded" && Array.isArray(data.incidents) && data.incidents.length) files.push({ name, data });
    }
    return files.length ? { available: true, files, reason: "" } : { available: false, files: [], reason: NO_RECORDED };
  } catch {
    return { available: false, files: [], reason: NO_RECORDED };
  }
}

export function liveStatus() {
  const ok = typeof location !== "undefined" && location.origin === "http://127.0.0.1:8088";
  return {
    available: ok,
    reason: ok
      ? "Connected to the local live bridge."
      : "Live mode needs `make live-demo` on the presenting laptop (serves this page at http://127.0.0.1:8088).",
  };
}

// Nearest stored point to t within `tolerance` seconds; null when there is none.
export function lookup(points, t, tolerance) {
  if (!points.length) return null;
  let lo = 0;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid][0] < t) lo = mid + 1;
    else hi = mid;
  }
  let best = null;
  for (const p of [points[lo - 1], points[lo]]) {
    if (p && Math.abs(p[0] - t) <= tolerance && (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t))) best = p;
  }
  return best ? best[1] : null;
}

const SCENARIO_FAULT = { cpu: "cpu", latency: "latency", latency_low: "latency", timeout: "timeout" };
const seconds = (iso) => (iso ? Date.parse(iso) / 1000 : null);

// Replays one incident from a file exported from the real lab: the evidence bundle the
// investigator saw, the ground truth, and the real rules report.
export class RecordedSource {
  kind = "recorded";
  interactive = false;

  constructor(file, index, config = CONFIG) {
    this.config = config;
    this.file = file;
    this.incident = file.incidents[index];
    const b = this.incident.bundle;
    this.bundle = b;
    this.start = seconds(b.window.start);
    this.end = seconds(b.window.end);
    this.step = b.step_seconds;
    const gt = this.incident.ground_truth ?? {};
    this.gt = { start: seconds(gt.started_at), end: seconds(gt.ended_at), params: gt.params ?? {} };
    this.reset();
  }

  reset() {
    this.playhead = this.start;
  }

  advance(dt) {
    this.playhead = Math.min(this.end, this.playhead + dt);
  }

  now() {
    return this.playhead;
  }

  window() {
    return { t0: this.start, t1: this.end, step: this.step, endLabel: "decision" };
  }

  raw(id, t) {
    const s = this.bundle.series[id];
    return s ? lookup(s.points, t, this.step / 2) : null;
  }

  query(id, t) {
    if (t > this.playhead + 1e-6) return null;
    return this.raw(id, t);
  }

  bands() {
    return this.gt.start ? [{ start: this.gt.start, end: this.gt.end, kind: this.incident.scenario }] : [];
  }

  alerts() {
    const t = this.playhead;
    return this.bundle.alerts.map((a) => ({
      key: a.alertname,
      state: a.first_seen <= t && t <= a.last_seen + this.step ? "firing" : "inactive",
      firedAt: a.first_seen,
      severity: a.severity,
      recorded: true,
    }));
  }

  events() {
    const out = [];
    if (this.gt.start) out.push({ t: this.gt.start, text: `ground truth: ${this.incident.scenario} fault started`, cls: "gt" });
    if (this.gt.end && this.gt.end <= this.end) out.push({ t: this.gt.end, text: "ground truth: fault ended", cls: "gt" });
    for (const a of this.bundle.alerts) {
      out.push({ t: a.first_seen, text: `alert ${a.alertname} first seen firing`, cls: "firing" });
      if (a.last_seen + this.step < this.end) out.push({ t: a.last_seen, text: `alert ${a.alertname} last seen firing`, cls: "resolved" });
    }
    out.push({ t: this.end, text: "decision time: evidence bundle captured, investigator ran", cls: "inv" });
    return out.sort((a, b) => a.t - b.t);
  }

  diagnose() {
    if (this.playhead < this.end - 1e-6) return null;
    return { ...this.incident.reports.rules, mode: "rules (real report from the lab)" };
  }

  state() {
    const t = this.playhead;
    const active = this.gt.start !== null && t >= this.gt.start && (this.gt.end === null || t <= this.gt.end);
    const kind = SCENARIO_FAULT[this.incident.scenario];
    const p = this.gt.params;
    const up = (id) => (this.raw(id, t) ?? 1) >= 0.5;
    return {
      fault: active && kind ? { kind, params: { latencyMs: p.latency_ms ?? p.latencyMs } } : null,
      up: { api: up("api.up"), worker: up("worker.up"), "impairment-proxy": up("proxy.up") },
      workerCpu: this.raw("worker.cpu", t),
      firing: this.alerts().filter((a) => a.state === "firing").length,
      load: null,
    };
  }
}

// Polls the local read-only bridge (`make live-demo`) and keeps a short client-side history.
export class LiveSource {
  kind = "live";
  interactive = false;

  constructor(config = CONFIG, url = "/live/snapshot") {
    this.config = config;
    this.url = url;
    this.history = {};
    this.latest = {};
    this.alertList = [];
    this.markers = {};
    this.openBands = {};
    this.closedBands = [];
    this.log = [];
    this.at = null;
    this.error = null;
    this.inFlight = false;
    this.nextPoll = 0;
  }

  reset() {}

  advance() {
    const now = Date.now();
    if (this.inFlight || now < this.nextPoll) return;
    this.inFlight = true;
    this.nextPoll = now + 2000;
    fetch(this.url, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data) => this.ingest(data))
      .catch((err) => {
        this.error = String(err?.message ?? err);
      })
      .finally(() => {
        this.inFlight = false;
      });
  }

  ingest(data) {
    this.error = null;
    const at = Number(data.at);
    this.at = at;
    for (const [id, v] of Object.entries(data.metrics ?? {})) {
      const value = v === null ? null : Number(v);
      const h = (this.history[id] ??= []);
      h.push([at, value]);
      while (h.length && h[0][0] < at - 600) h.shift();
      this.latest[id] = value;
    }
    const before = Object.fromEntries(this.alertList.map((a) => [a.alertname, a.state]));
    this.alertList = data.alerts ?? [];
    for (const a of this.alertList) {
      if (before[a.alertname] !== a.state) {
        this.log.push({ t: at, text: `alert ${a.alertname} → ${a.state}`, cls: a.state === "firing" ? "firing" : "" });
      }
    }
    for (const name of Object.keys(before)) {
      if (!this.alertList.some((a) => a.alertname === name)) {
        this.log.push({ t: at, text: `alert ${name} → resolved`, cls: "resolved" });
      }
    }
    for (const [kind, on] of Object.entries(data.fault_markers ?? {})) {
      if (on && !this.openBands[kind]) {
        this.openBands[kind] = { start: at, end: null, kind };
        this.log.push({ t: at, text: `ground truth: ${kind} fault marker on`, cls: "gt" });
      } else if (!on && this.openBands[kind]) {
        this.closedBands.push({ ...this.openBands[kind], end: at });
        delete this.openBands[kind];
        this.log.push({ t: at, text: `ground truth: ${kind} fault marker off`, cls: "gt" });
      }
    }
    this.markers = data.fault_markers ?? {};
  }

  now() {
    return this.at ?? Date.now() / 1000;
  }

  window() {
    const t1 = this.now();
    return { t0: t1 - this.config.ui.chartWindowS, t1, step: 2, endLabel: "now" };
  }

  query(id, t) {
    const v = lookup(this.history[id] ?? [], t, 2.5);
    return v === null || !Number.isFinite(v) ? null : v;
  }

  bands() {
    return [...this.closedBands, ...Object.values(this.openBands)];
  }

  alerts() {
    return this.alertList.map((a) => ({ key: a.alertname, state: a.state, severity: a.severity, recorded: true }));
  }

  events() {
    return this.log;
  }

  diagnose() {
    if (this.at === null) return null;
    const raw = (id, t) => lookup(this.history[id] ?? [], t, 2.5);
    return diagnoseWith(raw, this.at, this.config, "rules (JS port over live lab telemetry)");
  }

  state() {
    const up = (id) => (this.latest[id] ?? 1) >= 0.5;
    const m = this.markers;
    return {
      fault: m.cpu ? { kind: "cpu", params: {} } : m.network ? { kind: "network", params: {} } : null,
      up: { api: up("api.up"), worker: up("worker.up"), "impairment-proxy": up("proxy.up") },
      workerCpu: this.latest["worker.cpu"] ?? null,
      firing: this.alertList.filter((a) => a.state === "firing").length,
      load: null,
      error: this.error,
    };
  }
}
