// Data sources behind one interface, so the same UI can show the simulation, a recorded run
// exported from the real lab, or the live local lab. Only the simulation is implemented yet;
// the others never fall back to invented data.

import { CONFIG } from "./config.js";
import { Prometheus } from "./prom.js";
import { catalogue, diagnose } from "./rules.js";
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

export const RECORDED_STATUS = {
  available: false,
  reason: "No recorded runs are published yet. Real runs exported from the lab will appear here; nothing is shown until they exist.",
};

export function liveStatus() {
  const ok = typeof location !== "undefined" && location.origin === "http://127.0.0.1:8088";
  return {
    available: ok,
    reason: ok ? "Connected to the local live bridge." : "Live mode needs `make live-demo` on the presenting laptop (serves this page at http://127.0.0.1:8088).",
  };
}
