// A model of the lab: bounded load -> API -> impairment proxy -> worker, with the same
// faults the controllers inject. It produces the counters each service would export.
// Modeled behavior, not measurements.

import { CONFIG } from "./config.js";

export const FAULTS = {
  latency: { label: "Network latency", label2: "proxy holds worker->API responses" },
  timeout: { label: "Network timeout", label2: "proxy drops API->worker request bytes" },
  cpu: { label: "Worker CPU pressure", label2: "bounded CPU thread in the worker" },
  worker_down: { label: "Worker down", label2: "simulation-only scenario" },
  proxy_down: { label: "Proxy down", label2: "simulation-only scenario" },
};

class Histogram {
  constructor(buckets) {
    this.buckets = buckets;
    this.counts = buckets.map(() => 0); // non-cumulative per bucket
    this.inf = 0;
    this.count = 0;
    this.sum = 0;
  }
  observe(seconds) {
    this.count += 1;
    this.sum += seconds;
    const i = this.buckets.findIndex((le) => seconds <= le);
    if (i === -1) this.inf += 1;
    else this.counts[i] += 1;
  }
  cumulative() {
    let running = 0;
    const out = this.buckets.map((le, i) => {
      running += this.counts[i];
      return [le, running];
    });
    out.push([Infinity, running + this.inf]);
    return out;
  }
}

function serviceMetrics(buckets) {
  return { requests: {}, hist: new Histogram(buckets), cpuSeconds: 0 };
}

// Small deterministic PRNG so tests (and replays with a seed) are reproducible.
export function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export class Lab {
  constructor(config = CONFIG, rng = Math.random) {
    this.c = config;
    this.rng = rng;
    this.listeners = new Set();
    this.reset();
  }

  on(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  emit(type, data) {
    for (const listener of this.listeners) listener(type, data);
  }

  reset() {
    this.t = 0;
    this.seq = 0;
    this.nextSend = 0;
    this.inFlight = [];
    this.workerPending = [];
    this.fault = null;
    this.groundTruth = [];
    this.loadStats = { success: 0, errors: 0, skipped: 0 };
    const b = this.c.buckets;
    this.metrics = {
      api: { ...serviceMetrics(b), workerCalls: { success: 0, timeout: 0, error: 0, invalid_response: 0 } },
      worker: serviceMetrics(b),
      proxy: { sentUpstream: 0, sentDownstream: 0 },
    };
    this.emit("reset", {});
  }

  isUp(job) {
    if (job === "worker") return this.fault?.kind !== "worker_down";
    if (job === "impairment-proxy") return this.fault?.kind !== "proxy_down";
    return true;
  }

  // Mirrors the controllers: validated, bounded, one fault at a time, automatic expiry.
  inject(kind, params = {}) {
    if (!FAULTS[kind]) throw new Error(`unknown fault ${kind}`);
    if (this.fault) throw new Error("A fault is already active; stop it or wait for expiry");
    const f = this.c.faults;
    const duration = Number(params.durationS ?? f.defaultDurationS);
    if (!Number.isFinite(duration) || duration < f.minDurationS || duration > f.maxDurationS) {
      throw new Error(`duration must be between ${f.minDurationS} and ${f.maxDurationS} seconds`);
    }
    const run = { durationS: duration };
    if (kind === "latency") {
      const ms = Number(params.latencyMs ?? f.latency.defaultMs);
      const jitter = Number(params.jitterMs ?? 0);
      if (!Number.isInteger(ms) || ms < f.latency.minMs || ms > f.latency.maxMs) {
        throw new Error(`latency_ms must be a whole number between ${f.latency.minMs} and ${f.latency.maxMs}`);
      }
      if (!Number.isInteger(jitter) || jitter < 0 || jitter > f.latency.maxJitterMs || ms + jitter > f.latency.maxMs) {
        throw new Error(`jitter_ms must be 0-${f.latency.maxJitterMs} and latency + jitter <= ${f.latency.maxMs}`);
      }
      Object.assign(run, { latencyMs: ms, jitterMs: jitter });
    }
    this.fault = {
      runId: `run-${++this.seq}`,
      kind,
      params: run,
      start: this.t,
      end: this.t + duration,
    };
    this.groundTruth.push({ ...this.fault, endedAt: null, reason: null });
    this.emit("fault_start", this.fault);
    return this.fault;
  }

  stop(reason = "stopped") {
    if (!this.fault) return null;
    const ended = this.fault;
    const record = this.groundTruth.find((g) => g.runId === ended.runId);
    record.endedAt = this.t;
    record.reason = reason;
    this.fault = null;
    this.emit("fault_end", { ...ended, endedAt: this.t, reason });
    return ended;
  }

  normal(mean, sd) {
    // Box-Muller; clipped so service times stay positive.
    const u = Math.max(1e-9, this.rng());
    const v = this.rng();
    return mean + sd * Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  }

  // Decide what one /work request experiences, given the fault active when it starts.
  plan() {
    const h = this.c.healthy;
    const kind = this.fault?.kind;
    let workerMs = Math.max(0.6, this.normal(h.workerServiceMs, h.workerJitterMs));
    if (kind === "cpu") {
      workerMs += Math.max(0, this.normal(this.c.cpuFault.extraWorkerMs, this.c.cpuFault.extraWorkerJitterMs));
    }
    const base = this.c.api.overheadMs + 4 * h.hopMs;
    if (kind === "proxy_down") {
      return { status: 502, outcome: "error", apiMs: this.c.api.overheadMs + 0.5, worker: false, stage: "proxy_refused" };
    }
    if (kind === "worker_down") {
      return { status: 502, outcome: "error", apiMs: base + 1, worker: false, stage: "upstream_refused" };
    }
    if (kind === "timeout") {
      return {
        status: 504,
        outcome: "timeout",
        apiMs: this.c.api.workerTimeoutS * 1000 + this.c.api.overheadMs,
        worker: false,
        stage: "dropped",
      };
    }
    let heldMs = 0;
    if (kind === "latency") {
      const { latencyMs, jitterMs } = this.fault.params;
      heldMs = latencyMs + (jitterMs ? (this.rng() * 2 - 1) * jitterMs : 0);
    }
    return { status: 200, outcome: "success", apiMs: base + workerMs + heldMs, workerMs, heldMs, worker: true, stage: "ok" };
  }

  send() {
    const id = ++this.seq;
    const plan = this.plan();
    const request = { id, start: this.t, done: this.t + plan.apiMs / 1000, plan };
    this.inFlight.push(request);
    if (plan.worker) {
      const arrive = this.t + (this.c.api.overheadMs + 2 * this.c.healthy.hopMs) / 1000;
      this.workerPending.push({ done: arrive + plan.workerMs / 1000, workerMs: plan.workerMs });
      this.metrics.proxy.sentUpstream += 180;
    }
    this.emit("request", request);
  }

  complete(request) {
    const api = this.metrics.api;
    const { status, outcome, apiMs } = request.plan;
    api.requests[status] = (api.requests[status] ?? 0) + 1;
    api.hist.observe(apiMs / 1000);
    api.workerCalls[outcome] += 1;
    if (request.plan.worker) this.metrics.proxy.sentDownstream += 260;
    if (status === 200) this.loadStats.success += 1;
    else this.loadStats.errors += 1;
    this.emit("response", request);
  }

  tick(dt) {
    const end = this.t + dt;
    const step = 1 / this.c.load.ratePerSecond;
    while (this.nextSend <= end) {
      this.t = Math.max(this.t, this.nextSend);
      this.settle(this.t);
      if (this.inFlight.length < this.c.load.maxInFlight) this.send();
      else this.loadStats.skipped += 1; // no catch-up bursts, like the real generator
      this.nextSend += step;
    }
    this.t = end;
    this.settle(end);
    this.accountCpu(dt);
    if (this.fault && this.t >= this.fault.end) this.stop("duration_elapsed");
  }

  settle(now) {
    const finished = this.inFlight.filter((r) => r.done <= now).sort((a, b) => a.done - b.done);
    this.inFlight = this.inFlight.filter((r) => r.done > now);
    for (const r of finished) this.complete(r);
    const workerDone = this.workerPending.filter((w) => w.done <= now);
    this.workerPending = this.workerPending.filter((w) => w.done > now);
    const worker = this.metrics.worker;
    for (const w of workerDone) {
      worker.requests[200] = (worker.requests[200] ?? 0) + 1;
      worker.hist.observe(w.workerMs / 1000);
    }
  }

  accountCpu(dt) {
    const jitter = () => 1 + (this.rng() - 0.5) * 0.2;
    this.metrics.api.cpuSeconds += this.c.cpuBaseline.api * jitter() * dt;
    if (!this.isUp("worker")) return;
    const cores = this.fault?.kind === "cpu" ? this.c.cpuFault.workerCores : this.c.cpuBaseline.worker * jitter();
    this.metrics.worker.cpuSeconds += cores * dt;
  }

  // Counter snapshot a Prometheus scrape of `job` would see.
  scrape(job) {
    if (job === "impairment-proxy") {
      const p = this.metrics.proxy;
      return { "proxy|sent|upstream": p.sentUpstream, "proxy|sent|downstream": p.sentDownstream };
    }
    const m = this.metrics[job];
    const out = { [`${job}|cpu`]: m.cpuSeconds, [`${job}|count`]: m.hist.count, [`${job}|sum`]: m.hist.sum };
    for (const [status, n] of Object.entries(m.requests)) out[`${job}|req|${status}`] = n;
    for (const [le, n] of m.hist.cumulative()) out[`${job}|bucket|${le}`] = n;
    if (job === "api") {
      for (const [outcome, n] of Object.entries(m.workerCalls)) out[`api|calls|${outcome}`] = n;
    }
    return out;
  }
}
