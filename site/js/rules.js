// A JavaScript port of agent/rules_baseline.py (and the summary logic in agent/telemetry.py),
// run against the simulated Prometheus. Same thresholds, same decision order, same fail-closed
// behavior. The Python code is the source of truth.

import { CONFIG } from "./config.js";

const CPU_PRESSURE_CORES = 0.3;
const SLOW_FRACTION = 0.25;
const TIMEOUT_SHARE = 0.2;
const ERROR_SHARE = 0.2;
const MIN_TRAFFIC = 0.2;
const DOWN_BELOW = 0.5;

export const ACTIONS = {
  healthy: "No action needed. Keep monitoring.",
  cpu_pressure:
    "Find what is consuming worker CPU and whether it is expected load; consider capacity only after a human review. Do not restart or rescale automatically from this report.",
  network_latency:
    "Inspect the API-to-worker network path (proxy and links) for added delay; the worker itself responds quickly. Do not change routing from this report alone.",
  network_connectivity_or_timeout:
    "Test connectivity on the API-to-worker path and confirm the worker receives requests; check the proxy and network before restarting services.",
  service_unavailable: "Check the named component's container status and logs, then restore it with human approval.",
  unknown_or_ambiguous:
    "Escalate to a human operator; gather the missing or conflicting evidence listed under limitations before acting.",
};

// The allowlisted catalogue, as functions of evaluation time. Units match agent/catalog.py.
export function catalogue(prom) {
  const m = {
    "proxy.up": [(t) => prom.up("impairment-proxy", t), "bool"],
    "api.worker_calls.success": [(t) => prom.calls("success", t), "calls/s"],
    "api.worker_calls.timeout": [(t) => prom.calls("timeout", t), "calls/s"],
    "api.worker_calls.error": [(t) => prom.calls("error", t), "calls/s"],
    "api.worker_calls.invalid_response": [(t) => prom.calls("invalid_response", t), "calls/s"],
  };
  for (const job of ["api", "worker"]) {
    Object.assign(m, {
      [`${job}.up`]: [(t) => prom.up(job, t), "bool"],
      [`${job}.request_rate`]: [(t) => prom.requestRate(job, t), "requests/s"],
      [`${job}.error_fraction`]: [(t) => prom.errorFraction(job, t), "fraction"],
      [`${job}.latency_p50`]: [(t) => prom.quantile(job, 0.5, t), "seconds"],
      [`${job}.latency_p95`]: [(t) => prom.quantile(job, 0.95, t), "seconds"],
      [`${job}.slow_fraction`]: [(t) => prom.slowFraction(job, t), "fraction"],
      [`${job}.cpu`]: [(t) => prom.cpu(job, t), "cores"],
    });
  }
  return m;
}

// query_range over the lookback, then the same status and 30 s summary rules as telemetry.py.
export function summarize(prom, end, config = CONFIG) {
  const { stepS, recentS, minRecentSamples, lookbackS } = config.investigator;
  const out = {};
  for (const [id, [fn, unit]] of Object.entries(catalogue(prom))) {
    const points = [];
    for (let t = end - lookbackS; t <= end + 1e-9; t += stepS) {
      const v = fn(t);
      if (v !== null) points.push([t, Number.isFinite(v) ? v : null]);
    }
    const numeric = points.filter(([, v]) => v !== null);
    let status;
    if (!numeric.length) status = "no_data";
    else if (numeric[numeric.length - 1][0] < end - Math.max(15, 3 * stepS)) status = "stale";
    else status = "ok";
    const recent = numeric.filter(([t]) => t > end - recentS).map(([, v]) => v);
    if (status === "ok" && recent.length < minRecentSamples) status = "no_data";
    const usable = status === "ok";
    out[id] = {
      id,
      unit,
      status,
      usable,
      recent_mean: usable ? recent.reduce((a, b) => a + b, 0) / recent.length : null,
    };
  }
  return out;
}

const pct = (v) => (v === null ? "unknown" : `${Math.round(v * 100)}%`);

export function diagnose(prom, end, config = CONFIG) {
  const s = summarize(prom, end, config);
  const evidence = [];
  const limitations = [];
  const cite = (id) => {
    if (!evidence.some((e) => e.metric_id === id)) {
      evidence.push({ metric_id: id, statistic: "recent_mean", observed_value: s[id].recent_mean, units: s[id].unit });
    }
    return s[id].recent_mean;
  };
  const mean = (id) => s[id].recent_mean;
  const traffic = mean("api.request_rate");
  const apiSlow = mean("api.slow_fraction");
  const p95 = mean("api.latency_p95");
  const apiErrors = mean("api.error_fraction");
  const impact =
    `API /work traffic ${traffic === null ? "unknown" : `${traffic.toFixed(1)} req/s`}; ` +
    `${pct(apiSlow)} of responses slower than 250 ms (p95 ${p95 === null ? "unknown" : `${p95.toFixed(2)} s`}); ` +
    `${pct(apiErrors)} returned HTTP 5xx.`;
  const finish = (cause, summary) => ({
    mode: "rules (JS port, simulated data)",
    predicted_cause: cause,
    summary,
    evidence,
    business_impact: impact,
    recommended_action: ACTIONS[cause],
    action_taken: false,
    needs_human_review: cause !== "healthy",
    limitations,
    confidence: null,
  });

  const missing = ["api.up", "worker.up", "proxy.up", "api.request_rate"].filter((id) => !s[id].usable);
  if (missing.length) {
    limitations.push(`Required evidence missing or stale: ${missing.join(", ")}.`);
    return finish("unknown_or_ambiguous", "Core health evidence is missing.");
  }
  const down = [
    ["API", "api.up"],
    ["worker", "worker.up"],
    ["proxy", "proxy.up"],
  ]
    .filter(([, id]) => cite(id) < DOWN_BELOW)
    .map(([name]) => name);
  if (down.length) return finish("service_unavailable", `Metrics scrapes are failing for: ${down.join(", ")}.`);

  if (cite("api.request_rate") < MIN_TRAFFIC) {
    limitations.push("Too little /work traffic in the last 30 s to judge latency or errors.");
    return finish("unknown_or_ambiguous", "Insufficient traffic to assess.");
  }

  const calls = Object.fromEntries(
    ["success", "timeout", "error", "invalid_response"].map((o) => [o, mean(`api.worker_calls.${o}`) || 0]),
  );
  const total = Object.values(calls).reduce((a, b) => a + b, 0);
  const timeoutShare = total > 0 ? calls.timeout / total : 0;
  const errorShare = total > 0 ? (calls.error + calls.invalid_response) / total : 0;
  const workerCpu = mean("worker.cpu");
  const cpuHigh = workerCpu !== null && workerCpu >= CPU_PRESSURE_CORES;
  if (workerCpu !== null) cite("worker.cpu");

  if (timeoutShare >= TIMEOUT_SHARE || errorShare >= ERROR_SHARE) {
    const timingOut = timeoutShare >= TIMEOUT_SHARE;
    const kind = timingOut ? "timing out" : "failing";
    cite(timingOut ? "api.worker_calls.timeout" : "api.worker_calls.error");
    cite("api.worker_calls.success");
    if (s["worker.request_rate"].usable) cite("worker.request_rate");
    if (cpuHigh) {
      limitations.push("Worker CPU pressure is also present; it could cause the failures.");
      return finish("unknown_or_ambiguous", `API calls to the worker are ${kind} while worker CPU is high.`);
    }
    return finish(
      "network_connectivity_or_timeout",
      `${pct(Math.max(timeoutShare, errorShare))} of API calls to the worker are ${kind} while every component's metrics endpoint is up.`,
    );
  }

  if (workerCpu === null) {
    limitations.push("Worker CPU is unavailable, so CPU pressure cannot be ruled in or out.");
    return finish("unknown_or_ambiguous", "Worker CPU evidence is missing.");
  }

  if (cpuHigh) {
    if (apiSlow !== null) cite("api.slow_fraction");
    const workerSlow = mean("worker.slow_fraction");
    if (apiSlow !== null && apiSlow >= SLOW_FRACTION && (workerSlow || 0) < SLOW_FRACTION) {
      limitations.push(
        "The API is slow while worker-side latency is normal. CPU queueing before the worker's handler and path delay look alike here; network delay is not excluded.",
      );
    }
    return finish("cpu_pressure", `Worker process is using ${workerCpu.toFixed(2)} cores against a 0.5-core quota.`);
  }

  if (apiSlow === null) {
    limitations.push("API latency is undefined (no histogram observations in the last 30 s).");
    return finish("unknown_or_ambiguous", "API latency evidence is missing.");
  }
  cite("api.slow_fraction");

  if (apiSlow >= SLOW_FRACTION) {
    cite("api.latency_p95");
    const workerSlow = mean("worker.slow_fraction");
    if (workerSlow === null) {
      limitations.push("Worker-side latency is unavailable, so the delay cannot be located.");
      return finish("unknown_or_ambiguous", "API responses are slow.");
    }
    cite("worker.slow_fraction");
    if (s["worker.latency_p95"].usable) cite("worker.latency_p95");
    if (workerSlow >= SLOW_FRACTION) {
      limitations.push("The worker itself is slow with normal CPU; not a modeled cause.");
      return finish("unknown_or_ambiguous", "Both API and worker responses are slow.");
    }
    return finish(
      "network_latency",
      `${pct(apiSlow)} of API responses exceed 250 ms while only ${pct(workerSlow)} of worker-side responses do, so the delay is between the API and the worker.`,
    );
  }

  if (s["api.error_fraction"].usable) cite("api.error_fraction");
  return finish("healthy", "All components are up; latency, errors, and worker CPU are within alert thresholds.");
}
