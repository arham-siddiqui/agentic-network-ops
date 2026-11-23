import { LineChart } from "./charts.js";
import { CONFIG } from "./config.js";
import { FAULTS } from "./sim.js";
import { LiveSource, RecordedSource, SimulationSource, liveStatus, loadRecorded } from "./sources.js";
import { Topology } from "./topology.js";

const $ = (id) => document.getElementById(id);
const C = { api: "#4c95ea", worker: "#e0703c", good: "#22b14c", serious: "#f0a14a", critical: "#e2504f" };

const sim = new SimulationSource(CONFIG);
let source = sim;
const topology = new Topology($("topology"), CONFIG);
let speed = 1;
let recSpeed = 4;
let paused = false;
let recPlaying = false;
let quiet = false; // suppresses packet animation while pre-running history
let recorded = { available: false, files: [], reason: "Loading…" };
let live = null;

// ---------- formatting ----------
const mmss = (s) => {
  const v = Math.max(0, Math.floor(s));
  return `${String(Math.floor(v / 60)).padStart(2, "0")}:${String(v % 60).padStart(2, "0")}`;
};
function stamp(t) {
  if (source.kind === "simulation") return mmss(t);
  if (source.kind === "recorded") {
    const d = t - source.end;
    return d >= -0.5 ? "T+0" : `T−${Math.round(-d)}s`;
  }
  return new Date(t * 1000).toLocaleTimeString([], { hour12: false });
}
const fmtValue = (v, unit) => {
  if (v === null || v === undefined || !Number.isFinite(v)) return "no data";
  if (unit === "fraction") return `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%`;
  if (unit === "seconds") return v < 1 ? `${(v * 1000).toFixed(1)} ms` : `${v.toFixed(2)} s`;
  if (unit === "bool" || unit === "up") return v >= 0.5 ? "up" : "down";
  if (unit === "cores") return `${v.toFixed(2)} cores`;
  return `${v.toFixed(2)} ${unit}`;
};
const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

// ---------- event log ----------
const logEl = $("log");
let loggedEvents = 0;
function log(text, cls = "", t = source.now()) {
  const li = document.createElement("li");
  li.innerHTML = `<time>${stamp(t)}</time><span class="${cls}"></span>`;
  li.querySelector("span").textContent = text;
  logEl.prepend(li);
  while (logEl.children.length > 200) logEl.lastChild.remove();
}

sim.lab.on((type, data) => {
  if (source !== sim) return;
  if (type === "request" && !quiet) topology.addRequest(data, sim.now());
  if (type === "fault_start") {
    const p = data.params;
    const detail = data.kind === "latency" ? ` +${p.latencyMs} ms` : "";
    log(`ground truth: ${FAULTS[data.kind].label}${detail} started (${p.durationS} s, ${data.runId})`, "gt");
  }
  if (type === "fault_end") log(`ground truth: ${FAULTS[data.kind].label} ended (${data.reason})`, "gt");
});

// ---------- modes ----------
const PILLS = {
  simulation: ["Simulation · modeled, not measured", "pill sim"],
  recorded: ["Recorded · real lab data", "pill real"],
  live: ["Live · real lab data", "pill real"],
};
const HOW = {
  simulation:
    "A browser model of the lab: bounded load (10 req/s, at most 4 in flight) flows through the API, the Toxiproxy impairment proxy and the worker. Prometheus-style scrapes every 2 s feed the same alert rules as monitoring/alerts.yml, and a JavaScript port of agent/rules_baseline.py diagnoses the last 30 s of a 120 s window. Thresholds, limits and timeouts follow the repository; service timings are assumptions, and packet speed is slowed for visibility.",
  recorded:
    "Real telemetry exported from an evaluation run on the lab, replayed exactly as the investigator received it (120 s window at a 5 s step). The diagnosis is the real report from agent/rules_baseline.py, shown at the decision time. Packet animation is off in this mode.",
  live:
    "Real telemetry from the Compose lab on this laptop, polled every 2 s through a read-only bridge (make live-demo). Faults are injected from a terminal; this page cannot change the lab.",
};

function setMode(kind, next) {
  source = next;
  for (const k of ["simulation", "recorded", "live"]) $(`tab-${k}`).setAttribute("aria-selected", String(k === kind));
  const [pill, cls] = PILLS[kind];
  $("pill").textContent = pill;
  $("pill").className = cls;
  $("how-text").textContent = HOW[kind];
  $("live-cmds").hidden = kind !== "live";
  $("sim-controls").hidden = kind !== "simulation";
  $("rec-controls").hidden = kind !== "recorded";
  $("live-controls").hidden = kind !== "live";
  $("presenter").hidden = kind !== "simulation";
  $("speed").hidden = kind !== "simulation";
  $("pause").hidden = kind !== "simulation";
  $("run-inv").hidden = kind === "recorded";
  topology.clear();
  logEl.innerHTML = "";
  loggedEvents = 0;
  lastCause = null;
  nextAutoInv = kind === "simulation" ? Math.max(30, source.now()) : 0;
  clearReport(kind === "recorded" ? "The real report appears at the decision time." : "The investigator needs 30 s of telemetry.");
  if (kind === "simulation" && source.now() >= 30) runInvestigator();
  refresh();
}

$("tab-simulation").addEventListener("click", () => setMode("simulation", sim));
$("tab-recorded").addEventListener("click", () => {
  if (!recorded.available) return;
  openRecorded(Number($("rec-select").value || 0));
});
$("tab-live").addEventListener("click", () => {
  if (!liveStatus().available) return;
  live ??= new LiveSource(CONFIG);
  setMode("live", live);
});

// ---------- recorded ----------
function openRecorded(flatIndex) {
  const options = recordedOptions();
  const pick = options[flatIndex] ?? options[0];
  const rec = new RecordedSource(pick.file.data, pick.index, CONFIG);
  $("rec-select").value = String(options.indexOf(pick)); // keep the selector in step with deep links
  recPlaying = true;
  $("rec-play").textContent = "Pause";
  setMode("recorded", rec);
  const inc = rec.incident;
  const p = inc.ground_truth?.params ?? {};
  const params = Object.entries(p).map(([k, v]) => `${k}=${v}`).join(", ");
  $("rec-truth").textContent = `ground truth: ${inc.scenario}${params ? ` (${params})` : ""}`;
  const lab = pick.file.data.lab ?? {};
  const docker = lab.docker_version ?? lab.environment?.docker_version;
  $("rec-truth").title = `${pick.file.name} · captured ${pick.file.data.captured_on ?? "?"}${docker ? ` · Docker ${docker}` : ""}`;
}
function recordedOptions() {
  const out = [];
  for (const file of recorded.files) file.data.incidents.forEach((inc, index) => out.push({ file, index, inc }));
  return out;
}
$("rec-select").addEventListener("change", (e) => openRecorded(Number(e.target.value)));
$("rec-play").addEventListener("click", () => {
  if (source.kind !== "recorded") return;
  if (source.now() >= source.end) source.reset();
  recPlaying = !recPlaying;
  $("rec-play").textContent = recPlaying ? "Pause" : "Play";
});
$("rec-restart").addEventListener("click", () => {
  if (source.kind !== "recorded") return;
  source.reset();
  logEl.innerHTML = "";
  loggedEvents = 0;
  recPlaying = true;
  $("rec-play").textContent = "Pause";
  lastCause = null;
  clearReport("The real report appears at the decision time.");
});
$("rec-end").addEventListener("click", () => {
  if (source.kind === "recorded") source.advance(source.end - source.now());
});
for (const b of document.querySelectorAll("[data-rec-speed]")) {
  b.addEventListener("click", () => {
    recSpeed = Number(b.dataset.recSpeed);
    for (const o of document.querySelectorAll("[data-rec-speed]")) o.setAttribute("aria-pressed", String(o === b));
  });
}

// ---------- simulation controls ----------
const faultButtons = [...document.querySelectorAll("[data-fault]")];
for (const b of faultButtons) {
  b.addEventListener("click", () => {
    if (b.dataset.fault === "healthy") {
      if (sim.lab.fault) sim.lab.stop("stopped");
      refresh();
    } else {
      inject(b.dataset.fault);
    }
  });
}
const latencyInput = $("p-latency");
const showLatency = () => ($("latency-out").textContent = `${latencyInput.value} ms`);
latencyInput.addEventListener("input", showLatency);
showLatency();
$("proxy-down").addEventListener("click", () => inject("proxy_down"));
function inject(kind) {
  $("fault-error").textContent = "";
  try {
    if (sim.lab.fault) sim.lab.stop("stopped");
    sim.lab.inject(kind, {
      durationS: Number($("p-duration").value),
      latencyMs: kind === "latency" ? Number(latencyInput.value) : undefined,
    });
  } catch (err) {
    $("fault-error").textContent = err.message;
    $("fault-error").closest("details").open = true;
  }
  refresh();
}
for (const b of document.querySelectorAll("[data-speed]")) {
  b.addEventListener("click", () => {
    speed = Number(b.dataset.speed);
    for (const o of document.querySelectorAll("[data-speed]")) o.setAttribute("aria-pressed", String(o === b));
  });
}
$("pause").addEventListener("click", () => {
  paused = !paused;
  $("pause").textContent = paused ? "Resume" : "Pause";
});
$("reset").addEventListener("click", () => {
  sim.reset();
  topology.clear();
  logEl.innerHTML = "";
  lastAlertIndex = 0;
  lastCause = null;
  nextAutoInv = 30;
  clearReport("The investigator needs 30 s of telemetry.");
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
let lastAlertIndex = 0;
function renderAlerts() {
  const now = source.now();
  const byKey = {};
  for (const a of source.alerts()) {
    // Recorded/live alerts carry only the alert name; keep the most severe state per name.
    const rank = { firing: 2, pending: 1, inactive: 0 };
    if (!byKey[a.key] || rank[a.state] > rank[byKey[a.key].state]) byKey[a.key] = a;
  }
  const active = Object.values(byKey).filter((a) => a.state === "firing" || a.state === "pending");
  const order = { firing: 0, pending: 1 };
  active.sort((a, b) => order[a.state] - order[b.state]);
  $("tile-alerts").className = `tile ${active.some((a) => a.state === "firing") ? "bad" : active.length ? "warn" : "good"}`;
  $("alerts-body").innerHTML = active.length
    ? active
        .map((a) => `<div class="alert-line ${a.state}"><span class="dot"></span><span>${escapeHtml(a.key)}</span><small>${a.state}</small></div>`)
        .join("")
    : `<div class="calm">All clear</div><div class="tile-sub">no alerts firing</div>`;
  $("alerts").innerHTML = "";
  for (const [key, desc] of ALERT_ROWS) {
    const generic = key.split("{")[0];
    const a = byKey[key] ?? byKey[generic] ?? { state: "inactive" };
    const row = document.createElement("div");
    row.className = `alert-row ${a.state}`;
    const since = a.state === "pending" && a.activeAt !== undefined ? now - a.activeAt : a.state === "firing" && a.firedAt !== undefined ? now - a.firedAt : null;
    const label = a.state === "firing" ? "▲ FIRING" : a.state === "pending" ? "● PENDING" : "inactive";
    row.innerHTML = `<span class="name"></span><span class="state ${a.state}">${label}</span><span class="meta"></span>`;
    row.querySelector(".name").textContent = key;
    const value = a.value === undefined ? "" : ` · now ${fmtValue(a.value, a.unit)}`;
    const jobNote = !byKey[key] && byKey[generic] ? " · job not recorded" : "";
    row.querySelector(".meta").textContent = `${desc}${value}${since !== null ? ` · ${Math.round(since)} s` : ""}${jobNote}`;
    $("alerts").appendChild(row);
  }
  if (source === sim) {
    const transitions = sim.alertLog();
    for (; lastAlertIndex < transitions.length; lastAlertIndex++) {
      const x = transitions[lastAlertIndex];
      log(`alert ${x.key} → ${x.to}`, x.to === "firing" ? "firing" : x.to === "resolved" ? "resolved" : "", x.t);
    }
  } else {
    const events = source.events();
    for (; loggedEvents < events.length && events[loggedEvents].t <= now + 1e-6; loggedEvents++) {
      const e = events[loggedEvents];
      log(e.text, e.cls, e.t);
    }
  }
}

// ---------- charts ----------
const PANELS = [
  {
    title: "Response time (p95)",
    unit: "seconds",
    series: [{ key: "api.latency_p95", label: "API", color: C.api }, { key: "worker.latency_p95", label: "worker", color: C.worker }],
    minMax: 0.05,
  },
  {
    title: "Calls from API to worker / s",
    unit: "rate",
    series: [
      { key: "api.worker_calls.success", label: "success", color: C.good },
      { key: "api.worker_calls.timeout", label: "timeout", color: C.serious },
      { key: "api.worker_calls.error", label: "error", color: C.critical },
    ],
    minMax: 12,
  },
  {
    title: "Worker CPU (cores)",
    unit: "cores",
    series: [{ key: "worker.cpu", label: "worker CPU", short: "worker", color: C.worker }],
    thresholds: [
      { value: 0.3, label: "alert" },
      { value: 0.5, label: "quota" },
    ],
    fixedMax: 0.6,
  },
];
const MORE_PANELS = [
  {
    title: "Responses slower than 250 ms",
    unit: "fraction",
    series: [{ key: "api.slow_fraction", label: "API", color: C.api }, { key: "worker.slow_fraction", label: "worker", color: C.worker }],
    thresholds: [{ value: 0.25, label: "alert" }],
    fixedMax: 1,
  },
  {
    title: "API error fraction (5xx)",
    unit: "fraction",
    series: [{ key: "api.error_fraction", label: "API 5xx", short: "API", color: C.api }],
    thresholds: [{ value: 0.2, label: "alert" }],
    fixedMax: 1,
  },
  {
    title: "Requests per second",
    unit: "rate",
    series: [{ key: "api.request_rate", label: "API", color: C.api }, { key: "worker.request_rate", label: "worker", color: C.worker }],
    minMax: 12,
  },
];
const charts = [
  ...PANELS.map((spec) => [spec, new LineChart($("charts"), spec)]),
  ...MORE_PANELS.map((spec) => [spec, new LineChart($("charts-more"), spec)]),
];
function renderCharts() {
  const { t0, t1, step, endLabel } = source.window();
  const xs = [];
  for (let t = Math.ceil(t0 / step - 1e-9) * step; t <= t1 + 1e-9; t += step) xs.push(t);
  const bands = source.bands();
  const minT = source === sim ? 2 : -Infinity;
  for (const [spec, chart] of charts) {
    const series = {};
    for (const s of spec.series) series[s.key] = xs.map((t) => (t < minT ? null : source.query(s.key, t)));
    chart.setData({ t0, t1, xs, series, bands, endLabel });
  }
}

// ---------- user impact tile ----------
function renderImpact() {
  const t = source.kind === "live" ? source.at : source.now();
  const ok = (v) => v !== null && v !== undefined && Number.isFinite(v);
  const q = (key) => (ok(t) ? source.query(key, t) : null);
  const p95 = q("api.latency_p95");
  const err = q("api.error_fraction");
  const rate = q("api.request_rate");
  $("impact-big").textContent = !ok(p95) ? "—" : p95 < 1 ? `${Math.round(p95 * 1000)} ms` : `${p95.toFixed(2)} s`;
  $("impact-sub").textContent = `API p95 latency · ${ok(err) ? `${(err * 100).toFixed(err > 0 && err < 0.1 ? 1 : 0)}% errors` : "errors —"} · ${ok(rate) ? `${rate.toFixed(1)} req/s` : "— req/s"}`;
  const bad = (ok(err) && err >= 0.2) || (ok(p95) && p95 >= 1);
  const warn = (ok(err) && err >= 0.05) || (ok(p95) && p95 >= 0.25);
  $("tile-impact").className = `tile ${bad ? "bad" : warn ? "warn" : ok(p95) ? "good" : ""}`;
}

// ---------- investigator ----------
let lastCause = null;
let nextAutoInv = 30;
const CAUSE_LABEL = {
  healthy: "Healthy",
  cpu_pressure: "CPU pressure",
  network_latency: "Network latency",
  network_connectivity_or_timeout: "Network timeout",
  service_unavailable: "Service down",
  unknown_or_ambiguous: "Unclear",
};
function clearReport(message) {
  $("cause").textContent = "Waiting…";
  $("cause").className = "big cause";
  $("diag-summary").textContent = message;
  $("tile-diagnosis").className = "tile";
  $("inv-body").innerHTML = `<p class="muted">${escapeHtml(message)}</p>`;
}
function renderReport(r) {
  const cls = r.predicted_cause === "healthy" ? "good" : r.predicted_cause === "unknown_or_ambiguous" ? "" : "warn";
  $("cause").textContent = CAUSE_LABEL[r.predicted_cause] ?? r.predicted_cause;
  $("cause").className = `big cause ${cls}`;
  $("diag-summary").textContent = r.summary;
  $("tile-diagnosis").className = `tile ${cls}`;
  const evidence = (r.evidence ?? [])
    .map((e) => `<tr><td><code>${escapeHtml(e.metric_id)}</code></td><td class="num">${fmtValue(e.observed_value, e.units)}</td><td>${escapeHtml(e.statistic)}</td></tr>`)
    .join("");
  const limits = r.limitations?.length ? `<dt>Limitations</dt><dd>${r.limitations.map(escapeHtml).join("<br>")}</dd>` : "";
  $("inv-body").innerHTML = `
    <p class="muted">${escapeHtml(r.mode ?? "rules")} · at ${stamp(source.now())} · cause <code>${escapeHtml(r.predicted_cause)}</code></p>
    <dl class="kv">
      <dt>Impact</dt><dd>${escapeHtml(r.business_impact)}</dd>
      <dt>Recommended</dt><dd>${escapeHtml(r.recommended_action)}</dd>
      ${limits}
      <dt>Action taken</dt><dd><span class="flag">${String(r.action_taken)}</span> · advisory only</dd>
    </dl>
    ${evidence ? `<table class="inv-table"><thead><tr><th>Cited evidence</th><th class="num">Value</th><th>Statistic</th></tr></thead><tbody>${evidence}</tbody></table>` : ""}`;
  if (r.predicted_cause !== lastCause) {
    log(`investigator: ${r.predicted_cause}`, "inv");
    lastCause = r.predicted_cause;
  }
}
function runInvestigator() {
  const r = source.diagnose();
  if (r) renderReport(r);
}
$("run-inv").addEventListener("click", runInvestigator);

// ---------- presenter (simulation) ----------
const STEPS = [
  { text: "<b>Architecture.</b> Traffic flows Load → API → Proxy → Worker. Prometheus watches all three; the investigator only reads Prometheus." },
  { text: "<b>Healthy baseline.</b> About 10 requests/s, millisecond responses, no alerts." },
  {
    text: "<b>Inject network latency.</b> The proxy holds each response for 600 ms. Watch the yellow dots queue at the proxy.",
    action: () => {
      if (!sim.lab.fault) {
        latencyInput.value = 600;
        showLatency();
        inject("latency");
      }
    },
  },
  { text: "<b>Alert.</b> ApiHighLatency fires once over 25% of responses take longer than 250 ms for 10 s." },
  { text: "<b>Diagnose.</b> The API is slow but the worker is fast, so the delay is between them: network latency. No action is taken.", action: runInvestigator },
  { text: "<b>Recovery.</b> The fault expires, latency drops, the alert clears. The investigator repaired nothing." },
  { text: "<b>Try another.</b> Timeout, CPU pressure, or Worker down each leads to a different diagnosis." },
];
let step = 0;
function showStep(i) {
  step = Math.max(0, Math.min(STEPS.length - 1, i));
  $("step-text").innerHTML = STEPS[step].text;
  $("step-count").textContent = `${step + 1}/${STEPS.length}`;
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
  if (source === sim) {
    const f = st.fault;
    const current = f ? f.kind : "healthy";
    for (const b of faultButtons) b.setAttribute("aria-pressed", String(b.dataset.fault === current));
    const chip = $("countdown");
    if (f) {
      const left = Math.max(0, f.end - sim.now());
      const detail = f.kind === "latency" ? ` +${f.params.latencyMs} ms` : "";
      chip.hidden = false;
      chip.textContent = `${FAULTS[f.kind].label}${detail} · ${Math.ceil(left)} s left`;
    } else {
      chip.hidden = true;
    }
    $("clock").innerHTML = `<b>${mmss(sim.now())}</b> ${paused ? "paused" : `${speed}×`}`;
  } else if (source.kind === "recorded") {
    $("clock").innerHTML = `<b>${stamp(source.now())}</b> ${recPlaying ? `${recSpeed}×` : "paused"}`;
  } else {
    $("clock").innerHTML = `<b>${source.at ? stamp(source.at) : "—"}</b>`;
    $("live-status").className = st.error ? "chip bad" : "chip";
    $("live-status").textContent = st.error
      ? "Bridge unreachable — is make live-demo running?"
      : source.at
        ? `Connected${st.fault ? ` · ${st.fault.kind} fault marker on (ground truth)` : ""}`
        : "Connecting…";
  }
  renderAlerts();
  renderCharts();
  renderImpact();
  if (source.kind === "recorded") {
    if (source.now() >= source.end - 1e-6 && lastCause === null) runInvestigator();
  } else if (source.now() >= nextAutoInv) {
    nextAutoInv = source.now() + CONFIG.ui.investigatorAutoRunS;
    runInvestigator();
  }
}

let last = performance.now();
let lastUi = 0;
function frame(now) {
  const realDt = Math.min(0.25, (now - last) / 1000);
  last = now;
  if (source === sim && !paused) sim.advance(realDt * speed);
  if (source.kind === "recorded" && recPlaying) {
    source.advance(realDt * recSpeed);
    if (source.now() >= source.end) {
      recPlaying = false;
      $("rec-play").textContent = "Replay";
    }
  }
  if (source.kind === "live") source.advance();
  if (source === sim) topology.render(sim.now());
  if (now - lastUi > CONFIG.ui.chartRefreshMs) {
    lastUi = now;
    refresh();
  }
  requestAnimationFrame(frame);
}

// ---------- start ----------
const liveInfo = liveStatus();
$("tab-live").disabled = !liveInfo.available;
$("tab-live").title = liveInfo.reason;
$("tab-live").textContent = "Live";

// Optional deep links: ?warmup=60 (seconds of baseline history, default 60),
// ?fault=latency|timeout|cpu|worker_down|proxy_down&after=35 (inject, then run on),
// ?mode=recorded|live.
const params = new URLSearchParams(location.search);
const warmup = Math.min(600, Math.max(0, Number(params.get("warmup") ?? 60) || 0));
const warmFault = params.get("fault");
quiet = true;
sim.advance(warmup);
log(`simulation started with ${warmup} s of baseline history; bounded load at 10 req/s`);
if (warmFault && FAULTS[warmFault]) {
  inject(warmFault);
  sim.advance(Math.min(120, Math.max(0, Number(params.get("after") ?? 35) || 0)));
}
quiet = false;
if (sim.now() >= 30) runInvestigator();
nextAutoInv = sim.now() + CONFIG.ui.investigatorAutoRunS;
refresh();
requestAnimationFrame(frame);

loadRecorded().then((result) => {
  recorded = result;
  const tab = $("tab-recorded");
  tab.disabled = !result.available;
  tab.title = result.reason;
  tab.textContent = "Recorded";
  const select = $("rec-select");
  select.innerHTML = "";
  recordedOptions().forEach((o, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = `${o.inc.scenario} → ${CAUSE_LABEL[o.inc.reports?.rules?.predicted_cause] ?? "?"} · ${o.file.name.replace(/\.json$/, "")}`;
    select.appendChild(opt);
  });
  if (params.get("mode") === "recorded" && result.available) {
    openRecorded(Number(params.get("incident") ?? 0) || 0);
    if (params.get("at") === "end") {
      source.advance(source.end - source.now());
      recPlaying = false;
      $("rec-play").textContent = "Replay";
      refresh();
    }
  }
});
if (params.get("mode") === "live" && liveInfo.available) {
  live = new LiveSource(CONFIG);
  setMode("live", live);
}
