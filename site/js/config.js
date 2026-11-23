// Every tunable number for the demo lives here. Values marked "lab" mirror the repository's
// real configuration; values marked "model" are assumptions of this simulation.

export const CONFIG = {
  // Load generator (lab: scripts/generate_load.py defaults)
  load: { ratePerSecond: 10, maxInFlight: 4, clientTimeoutS: 3 },

  // API -> worker call (lab: lab/application.py)
  api: { workerTimeoutS: 2.0, connectTimeoutS: 0.5, overheadMs: 1.5 },

  // Healthy service times (model: typical few-millisecond local requests)
  healthy: { workerServiceMs: 3.0, workerJitterMs: 1.2, hopMs: 0.3 },

  // CPU fault effect (model: host-dependent; the real lab only guarantees CPU rises)
  cpuFault: { workerCores: 0.49, extraWorkerMs: 18, extraWorkerJitterMs: 10 },

  // Baseline process CPU in cores at ~10 req/s (model)
  cpuBaseline: { api: 0.035, worker: 0.045 },

  // Fault bounds (lab: lab/fault.py, lab/network_fault.py)
  faults: {
    defaultDurationS: 60,
    minDurationS: 1,
    maxDurationS: 120,
    latency: { defaultMs: 600, minMs: 50, maxMs: 1500, maxJitterMs: 250 },
  },

  // Prometheus (lab: monitoring/prometheus.yml)
  prometheus: { scrapeIntervalS: 2, evalIntervalS: 2, retentionS: 600 },

  // Histogram buckets (lab: lab/metrics.py)
  buckets: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 2.5, 5],

  // Alert rules (lab: monitoring/alerts.yml)
  alerts: {
    workerCpuCores: 0.3,
    cpuRateWindowS: 15,
    slowFraction: 0.25,
    slowTrafficGuard: 0.2,
    timeoutShare: 0.2,
    errorShare: 0.2,
    rateWindowS: 30,
    forS: 10,
  },

  // Investigator port (lab: agent/rules_baseline.py, agent/telemetry.py)
  investigator: { stepS: 5, recentS: 30, minRecentSamples: 3, lookbackS: 120 },

  // Display
  ui: {
    chartWindowS: 180,
    chartRefreshMs: 500,
    hopAnimationMs: 380, // packet travel time per hop; slowed for visibility (illustrative)
    maxPackets: 80,
    investigatorAutoRunS: 5,
  },
};
