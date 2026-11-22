import { LineChart } from "./charts.js";
import { CONFIG } from "./config.js";
import { FAULTS } from "./sim.js";
import { RECORDED_STATUS, SimulationSource, liveStatus } from "./sources.js";
import { Topology } from "./topology.js";

const $ = (id) => document.getElementById(id);
const C = { api: "#3987e5", worker: "#d95926", good: "#0ca30c", serious: "#ec835a", critical: "#d03b3b" };

const source = new SimulationSource(CONFIG);
const topology = new Topology($("topology"), CONFIG);
let speed = 1;
let paused = false;
let quiet = false; // suppresses packet animation while pre-running history

// ---------- formatting ----------
const clock = (t) => {
  const s = Math.max(0, Math.floor(t));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
};
const fmtValue = (v, unit) => {
  if (v === null || v === undefined || !Number.isFinite(v)) return "no data";
  if (unit === "fraction") return `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%`;
  if (unit === "seconds") return v < 1 ? `${(v * 1000).toFixed(1)} ms` : `${v.toFixed(2)} s`;
  if (unit === "bool" || unit === "up") return v >= 0.5 ? "up" : "down";
  if (unit === "cores") return `${v.toFixed(2)} cores`;
  return `${v.toFixed(2)} ${unit}`;
};

// ---------- tabs / honesty ----------
const live = liveStatus();
$("tab-recorded").title = RECORDED_STATUS.reason;
$("tab-recorded").textContent = "Recorded run · soon";
$("tab-live").title = live.reason;
$("tab-live").textContent = live.available ? "Live (local)" : "Live (local) · soon";

// ---------- event log ----------
const logEl = $("log");
function log(text, cls = "", t = source.now()) {
  const li = document.createElement("li");
  li.innerHTML = `<time>${clock(t)}</time><span class="${cls}"></span>`;
  li.querySelector("span").textContent = text;
  logEl.prepend(li);
  while (logEl.children.length > 200) logEl.lastChild.remove();
}

source.lab.on((type, data) => {
  if (type === "request" && !quiet) topology.addRequest(data, source.now());
  if (type === "fault_start") {
    const p = data.params;
    const detail = data.kind === "latency" ? ` +${p.latencyMs} ms` : "";
    log(`ground truth: ${FAULTS[data.kind].label}${detail} started (${p.durationS} s, ${data.runId})`, "gt");
  }
  if (type === "fault_end") log(`ground truth: ${FAULTS[data.kind].label} ended (${data.reason})`, "gt");
});

// ---------- controls ----------
const grid = $("fault-grid");
const simOnly = new Set(["worker_down", "proxy_down"]);
const FAULT_SUB = {
  latency: () => `+${$("p-latency").value} ms on worker→API responses`,
  timeout: () => "proxy drops API→worker bytes → 504 at 2 s",
  cpu: () => "bounded CPU thread in the worker",
  worker_down: () => "worker scrape and calls fail",
  proxy_down: () => "API cannot reach the proxy",
};
const faultButtons = {};
for (const kind of Object.keys(FAULTS)) {
  const b = document.createElement("button");
  b.className = `fault-btn${simOnly.has(kind) ? " sim-only" : ""}`;
  b.innerHTML = `<b></b><span></span>`;
  b.querySelector("b").textContent = FAULTS[kind].label;
  b.addEventListener("click", () => inject(kind));
  grid.appendChild(b);
  faultButtons[kind] = b;
}
function inject(kind) {
  $("fault-error").textContent = "";
  try {
    source.lab.inject(kind, {
      durationS: Number($("p-duration").value),
      latencyMs: kind === "latency" ? Number($("p-latency").value) : undefined,
    });
  } catch (err) {
    $("fault-error").textContent = err.message;
  }
  refresh();
}
$("stop-fault").addEventListener("click", () => {
  source.lab.stop("stopped");
  refresh();
});
for (const b of document.querySelectorAll("[data-speed]")) {
  b.addEventListener("click", () => {
    speed = Number(b.dataset.speed);
    for (const other of document.querySelectorAll("[data-speed]")) other.setAttribute("aria-pressed", String(other === b));
  });
}
$("pause").addEventListener("click", () => {
  paused = !paused;
  $("pause").textContent = paused ? "Resume" : "Pause";
});
$("reset").addEventListener("click", () => {
  source.reset();
  topology.clear();
  logEl.innerHTML = "";
  lastAlertIndex = 0;
  lastCause = null;
  nextAutoInv = 30;
  $("inv-body").innerHTML = `<p class="note">Waiting for the first 30 s of telemetry…</p>`;
  log("simulation reset; bounded load starts");
  refresh();
});

// ---------- alerts ----------
const ALERT_ROWS = [
  ["WorkerCpuPressure", "worker CPU > 0.30 cores (15 s rate) for 10 s"],
  ["ApiHighLatency", "> 25% of API responses slower than 250 ms (30 s), traffic > 0.2/s, for 10 s"],
  ["WorkerCallTimeouts", "> 20% of worker calls time out (30 s) for 10 s"],
  ["ApiHighErrorRate", "> 20% of API responses are 5xx (30 s) for 10 s"],
  ['ServiceUnavailable{job="api"}', "API scrape fails for 10 s"],
  ['ServiceUnavailable{job="worker"}', "worker scrape fails for 10 s"],
  ["ImpairmentProxyUnavailable", "proxy scrape fails for 10 s"],
];
const alertsEl = $("alerts");
let lastAlertIndex = 0;
function renderAlerts() {
  const now = source.now();
  const byKey = Object.fromEntries(source.alerts().map((a) => [a.key, a]));
  alertsEl.innerHTML = "";
  for (const [key, desc] of ALERT_ROWS) {
    const a = byKey[key] ?? { state: "inactive" };
    const row = document.createElement("div");
    row.className = `alert-row ${a.state}`;
    const since = a.state === "pending" ? now - a.activeAt : a.state === "firing" ? now - a.firedAt : null;
    const label = a.state === "firing" ? "▲ FIRING" : a.state === "pending" ? "● PENDING" : "inactive";
    row.innerHTML = `<span class="name"></span><span class="state ${a.state}">${label}</span><span class="meta"></span>`;
    row.querySelector(".name").textContent = key;
    const value = a.value === undefined ? "" : ` · now ${fmtValue(a.value, a.unit)}`;
    row.querySelector(".meta").textContent = `${desc}${value}${since !== null ? ` · ${Math.round(since)} s` : ""}`;
    alertsEl.appendChild(row);
  }
  const transitions = source.alertLog();
  for (; lastAlertIndex < transitions.length; lastAlertIndex++) {
    const x = transitions[lastAlertIndex];
    const cls = x.to === "firing" ? "firing" : x.to === "resolved" ? "resolved" : "";
    log(`alert ${x.key} → ${x.to}`, cls, x.t);
  }
}

// ---------- charts ----------
const PANELS = [
  { title: "Work requests / second", unit: "rate", series: [{ key: "api.request_rate", label: "API", color: C.api }, { key: "worker.request_rate", label: "worker", color: C.worker }], minMax: 12 },
  { title: "p95 work latency", unit: "seconds", series: [{ key: "api.latency_p95", label: "API", color: C.api }, { key: "worker.latency_p95", label: "worker", color: C.worker }], minMax: 0.05 },
  {
    title: "Responses slower than 250 ms",
    unit: "fraction",
    series: [{ key: "api.slow_fraction", label: "API", color: C.api }, { key: "worker.slow_fraction", label: "worker", color: C.worker }],
    thresholds: [{ value: 0.25, label: "alert 25%" }],
    fixedMax: 1,
  },
  {
    title: "API error fraction (5xx)",
    subtitle: "API",
    unit: "fraction",
    series: [{ key: "api.error_fraction", label: "API 5xx", short: "API", color: C.api }],
    thresholds: [{ value: 0.2, label: "alert 20%" }],
    fixedMax: 1,
  },
  {
    title: "Worker process CPU",
    subtitle: "worker",
    unit: "cores",
    series: [{ key: "worker.cpu", label: "worker CPU", short: "worker", color: C.worker }],
    thresholds: [
      { value: 0.3, label: "alert 0.30" },
      { value: 0.5, label: "quota 0.5" },
    ],
    fixedMax: 0.6,
  },
  {
    title: "API → worker call outcomes / s",
    unit: "rate",
    series: [
      { key: "api.worker_calls.success", label: "success", color: C.good },
      { key: "api.worker_calls.timeout", label: "timeout", color: C.serious },
      { key: "api.worker_calls.error", label: "error", color: C.critical },
    ],
    minMax: 12,
  },
];
const charts = PANELS.map((spec) => new LineChart($("charts"), spec));
function renderCharts() {
  const t1 = source.now();
  const span = CONFIG.ui.chartWindowS;
  const t0 = t1 - span;
  const step = CONFIG.prometheus.scrapeIntervalS;
  const xs = [];
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) xs.push(t);
  const bands = source.bands();
  for (const [i, spec] of PANELS.entries()) {
    const series = {};
    for (const s of spec.series) series[s.key] = xs.map((t) => (t < 2 ? null : source.query(s.key, t)));
    charts[i].setData({ t0, t1, xs, series, bands });
  }
}

// ---------- investigator ----------
let lastCause = null;
let nextAutoInv = 30;
function runInvestigator() {
  const r = source.diagnose();
  const cls = r.predicted_cause === "healthy" ? "healthy" : r.predicted_cause === "unknown_or_ambiguous" ? "unknown" : "fault";
  const evidence = r.evidence
    .map((e) => `<tr><td><code>${e.metric_id}</code></td><td class="num">${fmtValue(e.observed_value, e.units)}</td><td>${e.statistic}</td></tr>`)
    .join("");
  const limits = r.limitations.length ? `<dt>limitations</dt><dd>${r.limitations.map(escapeHtml).join("<br>")}</dd>` : "";
  $("inv-body").innerHTML = `
    <div class="inv-head"><span class="cause ${cls}">${r.predicted_cause}</span>
      <span class="note">at sim ${clock(source.now())} · last 30 s of a 120 s window</span></div>
    <p class="inv-summary">${escapeHtml(r.summary)}</p>
    ${evidence ? `<table class="inv-table"><thead><tr><th>cited evidence</th><th style="text-align:right">value</th><th>statistic</th></tr></thead><tbody>${evidence}</tbody></table>` : ""}
    <dl class="kv">
      <dt>impact</dt><dd>${escapeHtml(r.business_impact)}</dd>
      <dt>recommended</dt><dd>${escapeHtml(r.recommended_action)}</dd>
      ${limits}
      <dt>action_taken</dt><dd><span class="flag">false</span> — advisory only; no tool can change the lab</dd>
      <dt>human review</dt><dd>${r.needs_human_review ? "required" : "not required"}</dd>
    </dl>`;
  if (r.predicted_cause !== lastCause) {
    log(`investigator: ${r.predicted_cause}`, "inv");
    lastCause = r.predicted_cause;
  }
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}
$("run-inv").addEventListener("click", runInvestigator);

// ---------- presenter ----------
const STEPS = [
  {
    text: "<b>1 · Architecture</b> Load → API → impairment proxy → worker on an internal network. Prometheus scrapes every 2 s. The investigator on the host reads 21 fixed queries and has no path to the fault controls (red, right).",
  },
  { text: "<b>2 · Healthy baseline</b> About 10 requests/s, p95 in milliseconds, worker CPU far below its 0.5-core quota, no alerts." },
  {
    text: "<b>3 · Inject network latency</b> 600 ms on worker→API responses for 60 s. Responses queue at the proxy; the API's slow fraction climbs while the worker stays fast.",
    action: () => {
      if (!source.lab.fault) {
        $("p-latency").value = 600;
        $("p-duration").value = 60;
        inject("latency");
      }
    },
  },
  {
    text: "<b>4 · Alert</b> <code>ApiHighLatency</code> goes pending once over 25% of responses in its 30 s window exceed 250 ms, and fires 10 s later. Request rate dips too: at most 4 requests are in flight, so slow responses throttle the load.",
  },
  {
    text: "<b>5 · Investigate</b> The rules compare API-side and worker-side latency: slow API with a fast worker places the delay between them, so <code>network_latency</code>. Values are cited; <code>action_taken</code> is always false.",
    action: runInvestigator,
  },
  { text: "<b>6 · Recovery</b> The fault expires on its own, slow responses drain out of the 30 s window, and the alert resolves. The investigator repaired nothing." },
  { text: "<b>7 · Try the others</b> Timeout (proxy drops request bytes → 504 after 2 s), CPU pressure, or a component going down. Each leads to a different diagnosis." },
];
let step = 0;
function showStep(i) {
  step = Math.max(0, Math.min(STEPS.length - 1, i));
  $("step-text").innerHTML = STEPS[step].text;
  $("step-prev").disabled = step === 0;
  $("step-next").disabled = step === STEPS.length - 1;
}
$("step-next").addEventListener("click", () => {
  showStep(step + 1);
  STEPS[step].action?.();
});
$("step-prev").addEventListener("click", () => showStep(step - 1));
showStep(0);

// ---------- refresh + loop ----------
function refresh() {
  const st = source.state();
  topology.update(st);
  const f = st.fault;
  for (const [kind, b] of Object.entries(faultButtons)) {
    b.disabled = Boolean(f);
    b.querySelector("span").textContent = FAULT_SUB[kind]();
  }
  $("stop-fault").disabled = !f;
  const box = $("active-fault");
  if (f) {
    const left = Math.max(0, f.end - source.now());
    const detail = f.kind === "latency" ? ` +${f.params.latencyMs} ms` : "";
    box.className = "active-fault";
    box.textContent = `${FAULTS[f.kind].label}${detail} · ${Math.ceil(left)} s left · ${f.runId}`;
  } else {
    box.className = "active-fault none";
    box.textContent = "No fault active.";
  }
  $("clock").innerHTML = `sim <b>${clock(source.now())}</b> · ${paused ? "paused" : `${speed}×`}<br>load ${st.load.success} ok · ${st.load.errors} errors · ${st.load.skipped} skipped`;
  renderAlerts();
  renderCharts();
  if ($("auto-inv").checked && source.now() >= nextAutoInv) {
    nextAutoInv = source.now() + CONFIG.ui.investigatorAutoRunS;
    runInvestigator();
  }
}

let last = performance.now();
let lastUi = 0;
function frame(now) {
  const realDt = Math.min(0.25, (now - last) / 1000);
  last = now;
  if (!paused) source.advance(realDt * speed);
  topology.render(source.now());
  if (now - lastUi > CONFIG.ui.chartRefreshMs) {
    lastUi = now;
    refresh();
  }
  requestAnimationFrame(frame);
}
// Optional deep links: ?warmup=60 (seconds of baseline history, default 60),
// ?fault=latency|timeout|cpu|worker_down|proxy_down&after=35 (inject, then run on).
const params = new URLSearchParams(location.search);
const warmup = Math.min(600, Math.max(0, Number(params.get("warmup") ?? 60) || 0));
const warmFault = params.get("fault");
quiet = true;
source.advance(warmup);
log(`simulation started with ${warmup} s of baseline history; bounded load at 10 req/s`);
if (warmFault && FAULTS[warmFault]) {
  inject(warmFault);
  source.advance(Math.min(120, Math.max(0, Number(params.get("after") ?? 35) || 0)));
}
quiet = false;
if (source.now() >= 30) runInvestigator();
nextAutoInv = source.now() + CONFIG.ui.investigatorAutoRunS;
refresh();
requestAnimationFrame(frame);
