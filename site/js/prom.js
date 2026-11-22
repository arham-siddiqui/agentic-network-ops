// A small Prometheus stand-in: 2 s scrapes into an in-memory TSDB, range functions, and the
// alert rules from monitoring/alerts.yml evaluated every 2 s with pending -> firing semantics.

import { CONFIG } from "./config.js";

export const JOBS = ["api", "worker", "impairment-proxy"];

export class Prometheus {
  constructor(lab, config = CONFIG) {
    this.lab = lab;
    this.c = config;
    this.reset();
  }

  reset() {
    this.series = new Map(); // key -> [[t, v], ...]
    this.lastScrape = -Infinity;
    this.lastEval = -Infinity;
    this.alerts = new Map(); // instance key -> state
    this.alertLog = [];
  }

  push(key, t, v) {
    let s = this.series.get(key);
    if (!s) this.series.set(key, (s = []));
    s.push([t, v]);
    const cutoff = t - this.c.prometheus.retentionS;
    while (s.length && s[0][0] < cutoff) s.shift();
  }

  scrape(t) {
    for (const job of JOBS) {
      const up = this.lab.isUp(job) ? 1 : 0;
      this.push(`up|${job}`, t, up);
      if (!up) continue; // a failed scrape stores no samples for that target
      for (const [key, value] of Object.entries(this.lab.scrape(job))) this.push(key, t, value);
    }
  }

  // Called after every simulation tick.
  advance(t) {
    const p = this.c.prometheus;
    while (this.lastScrape + p.scrapeIntervalS <= t) {
      this.lastScrape = this.lastScrape === -Infinity ? t : this.lastScrape + p.scrapeIntervalS;
      this.scrape(this.lastScrape);
    }
    while (this.lastEval + p.evalIntervalS <= t) {
      this.lastEval = this.lastEval === -Infinity ? t : this.lastEval + p.evalIntervalS;
      this.evaluate(this.lastEval);
    }
  }

  // ----- query functions (each takes an evaluation time) -----

  instant(key, t, staleS = 300) {
    const s = this.series.get(key);
    if (!s) return null;
    for (let i = s.length - 1; i >= 0; i--) {
      if (s[i][0] <= t) return t - s[i][0] <= staleS ? s[i][1] : null;
    }
    return null;
  }

  rate(key, t, windowS) {
    const s = this.series.get(key);
    if (!s) return null;
    let first = null;
    let last = null;
    for (const point of s) {
      if (point[0] > t) break;
      if (point[0] > t - windowS) {
        if (!first) first = point;
        last = point;
      }
    }
    if (!first || first === last) return null;
    return (last[1] - first[1]) / (last[0] - first[0]);
  }

  sumRate(prefix, t, windowS, filter = () => true) {
    let total = null;
    for (const key of this.series.keys()) {
      if (!key.startsWith(prefix) || !filter(key.slice(prefix.length))) continue;
      const r = this.rate(key, t, windowS);
      if (r !== null) total = (total ?? 0) + r;
    }
    return total;
  }

  requestRate(job, t, w = this.c.alerts.rateWindowS) {
    return this.sumRate(`${job}|req|`, t, w);
  }

  errorRate(job, t, w = this.c.alerts.rateWindowS) {
    return this.sumRate(`${job}|req|`, t, w, (status) => status.startsWith("5"));
  }

  // Catalogue error fraction: undefined (NaN) when idle.
  errorFraction(job, t) {
    const total = this.requestRate(job, t);
    if (total === null) return null;
    return total > 0 ? (this.errorRate(job, t) ?? 0) / total : NaN;
  }

  slowFraction(job, t, w = this.c.alerts.rateWindowS) {
    const fast = this.rate(`${job}|bucket|0.25`, t, w);
    const count = this.rate(`${job}|count`, t, w);
    if (fast === null || count === null) return null;
    return count > 0 ? 1 - fast / count : NaN;
  }

  quantile(job, q, t, w = this.c.alerts.rateWindowS) {
    const buckets = [...this.c.buckets, Infinity].map((le) => [le, this.rate(`${job}|bucket|${le}`, t, w)]);
    if (buckets.some(([, r]) => r === null)) return null;
    const total = buckets[buckets.length - 1][1];
    if (!(total > 0)) return NaN;
    const rank = q * total;
    let prevLe = 0;
    let prevCount = 0;
    for (const [le, count] of buckets) {
      if (count >= rank) {
        if (le === Infinity) return this.c.buckets[this.c.buckets.length - 1];
        if (count === prevCount) return le;
        return prevLe + ((le - prevLe) * (rank - prevCount)) / (count - prevCount);
      }
      prevLe = le;
      prevCount = count;
    }
    return NaN;
  }

  cpu(job, t) {
    return this.rate(`${job}|cpu`, t, this.c.alerts.cpuRateWindowS);
  }

  calls(outcome, t) {
    return this.rate(`api|calls|${outcome}`, t, this.c.alerts.rateWindowS);
  }

  up(job, t) {
    return this.instant(`up|${job}`, t);
  }

  // ----- alert rules (monitoring/alerts.yml) -----

  ruleValues(t) {
    const a = this.c.alerts;
    const callsTotal = ["success", "timeout", "error", "invalid_response"]
      .map((o) => this.calls(o, t))
      .reduce((sum, r) => (r === null ? sum : (sum ?? 0) + r), null);
    const apiTotal = this.requestRate("api", t);
    const slow = this.slowFraction("api", t);
    const countRate = this.rate("api|count", t, a.rateWindowS);
    return [
      {
        name: "WorkerCpuPressure",
        severity: "warning",
        value: this.cpu("worker", t),
        threshold: a.workerCpuCores,
        unit: "cores",
        firing: (v) => v !== null && v > a.workerCpuCores,
      },
      ...["api", "worker"].map((job) => ({
        name: "ServiceUnavailable",
        job,
        severity: "critical",
        value: this.up(job, t),
        threshold: 0,
        unit: "up",
        firing: (v) => v === 0,
      })),
      {
        name: "ApiHighErrorRate",
        severity: "warning",
        value: apiTotal === null ? null : (this.errorRate("api", t) ?? 0) / Math.max(apiTotal, 0.1),
        threshold: a.errorShare,
        unit: "fraction",
        firing: (v) => v !== null && v > a.errorShare,
      },
      {
        name: "ApiHighLatency",
        severity: "warning",
        value: Number.isFinite(slow) ? slow : null,
        threshold: a.slowFraction,
        unit: "fraction",
        firing: (v) => v !== null && v > a.slowFraction && countRate !== null && countRate > a.slowTrafficGuard,
      },
      {
        name: "WorkerCallTimeouts",
        severity: "warning",
        value: callsTotal === null ? null : (this.calls("timeout", t) ?? 0) / Math.max(callsTotal, 0.1),
        threshold: a.timeoutShare,
        unit: "fraction",
        firing: (v) => v !== null && v > a.timeoutShare,
      },
      {
        name: "ImpairmentProxyUnavailable",
        severity: "critical",
        value: this.up("impairment-proxy", t),
        threshold: 0,
        unit: "up",
        firing: (v) => v === 0,
      },
    ];
  }

  evaluate(t) {
    for (const rule of this.ruleValues(t)) {
      const key = rule.job ? `${rule.name}{job="${rule.job}"}` : rule.name;
      const state = this.alerts.get(key) ?? { key, name: rule.name, job: rule.job, state: "inactive" };
      Object.assign(state, { severity: rule.severity, value: rule.value, threshold: rule.threshold, unit: rule.unit });
      const active = rule.firing(rule.value);
      if (active && state.state === "inactive") {
        Object.assign(state, { state: "pending", activeAt: t });
        this.alertLog.push({ t, key, to: "pending" });
      } else if (active && state.state === "pending" && t - state.activeAt >= this.c.alerts.forS) {
        Object.assign(state, { state: "firing", firedAt: t });
        this.alertLog.push({ t, key, to: "firing" });
      } else if (!active && state.state !== "inactive") {
        this.alertLog.push({ t, key, to: state.state === "firing" ? "resolved" : "inactive" });
        Object.assign(state, { state: "inactive", resolvedAt: t, wasFiring: state.state === "firing" });
      }
      this.alerts.set(key, state);
    }
  }

  firing() {
    return [...this.alerts.values()].filter((a) => a.state === "firing");
  }
}
