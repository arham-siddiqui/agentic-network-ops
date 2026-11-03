# Security model

This is a local development lab. The threat model covers what the lab and its read-only investigator could damage or leak on the owner's machine, and how the design limits that. It makes no production-security or intrusion-detection claims.

## Assets

| Asset | Why it matters |
| --- | --- |
| Host and Docker daemon | Control over containers is effectively control over the host. |
| Fault controls (CPU socket, network controller, proxy admin API) | They can degrade the lab on demand. |
| Telemetry and ground truth | Evaluation results are only meaningful if investigators never see labels. |
| Model API credentials | Billed access to a hosted model. |

## Adversaries and failure modes

1. **Untrusted text reaching a model**: alert labels, metric text, or future log lines containing instructions such as "run docker exec".
2. **Model misuse of tools**: a model, through error or injection, requesting actions or queries outside its purpose.
3. **A compromised or misbehaving lab container** trying to reach fault controls.
4. **Accidental leakage**: credentials in prompts or artifacts, or ground truth in investigator inputs.

## Controls

| Surface | Control |
| --- | --- |
| Containers | Non-root users, `cap_drop: ALL`, `no-new-privileges`, read-only root filesystems, size-limited tmpfs, CPU/memory/PID limits. No privileged mode, Docker socket mounts, or host networking. Configuration is mounted read-only. |
| Networks | Both Compose networks are `internal: true`. Published ports bind to `127.0.0.1` only. The proxy admin API binds to its `control` network alias. The API, worker, load generator, and smoke test are on `lab` only, and the smoke test checks from `lab` that the admin API and network controller are unreachable. |
| Fault controls | Reachable only through `docker compose exec` and `0600` Unix sockets, with no HTTP control endpoint. Parameters are validated and bounded (1–120 s; latency ≤ 1,500 ms). Fixed proxy, upstream, and toxic definitions; unknown request fields are rejected. One fault at a time; automatic expiry; cleanup in `finally`; stale-toxic reconcile. |
| Investigator data access | Host-side, loopback-only Prometheus. Only catalogue metric IDs exist, with server-defined PromQL; there is no method that accepts a query string. Bounded lookback, step, point counts, and HTTP timeouts. Missing data stays explicitly missing. |
| Investigator tools | Nine `get_*` tools, all reads over the already-captured bundle. No shell, Docker, restart, firewall, routing, or fault tools exist. Tool arguments are validated, and invalid or unknown calls return an error result. The budgets are 6 calls, 30 s, and 60,000 tokens. |
| Untrusted text | Alert names must match `[A-Za-z][A-Za-z0-9_]{0,63}`; anything else is dropped. Telemetry is wrapped in data tags, and the system prompt says it is data, not instructions. The output is a schema-checked JSON report whose `action_taken` is always `false`. The report is advisory, never an authorization decision. |
| Ground truth | Fault markers, the controller's job, toxic state, and scenario records are absent from the catalogue. Tests check that bundles, prompts, and tool definitions contain none of these tokens. Labels are written only to evaluator files. |
| Credentials | Resolved by the Anthropic SDK from the host environment (`ANTHROPIC_API_KEY` or an `ant auth login` profile). Never read, logged, or stored by project code. Run metadata excludes prompt text. `.env` and `artifacts/` are git-ignored. |

## Residual risk

- **Segmentation is not authentication.** Prometheus and the network controller share the `control` network with the proxy admin API, and anything able to join that network or exec into those containers can reach it. Toxiproxy's optional `TOXIPROXY_AUTH_TOKEN` would add authentication; it is not enabled.
- **Anyone with Docker access owns the lab.** The Unix sockets and `docker compose exec` boundaries assume a trusted local operator.
- **Prompt injection is mitigated, not eliminated.** A model might still be misled into a wrong diagnosis. It cannot act on that, because no tool changes anything, and wrong cited values are flagged against telemetry.
- **The Grafana password is a documented lab default**, not a secret.
- **Image tags are not digests.** Upstream could republish a tag.
- **Hosted model calls send telemetry summaries off the machine.** They contain lab metrics only: no addresses, user data, or credentials.
