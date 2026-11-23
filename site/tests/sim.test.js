import assert from "node:assert/strict";
import { test } from "node:test";

import { CONFIG } from "../js/config.js";
import { diagnose } from "../js/rules.js";
import { Lab, mulberry32 } from "../js/sim.js";
import { Prometheus } from "../js/prom.js";

function world(seed = 7) {
  const lab = new Lab(CONFIG, mulberry32(seed));
  const prom = new Prometheus(lab, CONFIG);
  const transitions = [];
  const run = (seconds, dt = 0.1) => {
    const steps = Math.round(seconds / dt);
    for (let i = 0; i < steps; i++) {
      lab.tick(dt);
      const before = prom.alertLog.length;
      prom.advance(lab.t);
      transitions.push(...prom.alertLog.slice(before));
    }
  };
  return { lab, prom, run, transitions };
}

const first = (transitions, key, to) => transitions.find((x) => x.key === key && x.to === to)?.t ?? null;

test("healthy traffic fires nothing and diagnoses healthy", () => {
  const { lab, prom, run, transitions } = world();
  run(90);
  assert.equal(transitions.length, 0);
  assert.equal(diagnose(prom, lab.t).predicted_cause, "healthy");
  assert.ok(prom.requestRate("api", lab.t) > 9 && prom.requestRate("api", lab.t) < 11);
});

test("sustained 600 ms latency fires ApiHighLatency on the rule's own timing, then resolves", () => {
  const { lab, prom, run, transitions } = world();
  run(60);
  const start = lab.t;
  lab.inject("latency", { latencyMs: 600, durationS: 60 });
  run(40);
  const report = diagnose(prom, lab.t);
  assert.equal(report.predicted_cause, "network_latency");
  assert.equal(report.action_taken, false);
  run(80);
  const pending = first(transitions, "ApiHighLatency", "pending") - start;
  const firing = first(transitions, "ApiHighLatency", "firing") - start;
  const resolved = first(transitions, "ApiHighLatency", "resolved") - start;
  // With at most 4 requests in flight, slow requests cut throughput to ~6.7/s, so the slow
  // fraction over the 30 s window crosses 25% after ~10 s (+ scrape/eval steps); then `for: 10s`.
  assert.ok(pending >= 8 && pending <= 15, `pending at ${pending}`);
  assert.ok(firing - pending >= 10 && firing - pending <= 12, `firing at ${firing}`);
  assert.ok(resolved > 60 && resolved < 90, `resolved at ${resolved}`);
  assert.equal(first(transitions, "WorkerCpuPressure", "pending"), null);
  assert.equal(lab.groundTruth[0].reason, "duration_elapsed");
});

test("a 3 s latency burst never fires the latency alert", () => {
  const { lab, run, transitions } = world();
  run(60);
  lab.inject("latency", { latencyMs: 600, durationS: 3 });
  run(60);
  assert.equal(first(transitions, "ApiHighLatency", "firing"), null);
});

test("timeout fault: 504s, worker sees nothing, WorkerCallTimeouts fires", () => {
  const { lab, prom, run, transitions } = world();
  run(60);
  const start = lab.t;
  const workerBefore = lab.metrics.worker.hist.count;
  lab.inject("timeout", { durationS: 60 });
  run(40);
  assert.ok(lab.metrics.worker.hist.count - workerBefore <= 4); // only requests already in flight
  assert.ok((lab.metrics.api.requests[504] ?? 0) > 10);
  assert.equal(diagnose(prom, lab.t).predicted_cause, "network_connectivity_or_timeout");
  const firing = first(transitions, "WorkerCallTimeouts", "firing") - start;
  // Timeouts surface 2 s after each call and cap throughput at ~2/s, so the share crosses 20%
  // after ~17 s; then `for: 10s`.
  assert.ok(firing > 20 && firing < 33, `firing at ${firing}`);
});

test("cpu fault fires WorkerCpuPressure and diagnoses cpu_pressure", () => {
  const { lab, prom, run, transitions } = world();
  run(60);
  lab.inject("cpu", { durationS: 60 });
  run(40);
  assert.ok(first(transitions, "WorkerCpuPressure", "firing") !== null);
  assert.equal(diagnose(prom, lab.t).predicted_cause, "cpu_pressure");
});

test("down components are service_unavailable", () => {
  for (const [kind, alert] of [
    ["worker_down", 'ServiceUnavailable{job="worker"}'],
    ["proxy_down", "ImpairmentProxyUnavailable"],
  ]) {
    const { lab, prom, run, transitions } = world();
    run(60);
    lab.inject(kind, { durationS: 60 });
    run(40);
    assert.ok(first(transitions, alert, "firing") !== null, kind);
    assert.equal(diagnose(prom, lab.t).predicted_cause, "service_unavailable", kind);
  }
});

test("fault controls are bounded and one at a time", () => {
  const { lab } = world();
  assert.throws(() => lab.inject("latency", { latencyMs: 1501 }));
  assert.throws(() => lab.inject("latency", { latencyMs: 1400, jitterMs: 200 }));
  assert.throws(() => lab.inject("latency", { durationS: 121 }));
  assert.throws(() => lab.inject("packet_loss"));
  lab.inject("cpu", { durationS: 10 });
  assert.throws(() => lab.inject("timeout"), /already active/);
  lab.stop();
  assert.equal(lab.groundTruth[0].reason, "stopped");
});

test("no traffic yet means the investigator abstains", () => {
  const { lab, prom, run } = world();
  run(4);
  assert.equal(diagnose(prom, lab.t).predicted_cause, "unknown_or_ambiguous");
});
