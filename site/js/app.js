import { LineChart } from "./charts.js";
import { CONFIG } from "./config.js";
import { FAULTS } from "./sim.js";
import { LiveSource, RecordedSource, SimulationSource, liveStatus, loadRecorded } from "./sources.js";
import { Topology } from "./topology.js";

const $ = (id) => document.getElementById(id);
const C = { api: "#3987e5", worker: "#d95926", good: "#0ca30c", serious: "#ec835a", critical: "#d03b3b" };

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
const BANNERS = {
  simulation: ["SIMULATION", "Modeled behavior, not measurements from the lab. Rules, thresholds and limits mirror the repository; service timings are assumptions.", ""],
  recorded: ["RECORDED RUN", "Real telemetry exported from a lab run, replayed exactly as the investigator received it. The report is the real rules output.", "recorded"],
  live: ["LIVE LAB", "Real telemetry from the Compose lab on this laptop, polled every 2 s through a read-only bridge.", "live"],
};
const HINTS = {
  simulation: {
    topology: "packet speed slowed for visibility; fault delays at real scale",
    charts: "Charts mirror the Grafana dashboard panels, computed from simulated scrapes with the same windows.",
    inv: "JS port of agent/rules_baseline.py · read-only",
    alerts: "rules from monitoring/alerts.yml · evaluated every 2 s · pending → firing after <code>for: 10s</code>",
  },
  recorded: {
    topology: "node states from recorded scrapes; fault badges from ground truth",
    charts: "Charts replay the real evidence bundle (catalogue queries at a 5 s step).",
    inv: "the real report produced by agent/rules_baseline.py on this bundle",
    alerts: "firing alerts as recorded in the bundle (ALERTS samples; pending state is not recorded)",
  },
  live: {
    topology: "node states from live scrapes; fault badges from ground-truth markers",
    charts: "Charts plot live instant-query values from the lab's Prometheus.",
    inv: "JS port of agent/rules_baseline.py over live telemetry; run <code>make investigate</code> for the real report",
    alerts: "live alert states from the lab's Prometheus",
  },
};

function setMode(kind, next) {
  source = next;
  for (const k of ["simulation", "recorded", "live"]) $(`tab-${k}`).setAttribute("aria-selected", String(k === kind));
  const [title, text, cls] = BANNERS[kind];
  $("banner-title").textContent = title;
  $("banner-text").textContent = text;
  $("banner").className = `banner ${cls}`.trim();
  $("topology-hint").textContent = HINTS[kind].topology;
  $("charts-note").innerHTML = `${HINTS[kind].charts} The shaded band is the injected fault — <b>ground truth, not an alert or investigator input</b>. Hover any chart for values.`;
  $("inv-hint").innerHTML = HINTS[kind].inv;
  $("alerts-hint").innerHTML = HINTS[kind].alerts;
  $("sim-controls").hidden = kind !== "simulation";
  $("rec-controls").hidden = kind !== "recorded";
  $("live-controls").hidden = kind !== "live";
  $("presenter").hidden = kind !== "simulation";
  $("inv-controls").hidden = kind === "recorded";
  topology.clear();
  logEl.innerHTML = "";
  loggedEvents = 0;
  lastCause = null;
  nextAutoInv = kind === "simulation" ? Math.max(30, source.now()) : 0;
  $("inv-body").innerHTML = `<p class="note">${kind === "recorded" ? "The real report appears at the decision time (end of the window)." : "Waiting for 30 s of telemetry…"}</p>`;
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
  recPlaying = true;
  $("rec-play").textContent = "Pause";
  setMode("recorded", rec);
  const inc = rec.incident;
  const p = inc.ground_truth?.params ?? {};
  const params = Object.entries(p).map(([k, v]) => `${k}=${v}`).join(", ");
  $("rec-truth").className = "active-fault";
  $("rec-truth").textContent = `ground truth: ${inc.scenario} → label ${inc.label}${params ? ` (${params})` : ""}`;
  const lab = pick.file.data.lab ?? {};
  $("rec-meta").textContent = `File ${pick.file.name} · captured ${pick.file.data.captured_on ?? "?"}${lab.docker_version ? ` · Docker ${lab.docker_version}` : ""}`;
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
  $("inv-body").innerHTML = `<p class="note">The real report appears at the decision time (end of the window).</p>`;
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
  b.innerHTML = "<b></b><span></span>";
  b.querySelector("b").textContent = FAULTS[kind].label;
  b.addEventListener("click", () => inject(kind));
  $("fault-grid").appendChild(b);
  faultButtons[kind] = b;
}
function inject(kind) {
  $("fault-error").textContent = "";
  try {
    sim.lab.inject(kind, {
      durationS: Number($("p-duration").value),
      latencyMs: kind === "latency" ? Number($("p-latency").value) : undefined,
    });
  } catch (err) {
    $("fault-error").textContent = err.message;
  }
  refresh();
}
$("stop-fault").addEventListener("click", () => {
  sim.lab.stop("stopped");
  refresh();
});
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
let lastAlertIndex = 0;
function renderAlerts() {
  const now = source.now();
  const byKey = {};
  for (const a of source.alerts()) {
    // Recorded/live alerts carry only the alert name; keep the most severe state per name.
    const rank = { firing: 2, pending: 1, inactive: 0 };
    if (!byKey[a.key] || rank[a.state] > rank[byKey[a.key].state]) byKey[a.key] = a;
  }
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
  const { t0, t1, step, endLabel } = source.window();
  const xs = [];
  for (let t = Math.ceil(t0 / step - 1e-9) * step; t <= t1 + 1e-9; t += step) xs.push(t);
  const bands = source.bands();
  const minT = source === sim ? 2 : -Infinity;
  for (const [i, spec] of PANELS.entries()) {
    const series = {};
    for (const s of spec.series) series[s.key] = xs.map((t) => (t < minT ? null : source.query(s.key, t)));
    charts[i].setData({ t0, t1, xs, series, bands, endLabel });
  }
}

// ---------- investigator ----------
let lastCause = null;
let nextAutoInv = 30;
function renderReport(r) {
  const cls = r.predicted_cause === "healthy" ? "healthy" : r.predicted_cause === "unknown_or_ambiguous" ? "unknown" : "fault";
  const evidence = (r.evidence ?? [])
    .map((e) => `<tr><td><code>${escapeHtml(e.metric_id)}</code></td><td class="num">${fmtValue(e.observed_value, e.units)}</td><td>${escapeHtml(e.statistic)}</td></tr>`)
    .join("");
  const limits = r.limitations?.length ? `<dt>limitations</dt><dd>${r.limitations.map(escapeHtml).join("<br>")}</dd>` : "";
  $("inv-body").innerHTML = `
    <div class="inv-head"><span class="cause ${cls}">${escapeHtml(r.predicted_cause)}</span>
      <span class="note">${escapeHtml(r.mode ?? "rules")} · at ${stamp(source.now())} · last 30 s of a 120 s window</span></div>
    <p class="inv-summary">${escapeHtml(r.summary)}</p>
    ${evidence ? `<table class="inv-table"><thead><tr><th>cited evidence</th><th style="text-align:right">value</th><th>statistic</th></tr></thead><tbody>${evidence}</tbody></table>` : ""}
    <dl class="kv">
      <dt>impact</dt><dd>${escapeHtml(r.business_impact)}</dd>
      <dt>recommended</dt><dd>${escapeHtml(r.recommended_action)}</dd>
      ${limits}
      <dt>action_taken</dt><dd><span class="flag">${String(r.action_taken)}</span> — advisory only; no tool can change the lab</dd>
      <dt>human review</dt><dd>${r.needs_human_review ? "required" : "not required"}</dd>
    </dl>`;
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
  {
    text: "<b>1 · Architecture</b> Load → API → impairment proxy → worker on an internal network. Prometheus scrapes every 2 s. The investigator on the host reads 21 fixed queries and has no path to the fault controls (red, right).",
  },
  { text: "<b>2 · Healthy baseline</b> About 10 requests/s, p95 in milliseconds, worker CPU far below its 0.5-core quota, no alerts." },
  {
    text: "<b>3 · Inject network latency</b> 600 ms on worker→API responses for 60 s. Responses queue at the proxy; the API's slow fraction climbs while the worker stays fast.",
    action: () => {
      if (!sim.lab.fault) {
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
  if (source === sim) {
    const f = st.fault;
    for (const [kind, b] of Object.entries(faultButtons)) {
      b.disabled = Boolean(f);
      b.querySelector("span").textContent = FAULT_SUB[kind]();
    }
    $("stop-fault").disabled = !f;
    const box = $("active-fault");
    if (f) {
      const left = Math.max(0, f.end - sim.now());
      const detail = f.kind === "latency" ? ` +${f.params.latencyMs} ms` : "";
      box.className = "active-fault";
      box.textContent = `${FAULTS[f.kind].label}${detail} · ${Math.ceil(left)} s left · ${f.runId}`;
    } else {
      box.className = "active-fault none";
      box.textContent = "No fault active.";
    }
    $("clock").innerHTML = `sim <b>${mmss(sim.now())}</b> · ${paused ? "paused" : `${speed}×`}<br>load ${st.load.success} ok · ${st.load.errors} errors · ${st.load.skipped} skipped`;
  } else if (source.kind === "recorded") {
    $("clock").innerHTML = `replay <b>${stamp(source.now())}</b> · ${recPlaying ? `${recSpeed}×` : "paused"}<br>decision at T+0`;
  } else {
    $("clock").innerHTML = `live <b>${source.at ? stamp(source.at) : "—"}</b><br>${st.error ? `bridge error: ${escapeHtml(st.error)}` : "polling every 2 s"}`;
    $("live-status").className = st.error ? "active-fault" : "active-fault none";
    $("live-status").textContent = st.error
      ? `Bridge unreachable (${st.error}). Is make live-demo running?`
      : source.at
        ? `Connected · last snapshot ${stamp(source.at)}${st.fault ? ` · ${st.fault.kind} fault marker ON (ground truth)` : ""}`
        : "Connecting…";
  }
  renderAlerts();
  renderCharts();
  if (source.kind === "recorded") {
    if (source.now() >= source.end - 1e-6 && lastCause === null) runInvestigator();
  } else if ($("auto-inv").checked && source.now() >= nextAutoInv) {
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
$("tab-live").textContent = liveInfo.available ? "Live (local)" : "Live (local) · needs make live-demo";

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
  tab.textContent = result.available ? "Recorded run" : "Recorded run · none yet";
  const select = $("rec-select");
  select.innerHTML = "";
  recordedOptions().forEach((o, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = `${o.inc.scenario} → ${o.inc.reports?.rules?.predicted_cause ?? "?"} (${o.file.name})`;
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
