// Synthetic, test-only fixtures. Real recorded runs live in site/data/recorded/ and are
// exported from the lab; nothing here is shown on the site.
import assert from "node:assert/strict";
import { test } from "node:test";

import { CONFIG } from "../js/config.js";
import { LiveSource, RecordedSource, lookup } from "../js/sources.js";

const START = Date.parse("2026-11-21T12:00:00Z") / 1000;
const END = START + 120;
const steps = Array.from({ length: 25 }, (_, i) => START + i * 5);
const series = (fn, unit) => ({ unit, status: "ok", points: steps.map((t) => [t, fn(t)]) });

const FIXTURE = {
  source: "recorded",
  captured_on: "test-fixture",
  lab: {},
  incidents: [
    {
      scenario: "latency",
      label: "network_latency",
      ground_truth: { started_at: new Date((START + 80) * 1000).toISOString(), ended_at: new Date((START + 140) * 1000).toISOString(), params: { latency_ms: 600 } },
      bundle: {
        window: { start: new Date(START * 1000).toISOString(), end: new Date(END * 1000).toISOString() },
        step_seconds: 5,
        series: {
          "api.up": series(() => 1, "bool"),
          "api.slow_fraction": series((t) => (t >= START + 85 ? 0.9 : 0), "fraction"),
          "worker.slow_fraction": series((t) => (t > START + 110 ? null : 0), "fraction"),
        },
        alerts: [{ alertname: "ApiHighLatency", severity: "warning", first_seen: START + 100, last_seen: END }],
      },
      reports: { rules: { predicted_cause: "network_latency", summary: "s", evidence: [], business_impact: "i", recommended_action: "a", action_taken: false, needs_human_review: true, limitations: [] } },
    },
  ],
};

test("lookup finds the nearest point within tolerance", () => {
  const pts = [[0, 1], [5, 2], [10, null]];
  assert.equal(lookup(pts, 5, 2.5), 2);
  assert.equal(lookup(pts, 6, 2.5), 2);
  assert.equal(lookup(pts, 10, 2.5), null);
  assert.equal(lookup(pts, 30, 2.5), null);
  assert.equal(lookup([], 0, 1), null);
});

test("recorded replay masks the future and reveals the report only at decision time", () => {
  const rec = new RecordedSource(FIXTURE, 0, CONFIG);
  assert.equal(rec.query("api.slow_fraction", START + 90), null); // playhead at start
  rec.advance(95);
  assert.equal(rec.query("api.slow_fraction", START + 90), 0.9);
  assert.equal(rec.query("api.slow_fraction", START + 100), null);
  assert.equal(rec.state().fault.kind, "latency");
  assert.equal(rec.state().fault.params.latencyMs, 600);
  assert.equal(rec.alerts()[0].state, "inactive");
  assert.equal(rec.diagnose(), null);
  rec.advance(100);
  assert.equal(rec.now(), END);
  assert.equal(rec.alerts()[0].state, "firing");
  assert.equal(rec.query("worker.slow_fraction", START + 115), null); // NaN in the bundle stays a gap
  assert.equal(rec.diagnose().predicted_cause, "network_latency");
  assert.deepEqual(rec.bands(), [{ start: START + 80, end: START + 140, kind: "latency" }]);
  const events = rec.events();
  assert.deepEqual(events.map((e) => e.t), [...events.map((e) => e.t)].sort((a, b) => a - b));
  assert.equal(events.at(-1).cls, "inv");
});

test("live snapshots build history, ground-truth bands, and alert transitions", () => {
  const live = new LiveSource(CONFIG);
  const metrics = (slow) => ({ "api.up": 1, "worker.up": 1, "proxy.up": 1, "api.slow_fraction": slow, "api.request_rate": 9.8 });
  live.ingest({ at: 1000, metrics: metrics(0), alerts: [], fault_markers: { cpu: 0, network: 0 } });
  live.ingest({ at: 1002, metrics: metrics(0.8), alerts: [{ alertname: "ApiHighLatency", severity: "warning", state: "pending" }], fault_markers: { cpu: 0, network: 1 } });
  live.ingest({ at: 1004, metrics: metrics(0.9), alerts: [{ alertname: "ApiHighLatency", severity: "warning", state: "firing" }], fault_markers: { cpu: 0, network: 1 } });
  live.ingest({ at: 1006, metrics: metrics(0.1), alerts: [], fault_markers: { cpu: 0, network: 0 } });
  assert.equal(live.query("api.slow_fraction", 1004), 0.9);
  assert.deepEqual(live.bands(), [{ start: 1002, end: 1006, kind: "network" }]);
  const texts = live.events().map((e) => e.text);
  assert.ok(texts.includes("alert ApiHighLatency → firing"));
  assert.ok(texts.includes("alert ApiHighLatency → resolved"));
  assert.equal(live.state().fault, null);
  // Too little history for the 30 s summary: the port abstains rather than guessing.
  assert.equal(live.diagnose().predicted_cause, "unknown_or_ambiguous");
});
