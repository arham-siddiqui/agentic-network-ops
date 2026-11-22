# Validation record

## 2026-11-21: first Docker runtime (colima)

macOS 15.6.1 arm64; colima VM (vz, 4 vCPU, 6 GiB, Ubuntu 24.04), Docker Engine 29.5.2, Compose 5.8.0. These are the first container runs of this repository. Single runs are observations, not benchmarks.

| Check | Observed result |
| --- | --- |
| `make check-rules` | Config valid; 6 rules found; all rule fixtures pass, including the network fixtures that had not been executed before. |
| `make up` (first attempt) | **Failed.** Grafana exited with `fatal error: newosproc`: the shared 64-PID cap counts threads, and Grafana 13 starts one process per bundled plugin. Fixed with `pids_limit: 512` for Grafana (91 PIDs observed at idle). |
| Published ports (first attempt) | **Not bound.** Docker does not publish ports for containers attached only to `internal: true` networks, so the Stage 1 localhost URLs never worked. Fixed with an `edge` bridge for API, Prometheus, and Grafana with IP masquerading disabled. Requests to `http://1.1.1.1` then failed from the API, worker, Prometheus, and Grafana containers. |
| Grafana memory | 227 MiB of a 256 MiB limit after startup and a smoke test; the limit was raised to 384 MiB. |
| `make up` / `curl /work` / `make smoke` (after fixes) | All six services healthy. `/work` returned a checksum through the proxy. Smoke passed, including the proxy path in both directions and fault-control endpoints unreachable from the `lab` network. The proxy admin binding to its control-network alias worked. |
| CPU experiment (`demo.py --scenario cpu`, 1 run) | Passed. Worker CPU went from 0.011 cores at baseline to a peak sample of 0.476 against the 0.5 quota. `WorkerCpuPressure` was observed firing 22.1 s after injection; recovery was observed 6.0 s after expiry; start/end records paired (`duration_elapsed`). API p95 rose from about 5 ms to about 89 ms. |
| Latency experiment (`demo.py --scenario latency`, 600 ms, 1 run) | Passed. `ApiHighLatency` was observed 24.2 s after injection; recovery 18.1 s after expiry; records paired. During the fault the API slow fraction was 1.0 and the worker's was 0.0. API p95 read 0.975 s, a histogram-bucket estimate within the 0.5–1 s bucket. |
| Added latency per request | Four direct `curl` requests to `/work` during the fault took 0.603–0.608 s (one delay per request, not one per chunk or direction). One took 0.223 s; it coincided with the toxic's removal at 07:26:48.97 UTC, when Toxiproxy releases held data immediately. |
| Throughput under latency | The API request rate fell from about 9.8 to 6.2 req/s because the load generator caps in-flight requests at 4. This is real backpressure, not a fault in the generator. |
| Timeout experiment (`demo.py --scenario timeout`, 1 run) | Passed. `WorkerCallTimeouts` was observed 28.2 s after injection; recovery 16.1 s after expiry; records paired. The peak timeout share was 0.85 and the API 5xx fraction 0.84. Over the run, 116 requests returned 504 after 2.002–2.010 s (median 2.005 s), matching the API's 2 s worker timeout. |
| Recovery artifact of the timeout toxic | Exactly 4 requests returned 502 at 07:30:25.140 UTC, 1 ms after the toxic was removed. Toxiproxy closes the connections a removed toxic affected, so the 4 in-flight requests (the load generator's concurrency) failed with `worker unavailable`. |
| Latency histogram resolution | With buckets at only 1/2/5 s, the timed-out requests (about 2.005 s) were interpolated to a 4.8 s p95. Buckets at 0.75, 1.5, and 2.5 s were added; the change was rebuilt and verified on `/metrics`. Results recorded above used the old buckets. |
| `make investigate` during the latency fault | Rules verdict: `network_latency`. Cited: API slow fraction 1.0, worker slow fraction 0.0, worker CPU 0.007 cores, all targets up. |
| Error fraction with zero 5xx | The query returned an empty vector (no 5xx series exists yet), which the investigator treated as missing data. Fixed with `or on () vector(0)` on the numerator; idle traffic remains undefined. |
| `scripts/live_bridge.py` against real Prometheus | Snapshot showed the CPU marker, `WorkerCpuPressure` firing, and worker CPU 0.37 cores during the CPU run; during the latency run it showed `ApiHighLatency` firing with API/worker slow fractions of 1.0/0.0. |

## 2026-11-03: network impairment slice (branch `feat/november-incident-diagnosis`)

Run on macOS arm64 (12 CPUs, 24 GiB), CPython 3.12.13 via `uv`. **Docker is still not installed on this machine, so Milestone 0 (Compose startup, smoke test, CPU demo ×3) and all network-scenario runtime checks are blocked, not passed.**

| Check | Observed result |
| --- | --- |
| `uv run --frozen pytest -q` before changes | 37 passed (same Starlette deprecation warning). |
| `uv run --frozen pytest -q` after the network slice | 63 passed. New: network toxic bounds, fixed toxic definitions, apply/expiry/early-stop/idempotent stop, stale-toxic refusal and clearing, misconfigured and unreachable proxy, revert retry and `cleanup_failed`, startup reconcile, unknown request fields, a slow revert, and demo/alert-rule consistency. All run against an in-memory fake of the Toxiproxy admin API. |
| `ruff check .` / `ruff format --check .` | Passed. |
| YAML parse of `compose.yaml`, `monitoring/*.yml` | Parsed. Resolved service networks, users, read-only roots, dropped capabilities, and CPU/memory limits inspected for all eight services. This is not `docker compose config`. |
| `promtool test rules` for the new `ApiHighLatency`, `WorkerCallTimeouts`, and `ImpairmentProxyUnavailable` fixtures | **Not executed.** promtool needs Docker (`make check-rules`) or a native binary; downloading one was not permitted in this session. Run `make check-rules` first once Docker is available. |
| Toxiproxy behavior | **Not executed.** Admin endpoints, error codes, toxic semantics (`latency` holds each chunk; `timeout` with 0 drops data and keeps the connection open; removal closes affected links), the `-host`/`-proxy-metrics` flags, metric names and labels, and the scratch-based image contents were read from the v2.13.0 source and release configuration. |

### Investigator and evaluation slices (same day, same machine)

| Check | Observed result |
| --- | --- |
| `make test` (`uv run --frozen --all-extras pytest -q`) | 132 passed. Investigator: catalogue allowlist and leakage, loopback-only source, bounded lookback, NaN/absent/stale/sparse/unavailable handling, untrusted alert labels, and each rules verdict, including fail-closed cases and the documented CPU/network overlap. LLM: scripted provider for valid, invalid, out-of-range, refused, and provider-error outputs, wrong citations, unretrieved citations, unlisted or malicious tool calls, and tool/wall/token budgets; the Anthropic request shape is checked against a fake client. Evaluation: end-to-end against a fake lab, label-free bundles, seeded order, failed-trial retention and cleanup, write-once predictions, scoring denominators. |
| `uv run --frozen python -m agent.cli investigate` with no Prometheus running | Saved an `unknown_or_ambiguous` report with the limitation "Prometheus was unavailable" (fails closed, not healthy). A non-loopback `--prometheus-url` was refused. |
| `ruff check .` / `ruff format --check .` | Passed. |
| Anthropic SDK | `anthropic` 1.17.0 installed as the optional `llm` extra. **No API request was made**, so real-model behavior, latency, token use, and cost are unmeasured. |

Remaining runtime checks, in order: `make check-rules`; `make up`; `make smoke`, which now also requires proxy and controller scrapes, traffic through the proxy in both directions, and the fault-control endpoints being *unreachable* from the lab network. Then `python3 scripts/demo.py --scenario latency --runs 3` and `--scenario timeout --runs 3`. Also check that the measured added latency per request roughly equals `LATENCY_MS` rather than a multiple of it, and record any 502s seen as the timeout toxic is removed. Then run `make investigate` during each fault, `make evaluate`, and only then, if wanted and approved, `make evaluate-score RUN=... MODES=llm_snapshot,llm_tools`, which makes billed model calls.

## 2026-10-09: initial Stage 1 checks

Initial checks ran on 2026-10-09 on macOS arm64, using CPython 3.12.13. Docker was not installed. **Container startup, Grafana runtime behavior, and the actual CPU-pressure demonstration remain unverified.** Stage 1 acceptance is pending those checks; the presence of working code and synthetic tests does not establish successful container experiments.

## Executed checks

| Check | Observed result |
| --- | --- |
| `uv sync --frozen` | Installed the locked application and test dependencies. |
| `uv run --frozen pytest -q` | 37 tests passed. One upstream Starlette TestClient deprecation warning about its HTTPX adapter; no test failures. |
| `uv run --frozen ruff check .` and formatting | Passed. |
| Prometheus 3.15.0 `promtool check rules monitoring/alerts.yml` | All three alert definitions accepted. |
| `promtool test rules monitoring/rules.test.yml` | CPU healthy/firing/recovery, short CPU burst, unavailable service, and API error-rate fixtures passed. These use synthetic input series. |
| Native `promtool check config` | Accepted a temporary copy of the committed configuration with local test ports and an absolute rule-file path. |
| Standalone Compose 5.6.0 `config --format json` | Configuration accepted; normal and tools-profile services resolved. Inspected internal networking, localhost port bindings, CPU limits, and read-only filesystems. No engine/container execution occurred. |
| Native HTTP integration | Two separate Uvicorn processes communicated over localhost TCP; 25 of 25 work requests returned HTTP 200. Live Prometheus reported both targets UP and request counts for both. |
| Native failure/recovery | Terminating only the test worker produced HTTP 502 from the API. `ServiceUnavailable` was observed firing after 11.03 seconds of polling. Restarting the test worker restored requests and both scrapes; the alert cleared after 1.83 seconds of recovery polling. |
| Dashboard queries | A live native Prometheus server accepted all ten checked-in panel expressions. This checks PromQL validity, not browser rendering or Grafana provisioning. |
| Image metadata | Selected Python, Prometheus, and Grafana tags were present in the public registry with Linux amd64 and arm64 variants. Images were not built or run. |
| Repository privacy | The source brief was not copied into the repository. Brief filename patterns, local secrets, and experiment artifacts are ignored; generated project content contains no private conversation narrative. |

The temporary native processes bound to localhost and were stopped after testing. CPU fault controls stayed disabled. The native availability experiment is a validation of HTTP error handling and an alert, **not a CPU incident benchmark**. No latency, CPU, or detection-accuracy claims are inferred from it. Native macOS does not export the Linux process metrics used by the container demo.

Prometheus and the standalone Compose parser were downloaded to temporary directories and checked against their published SHA-256 checksums. They were not installed globally or added to the repository.

## Remaining Docker acceptance checks

After installing and starting Docker Desktop or Docker Engine with Compose, run from the repository:

```sh
docker compose up --build -d --wait
curl --fail http://127.0.0.1:8000/work
make check-rules
make smoke
python3 scripts/demo.py --runs 3
```

Then open http://127.0.0.1:3000/d/local-lab and log in with the configured Grafana credentials. During a demo, verify that the dashboard visibly shows actual request traffic, CPU/RSS, the scenario interval, and the firing alert. Confirm `WorkerCpuPressure` in http://127.0.0.1:9090/alerts. Read `artifacts/*.json` for observed CPU change, detection and recovery intervals, ground truth, and pass/fail status.

The container smoke check validates both applications, Linux CPU/RSS metrics, a finite latency percentile, the provisioned dashboard, and Grafana's Prometheus data-source health. The experiment additionally requires a healthy baseline, a measured CPU rise above the default threshold, a firing CPU alert, natural fault expiry, healthy scrapes, and cleared alerts. A missing metric is a failure, not evidence of recovery.

Also exercise `make inject-cpu-fault` followed by `make stop-fault` to verify early cancellation in the real container. Inspect worker logs and `/var/lib/lab/scenarios.jsonl` for paired start/end records. Finish with `docker compose down` to stop the lab while retaining results. Use `docker compose down -v` only when intentionally deleting lab history.
