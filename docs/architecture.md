# Architecture

The API and worker are separate processes communicating over HTTP on an internal Compose network. A shared Python package and Docker image keep the small codebase consistent. The API has a lifespan-managed HTTP client and propagates downstream failures with stable error messages. The worker performs a small deterministic checksum computation; traffic volume and fault duration are independently bounded.

One Uvicorn process per service is an intentional constraint. Process CPU and memory metrics from the [Prometheus Python process collector](https://prometheus.github.io/client_python/collector/) describe that process on Linux. The CPU burner is a Python thread inside the worker PID, so its user/system CPU time is included. A separate stress process would require different resource instrumentation. Changing to multiple Uvicorn workers would also require redesigning aggregation and fault targeting.

The CPU thread competes for the GIL and the container's 0.5-core quota, creating actual CPU pressure. Effects on request latency depend on host scheduling, VM capacity, and traffic. The resource alert uses process CPU rate; it does not depend on the scenario's marker. Synthetic rule fixtures verify alert semantics, while the Compose experiment must verify the actual causal chain and recovery on a target machine.

Prometheus scrapes every two seconds and evaluates rules every two seconds. The default CPU rule uses a 15-second rate and ten-second pending period, following [Prometheus alert rule semantics](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/). Separate availability and API-error rules provide service-level signals. `ApiHighLatency` (over 25% of API `/work` responses slower than 250 ms, with at least 0.2 requests/second) and `WorkerCallTimeouts` (over 20% of worker calls timing out) cover the network scenarios. `ImpairmentProxyUnavailable` reports a failed proxy scrape. All three hold for 10 seconds. No Alertmanager is configured: alerts appear in Prometheus and Grafana, with no email, paging, or external delivery.

The [Grafana provisioning configuration](https://grafana.com/docs/grafana/latest/administration/provisioning/) loads a stable Prometheus data-source UID and a twelve-panel dashboard from version-controlled files. Panels show scrape health, request volume, p50/p95 latency, error fraction, process CPU/RSS, dependency outcomes, the slow-response fraction per service, proxy throughput by direction, scenario activity, and firing alerts. All time series share the dashboard time axis; scenario JSON events provide exact start/end timestamps for comparison.

Fault controls are separated from workload HTTP. A local Unix socket exists only when the Docker worker starts with fault control enabled. The non-root container user owns it. Start commands validate finite duration bounds before launching a thread; an event supports early stop, a monotonic deadline bounds execution, and shutdown joins the thread. This is a development control, not an authorization mechanism suitable for a hostile multi-tenant service.

Ground truth is recorded separately from telemetry. Each run has a UUID, scenario name, method, duration, configured CPU limit, UTC start time, and an end event with reason and UTC end time. These records are never used to trigger alerts. The host demo reads them only to validate the experiment and saves observations in ignored local artifacts. A future investigator should receive telemetry without these answer labels.

## Network-path impairment

The API's `WORKER_URL` points at `impairment-proxy:8000`, a Toxiproxy 2.13.0 listener that forwards to `worker:8000`. The proxy definition is a read-only file (`network/toxiproxy.json`). In healthy mode the proxy has no toxics and only forwards bytes. The API keeps its existing timeouts: 2 seconds total per worker call, 0.5 seconds to connect.

The scenarios are written around those timeouts. The `latency` scenario adds a Toxiproxy `latency` toxic on the downstream stream (worker→API). Each response chunk is held until `latency ± jitter` after it arrived, so a small `/work` response should gain roughly one delay, not one per chunk; that expectation must be measured in Docker before it is reported. Latency plus jitter is capped at 1,500 ms, so it stays a slow-success regime. The `timeout` scenario adds a `timeout` toxic with `timeout: 0` on the upstream stream (API→worker). It drops request bytes while keeping the TCP connection open, so the API's read timeout fires and returns 504. When the toxic is removed, Toxiproxy closes the affected connections; a reused keep-alive connection may then cause a brief 502 during recovery, which the experiment report records rather than hides.

### Why a proxy rather than `tc netem`

| | Toxiproxy (chosen) | `tc netem` |
| --- | --- | --- |
| Privilege | None; non-root, all capabilities dropped | `NET_ADMIN` in the target's network namespace |
| Portability | Same on Docker Desktop and Linux | Depends on the Docker VM kernel's `sch_netem` module |
| Fidelity | Delays or drops data at the TCP stream level | Per-packet delay, loss, reordering, and rate on an interface |
| Blast radius | One named proxy on one service path | Everything on the interface it is attached to |
| Cleanup | Toxics are removed through the admin API; a proxy restart discards all of them | `qdisc` must be deleted, or it persists until the namespace goes away |

The proxy gives a privilege-free, portable, narrowly scoped November demo. It cannot model packet loss, retransmission, reordering, or routing convergence, so results are reported as "transport-path latency/timeout", never "packet loss". A `tc netem` sidecar sharing the worker's network namespace is the natural follow-up for packet-level studies.

### Control path and trust boundary

The proxy joins two internal networks. On `lab` it serves the data port to the API. On `control` it has the alias `proxy-admin`, and its admin API binds only to that alias (`-host=proxy-admin`). The API, worker, load generator, and smoke test are only on `lab`, so they cannot open the admin port; the smoke test checks this from the `lab` side. Only Prometheus (scraping proxy and controller metrics) and the network fault controller share `control`. This is segmentation, not authorization; Toxiproxy's optional bearer token (`TOXIPROXY_AUTH_TOKEN`) is a possible later hardening step.

The network fault controller is a separate container running `lab.network_fault`. It uses the same `BoundedScenario` lifecycle as the CPU fault: validate first, record a start event only once the toxic is applied, hold until stop or deadline, and always revert in `finally`. Revert retries three times and records `cleanup_failed` if the toxic may still be present. The controller refuses to stack on an existing toxic. It clears leftovers both at startup (recording a `stale_toxics_removed` event) and on `stop` with nothing active. Operators reach it only through `docker compose exec` and a `0600` Unix socket; there is no HTTP control endpoint. Its metrics port exposes the ground-truth `lab_network_fault_active` marker for dashboards and evaluation only.

All containers drop capabilities, use non-root users and read-only root filesystems, and have memory/CPU/PID limits. Writable state lives in named volumes or a size-limited temporary filesystem. Source configuration mounts are read-only. Application request logs omit raw query strings and client addresses. Prometheus retention settings are 24 hours and 200 MB; its active head and write-ahead log can require additional space. Grafana state and scenario records persist until explicitly removed.

The implementation is a monitoring substrate. It does not establish cybersecurity detection accuracy, agent correctness, or safe autonomous operations. Those require later labeled experiments and measured evaluation.
