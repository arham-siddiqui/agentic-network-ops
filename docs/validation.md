# Validation record

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
