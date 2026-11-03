# Demo walkthrough (2–3 minutes)

A repeatable script for showing the lab end to end. Every number shown comes from the running lab or from saved artifacts; nothing is pre-filled. Before showing anything, run it once on the presenting machine and check [validation notes](validation.md) for what has actually been verified there.

## Before the demo

```sh
make up && make smoke            # stack healthy; fault-control isolation checked
make check-rules                 # alert fixtures pass
open http://127.0.0.1:3000/d/local-lab
```

Have three terminals ready: load, fault control, and investigator.

## Script

1. **Architecture (15–20 s).** `load → api → impairment proxy → worker`, with Prometheus and Grafana. Everything is local, non-root, with dropped capabilities and CPU/memory limits. Fault controls sit on a separate control network and the investigator cannot reach them.
2. **Healthy baseline (30 s).** Run `make load`. In Grafana, show both targets up, request rate around 10/s, low p95, and worker CPU well under its 0.5-core quota.
3. **Inject a network fault (30–45 s).** Run `make inject-network-fault` (600 ms for 60 s). The proxy is changing real traffic, not adding a sleep in the application. Show API p95 rising and the "slower than 250 ms" panel: the API is slow while the worker stays fast. `ApiHighLatency` fires if the threshold is crossed for 10 s. The ground-truth marker is shown, but nothing detects with it.
4. **Investigate (30–45 s).** While the fault is active, run `make investigate`. Show the predicted cause, the cited evidence (metric, statistic, value), user-facing impact, the advisory recommendation, and `action_taken: false`. If LLM modes are configured, run `diagnose <bundle> --mode llm_snapshot` on **the same bundle** and compare.
5. **Recovery and results (20–30 s).** The fault expires: latency normalizes, the alert clears, and `make network-fault-status` shows no toxics. Open `artifacts/evaluation-summary.md` from a real `make evaluate` run and read the counts with their denominators.
6. **Trade-offs (20 s).** The investigator is read-only on purpose. The evaluation measures when it diagnoses correctly and when it abstains. Possible next steps: deeper network telemetry (packet-level impairment), synthetic security scenarios, or human-approved remediation with rollback.

## If something is not ready

- **No LLM credentials:** show the deterministic report and say the LLM modes are implemented and tested against a scripted provider, with no real-model results yet.
- **Alert did not fire:** say so and show the measured signal. Do not lower thresholds live.
- **Stale toxic after an interrupted run:** run `make stop-network-fault`, or `docker compose restart impairment-proxy`.
