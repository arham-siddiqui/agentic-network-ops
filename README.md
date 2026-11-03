# Agentic Network Ops

A local distributed-services lab for studying infrastructure failures through measured telemetry. Stage 1 implements two Python services, Prometheus monitoring, a provisioned Grafana dashboard, bounded traffic, and one controlled CPU-pressure experiment. The November milestone adds a transparent TCP impairment proxy on the API→worker path with two bounded network scenarios (added latency and request timeouts). Agent-based diagnosis is in progress; see the [roadmap](docs/roadmap.md).

```mermaid
flowchart LR
    load[Bounded load generator] --> api[API]
    api --> proxy[Impairment proxy<br/>Toxiproxy]
    proxy --> worker[Worker]
    prom[Prometheus] -->|scrape /metrics| api
    prom -->|scrape /metrics| worker
    prom -->|scrape, control network| proxy
    grafana[Grafana] -->|query| prom
    local[Local Compose exec] -->|Unix socket| worker
    local -->|Unix socket| netctl[Network fault controller]
    netctl -->|admin API, control network| proxy
```

The application services share one small image. Each runs a single Uvicorn process; `/work` on the API makes a real HTTP call to the worker, which computes a fixed checksum. Both expose `/health` and `/metrics`. The API reaches the worker through [Toxiproxy](https://github.com/Shopify/toxiproxy) 2.13.0, which forwards TCP unchanged unless a network scenario is active.

## Start the lab

Requires Docker Desktop on macOS/Windows, or Docker Engine with Compose v2+ on Linux. Python 3.11+ is needed only for the host demo script; Python and `uv` are needed for local unit tests. No cloud accounts or API keys are required.

Allow about 2 GB of Docker VM memory and two CPU cores as an initial allocation; these are setup estimates, not measured requirements. The six persistent containers have a combined 1,056 MiB memory limit, plus Docker overhead. The application, Prometheus, and Grafana containers are each capped at 0.5 CPU cores; the proxy and network fault controller at 0.25. Images are pulled during setup; the running lab uses an internal Docker network.

```sh
docker compose up --build -d --wait
curl --fail http://127.0.0.1:8000/work
make smoke
```

| Interface | Address | Access |
| --- | --- | --- |
| API health | http://127.0.0.1:8000/health | Localhost |
| API metrics | http://127.0.0.1:8000/metrics | Localhost |
| Prometheus alerts | http://127.0.0.1:9090/alerts | Localhost |
| Grafana dashboard | http://127.0.0.1:3000/d/local-lab | `admin` / `local-lab-only` |

The worker has no published port. Grafana has its data source and dashboard provisioned automatically; log in and open the dashboard link. The password is a public **local lab default**, not a secret. To change it or any published port, copy `.env.example` to `.env` before the first start and edit the values. Grafana stores its initial credentials in its persistent volume.

## Run the incident demo

For an automated baseline → fault → alert → recovery check:

```sh
make check-rules
make demo
# Validation target: repeat the entire experiment three times.
python3 scripts/demo.py --runs 3
```

The script starts bounded traffic, collects a 20-second baseline, injects a 60-second worker CPU fault, checks for `WorkerCpuPressure` firing, waits for natural expiry, and checks both scrape targets, CPU recovery, and alert clearance. It also checks Grafana provisioning and API functionality. It exits with a failure if any required observation is missing. Each run takes roughly two minutes plus setup. Run it alone: concurrent experiments or a manually active fault invalidate the baseline.

Actual observations and ground truth are saved to ignored `artifacts/<experiment-id>.json`, with load output beside them. Detection/recovery times are polling observations, not exact event times. Ground-truth start/end events carry their own UTC timestamps. Three successful repetitions are a target until you run them; this repository does not claim measured incident performance yet.

## Run the network impairment

The impairment proxy sits on the real API→worker TCP connection. Two scenarios are available, each lasting 1–120 seconds (default 60) and expiring automatically:

| Scenario | What the proxy does | Expected regime |
| --- | --- | --- |
| `latency` | Holds each worker→API response chunk for `LATENCY_MS` (50–1500 ms, default 600; optional jitter ≤ 250 ms, total ≤ 1500 ms). | Slower `/work` responses, still HTTP 200. Kept below the API's 2-second worker timeout so it cannot become the timeout regime. |
| `timeout` | Drops API→worker request bytes while keeping connections open. | The API's own 2-second read timeout fires: HTTP 504 and `lab_worker_calls_total{outcome="timeout"}`. |

```sh
make load                                          # terminal 1: bounded traffic
make inject-network-fault                          # terminal 2: latency, 600 ms, 60 s
make inject-network-fault LATENCY_MS=900 DURATION=30
make inject-network-fault SCENARIO=timeout
make network-fault-status
make stop-network-fault                            # early stop; also clears leftover toxics
make network-demo                                  # automated latency run (SCENARIO=timeout too)
python3 scripts/demo.py --scenario latency --runs 3
```

`ApiHighLatency` fires when over 25% of API `/work` responses take longer than 250 ms for 10 seconds, with a traffic guard so idle periods cannot fire. `WorkerCallTimeouts` fires when over 20% of worker calls time out for 10 seconds. Compare the "slower than 250 ms" panel for the API and the worker: a slow API with a fast worker places the delay between them. The network fault marker on the dashboard is ground truth and is **not** an alert input. These delays come from a userspace TCP proxy: they model a slow or black-holed transport path, not packet loss, routing changes, or link failure.

These network scenarios are implemented and unit-tested against a simulated proxy admin API. They have **not yet been run in Docker**; see [validation notes](docs/validation.md).

To explore manually, run traffic in one terminal:

```sh
make load
# Optional: 180 seconds at 10 requests/second, at most 4 in flight.
docker compose run --rm load-generator python scripts/generate_load.py --duration 180 --rate 10 --concurrency 4
```

Then, after about 20 seconds of healthy traffic, use another terminal:

```sh
make inject-cpu-fault       # default: 60 seconds
make fault-status
make stop-fault             # optional early stop; otherwise it expires automatically
```

Watch the CPU, latency, fault marker, and firing-alert panels in Grafana. The default alert evaluates `rate(process_cpu_seconds_total{job="worker"}[15s]) > 0.30` continuously for 10 seconds. At the 0.5-core quota, that threshold represents 60% of the worker's available CPU. Firing is expected within roughly 15–30 seconds of sustained pressure, with several seconds of lag on recovery; these are expectations to verify, not benchmark results. The graph's fault marker is ground truth and is **not** an input to the alert.

CPU usage should rise clearly. Request latency may rise, but a fixed amount of user-visible slowdown is not guaranteed across laptops or Docker VM configurations. The required CPU experiment therefore checks a real **resource-pressure alert**. It does not simulate latency by sleeping or fabricate request metrics. `ServiceUnavailable` and `ApiHighErrorRate` also detect service degradation when it occurs; they are not guaranteed consequences of the CPU fault.

To tune the CPU alert, edit its threshold, `[15s]` rate window, or `for: 10s` in `monitoring/alerts.yml`, update the rule fixtures, run `make check-rules`, and restart Prometheus with `docker compose restart prometheus`. The automated demo assumes the checked-in 0.30-core threshold and 60-second experiment. Short faults may end before the alert can fire.

## Investigate an incident (read-only)

```sh
make investigate                                        # capture the last 120 s and diagnose
uv run --frozen python -m agent.cli capture --lookback 180
uv run --frozen python -m agent.cli diagnose artifacts/investigations/<id>-bundle.json
```

The investigator runs on the host and reads only the local Prometheus on a loopback address. It captures an **evidence bundle**: 21 allowlisted series (scrape health, request rate, latency percentiles, slow and error fractions, worker-call outcomes, process CPU and RSS) plus firing alert names. Each series' PromQL is fixed in [`agent/catalog.py`](agent/catalog.py); nothing accepts a query string. Fault markers, the network fault controller, toxic state, and scenario records are not in the catalogue, and tests check that their names cannot appear in a bundle.

The deterministic baseline ([`agent/rules_baseline.py`](agent/rules_baseline.py)) uses the alert thresholds. It maps the last 30 seconds of evidence to `healthy`, `cpu_pressure`, `network_latency`, `network_connectivity_or_timeout`, `service_unavailable`, or `unknown_or_ambiguous`. It separates network delay from worker slowness by comparing the API's and the worker's own latency. It fails closed: missing, stale, sparse, or undefined data, no traffic, or conflicting signals give `unknown_or_ambiguous`, with the reason listed under `limitations`. Reports follow a versioned schema with cited evidence, user-facing impact, and an advisory recommendation. `action_taken` is always `false`, and nothing in the investigator can change containers, the proxy, or services.

### LLM modes (optional)

```sh
uv sync --frozen --extra llm                    # adds the Anthropic SDK
export ANTHROPIC_API_KEY=...                    # or `ant auth login`; never put keys in .env
uv run --frozen python -m agent.cli investigate --mode llm_snapshot
uv run --frozen python -m agent.cli diagnose artifacts/investigations/<id>-bundle.json --mode llm_tools
```

Both modes read the same saved evidence bundle as the rules, so all three can be compared on identical data. Each run is a billed API call; the default model is `claude-opus-5-5` (override with `--model`).

- `llm_snapshot` sends one request containing a standardized summary of every allowlisted series and the firing alerts.
- `llm_tools` starts with only the window and firing alert names. It may call up to six read-only `get_*` tools (health, rate, latency, slow and error fractions, worker-call outcomes, CPU, RSS, alerts) that summarize series already in the bundle. It has a 30-second wall-time budget and a 60,000-token budget. No tool can query arbitrary PromQL or change anything.

Model output is untrusted. It must match a JSON schema with enumerated causes and metric IDs. Cited values are checked against the bundle, and a mismatch, or (in `llm_tools`) a citation of a metric that was never retrieved, is recorded and added to the report's limitations. Invalid output, a refusal, a provider error, or an exhausted budget produces `unknown_or_ambiguous`. The model's `confidence` is its own uncalibrated estimate. Server-side refusal fallback (`fallbacks: "default"`) is enabled on the request. A `-run-<mode>.json` file records the outcome, turns, token counts, estimated cost, latency, and tool-call names and arguments, but never prompt text or credentials. Tests use a scripted fake provider and make no API calls. **No real-model evaluation has been run yet.**

One overlap is documented rather than hidden. CPU pressure can delay requests before the worker's handler timer starts, so a slow API with a fast worker under high worker CPU is reported as `cpu_pressure` with a limitation saying network delay is not excluded.

## What the metrics mean

| Metric | Meaning |
| --- | --- |
| `up{job="api"}` / `up{job="worker"}` | Prometheus successfully scraped that service; not a dependency-readiness probe. |
| `lab_http_requests_total` | Completed application requests by bounded method, route template, and HTTP status. `/health` and `/metrics` are excluded. HTTP 5xx samples are the error count. |
| `lab_http_request_duration_seconds` | Response-completion histogram in seconds; the dashboard computes approximate p50/p95 over 30 seconds. |
| `lab_worker_calls_total` | API dependency outcomes: success, timeout, error, or invalid response. |
| `process_cpu_seconds_total` | Linux process user + system CPU time, including the fault thread in the same PID. Its rate is CPU cores consumed, not a percentage of the host. |
| `process_resident_memory_bytes` | Resident memory of the application process, not total container memory or host memory. |
| `lab_cpu_fault_active` | 0/1 ground-truth scenario marker; worker only is meaningful. |
| `lab_network_fault_active` | 0/1 ground-truth marker from the network fault controller. Not an alert or diagnosis input. |
| `toxiproxy_proxy_sent_bytes_total` / `_received_bytes_total` | Bytes the impairment proxy forwarded/accepted per `direction` (`upstream` = API→worker). |
| `up{job="impairment-proxy"}` | Prometheus scraped the proxy's metrics endpoint; data-path health shows in API worker-call outcomes. |
| `ALERTS{alertstate="firing"}` | Prometheus's currently firing alerts; no series means no firing alert. |

Unknown paths are grouped under `unmatched`; unknown HTTP methods under `OTHER`. Raw URLs, user data, run IDs, and addresses are not metric labels. Idle traffic panels may be empty until requests arrive; latency percentiles are undefined without observations. Request logs are JSON records with timestamps, service, bounded route/method, status, and duration.

## Limits and controls

- Faults are off until explicitly requested. A fault runs in a worker thread for 1–120 seconds, defaults to 60 seconds, rejects concurrent starts, and ends on graceful shutdown. A container-local socket, mode `0600`, accepts controls through `docker compose exec`; there is no fault HTTP endpoint. Fault control refuses to start outside Docker.
- The worker's 0.5-core CPU and 192 MiB memory limits bound its resource use. There are no privileged containers, Docker socket mounts, host process mounts, or host network mode. Project monitoring files are mounted read-only; data uses named volumes.
- Load targets the fixed `http://api:8000/work` address, follows no redirects, and accepts no external target option. Defaults are 120 seconds, 10 requests/second, and four in-flight requests; hard caps are 600 seconds, 50 requests/second, and eight in flight. Slow requests cause skipped sends rather than an expanding queue. Requests have a three-second timeout and may finish just after the generation deadline.
- Network faults use one fixed proxy (`api_to_worker` → `worker:8000`, from a read-only config file) and two fixed toxic definitions. The controller rejects unknown fields, refuses to start while any toxic is present, removes its toxic in a `finally` block with retries, and clears leftovers when it starts or when `stop` runs with nothing active. Ground truth goes to `/var/lib/lab/network-scenarios.jsonl` in the controller's volume. The proxy's admin API listens only on the internal `control` network, which the API, worker, load generator, and smoke test do not join. That is network segmentation, not authentication: anything on the control network (the controller and Prometheus) can reach it.
- API calls have a two-second downstream timeout, a 0.5-second connection timeout, bounded connections, and no retries. Worker failures become 502 responses; timeouts become 504. `/health` only reports process liveness.
- User-facing ports bind to `127.0.0.1`. The internal network isolates outbound container traffic. These controls describe a local development lab, not a production deployment.
- Scenario events persist at `/var/lib/lab/scenarios.jsonl` in the worker volume and also appear in worker logs. Abrupt container termination can leave a start event without an end event; treat that run as interrupted. Docker logs rotate, but scenario history remains until explicitly removed.

```sh
docker compose logs --tail 100 worker
docker compose exec -T worker cat /var/lib/lab/scenarios.jsonl
docker compose down         # keep metrics, Grafana state, and scenario history
docker compose down -v      # explicitly delete all lab volumes and recorded history
```

## Development and validation

```sh
uv sync --frozen
make test                   # includes the optional LLM extra; makes no API calls
make lint
make check-rules            # requires Docker; native promtool also works (below)
```

Application and development dependencies are resolved in `uv.lock`; Docker installs the hash-pinned runtime export in `requirements.txt`. After intentionally updating the lock, refresh that export with:

```sh
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt
```

Container images use explicit version tags, not immutable digests. Lock files pin Python artifacts; image tags can still be republished upstream.

Tests cover real API-to-worker ASGI requests, downstream failures, HTTP instrumentation, label bounds, fault duration/stop/expiry behavior, network-fault parameter bounds and toxic cleanup against a simulated proxy admin API, the Unix control protocol, and load parameter limits. Fault unit tests use a substitute burner or a simulated clock; they do not stress the host. Prometheus fixtures test healthy, firing, recovery, and short-burst cases:

```sh
promtool check rules monitoring/alerts.yml
promtool test rules monitoring/rules.test.yml
```

See [validation notes](docs/validation.md) for checks actually executed and remaining Docker verification. On native macOS, the Python client's Linux process metrics are absent. Run the full demo in Linux containers, including Docker Desktop's Linux VM; it measures those application processes, not the Mac host. Node Exporter and cAdvisor are deliberately not dependencies. No packet-loss, security detection, or remediation capability is implemented.

Further design details: [architecture](docs/architecture.md). Next milestones: [roadmap](docs/roadmap.md).

## Troubleshooting

- `docker: command not found` or daemon unavailable: install/start Docker Desktop or Docker Engine + Compose, then retry the startup command.
- A port is in use: set a different port in `.env`, recreate the containers, and use the new localhost URL. The automated demo reads effective Compose port mappings.
- Dashboard empty: run traffic and allow at least 30 seconds of scrapes; check Prometheus targets at `/targets` and confirm both report UP.
- CPU alert does not fire: inspect worker logs, its CPU time series, the fault marker, and scrape availability. Check Docker VM CPU allocation and avoid competing workloads. Preserve the failed demo report; do not treat missing CPU metrics as recovery.
- Network latency never clears or `make stop-network-fault` fails: run `make network-fault-status` to see `proxy_toxics`. If the controller itself is unavailable, `docker compose restart impairment-proxy` discards all toxics (Toxiproxy keeps them in memory only) and reloads the fixed proxy definition.
- Grafana login fails after changing `.env`: the existing volume retains its prior password. Use the old login or reset it through Grafana's supported administration tools. Removing volumes also resets it but deletes lab history.
