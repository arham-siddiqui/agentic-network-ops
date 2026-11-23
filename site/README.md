# Fault Lab Console (demo site)

A static page that shows the lab's topology, traffic, injected faults, Prometheus alerts, and the read-only investigator, for walking someone through the project. It is published from this folder to GitHub Pages by `.github/workflows/pages.yml` on every push to `demo-site` or `main` that touches `site/`.

Live: https://arham-siddiqui.github.io/agentic-network-ops/

## Modes

| Mode | Data | Status |
| --- | --- | --- |
| **Simulation** | A browser model of the lab (`js/sim.js`) feeding a small Prometheus stand-in (`js/prom.js`) that scrapes every 2 s and evaluates the real alert rules with their windows and `for` durations. The investigator card runs a JS port of `agent/rules_baseline.py` (`js/rules.js`). | Working. Labelled "modeled behavior, not measurements". |
| **Recorded run** | Real runs exported from the lab into `data/recorded/<name>.json`, listed in `data/recorded/index.json` (`["<name>.json", ...]`). Replays each incident's evidence bundle over its 120 s window, with the ground-truth band and the real rules report at the decision time. | Disabled until real files exist. Never add invented data there. |
| **Live (local)** | Polls `GET /live/snapshot` every 2 s from the read-only bridge (`make live-demo`, which serves this folder at http://127.0.0.1:8088). Faults come from the terminal. | Enabled only when the page is served from that origin. |

What is real and what is modeled in Simulation mode:

- **Mirrors the repository:** topology and trust boundaries; load limits (10 req/s, ≤ 4 in flight); the API's 2 s worker timeout; fault kinds and bounds (1–120 s, latency 50–1500 ms, one fault at a time, automatic expiry); histogram buckets; every alert expression, window, threshold, and `for`; the catalogue queries; the rules decision tree.
- **Assumed:** healthy service times (a few ms), process CPU baselines, and how much the CPU fault slows the worker (the real lab only guarantees that CPU rises). Packet animation is slowed for visibility. Fault delays are drawn at real scale. "Proxy down" is simulation-only; "worker down" also exists in the real lab (`make worker-outage`).

One emergent effect worth pointing out while presenting: because the load generator caps in-flight requests at 4, slow or timing-out responses reduce throughput. The request-rate panel dips during latency and timeout faults, and alerts take a little longer to cross their thresholds than a constant-rate estimate suggests.

## Run locally

```sh
python3 -m http.server -d site 8080
open http://127.0.0.1:8080/
```

Deep links (handy for presenting):

| Parameter | Effect |
| --- | --- |
| `?warmup=60` | seconds of healthy history to pre-run (default 60) |
| `?fault=latency&after=35` | inject `latency`, `timeout`, `cpu`, `worker_down`, or `proxy_down` after the warm-up, then run `after` more seconds |
| `?mode=recorded&incident=0&at=end` | open a recorded incident, optionally at its decision time |
| `?mode=live` | open Live mode (only from `make live-demo`) |

## Tweak it

- **Numbers:** every tunable value is in `js/config.js`. Values marked *lab* mirror the repo; values marked *model* are assumptions. Examples: healthy service time, CPU-fault slowdown, chart window, packet speed.
- **Colors and layout:** `css/style.css`. API is blue (`#4c95ea`) and worker orange (`#e0703c`) throughout.
- **Layout:** the main view is deliberately small: the topology hero with one row of fault buttons, three story tiles (user impact, alerts, diagnosis), and three charts (p95 API vs worker, API→worker call outcomes, worker CPU). Everything else (more charts, every alert rule, the full investigator report and evidence, the event log, security boundaries, and an explanation of the mode) sits in collapsed sections below. Speed and pause are in the header; fault duration and the simulation-only "proxy down" are under *Options*.
- **Presenter steps:** the `STEPS` array in `js/app.js`.
- **Rules port:** `js/rules.js` must stay in step with `agent/rules_baseline.py`; the Python code is the source of truth.

Tests (Node 20+, no dependencies):

```sh
node --test site/tests/sim.test.js site/tests/sources.test.js
```

They check alert timing (a sustained 600 ms latency fault fires `ApiHighLatency`; a 3 s burst does not), each fault's diagnosis, fault bounds, and Recorded/Live replay. The fixtures in `tests/` are synthetic and are never shown on the site.
