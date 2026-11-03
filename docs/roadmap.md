# Roadmap

## Status (2026-11-03)

| Milestone | Code | Runtime evidence |
| --- | --- | --- |
| 1. Monitoring lab + CPU experiment | Done (Stage 1) | **Pending**: no Docker run yet |
| Network-path impairment (latency, timeout) | Done | **Pending**: no Docker run yet; promtool fixtures not yet executed |
| 2. Read-only diagnosis: rules, LLM snapshot, LLM tools | Done, tested offline | Rules: none on live data. LLM modes: scripted provider only, no real-model calls |
| Labelled evaluation pipeline | Done, tested against a simulated lab | **No results yet** |
| 3. Defensive security experiments | Not started | — |
| 4. Constrained remediation | Not started | — |


1. **Validate the monitoring lab.** Repeat the CPU experiment on Docker at least three times, preserve healthy/fault/recovery observations, investigate missed or false alerts, and document platform resource settings. Add further faults only after this baseline is dependable.
2. **Read-only diagnosis.** Add structured, bounded telemetry/log queries and evidence-grounded incident reports. Compare a deterministic diagnosis baseline, a summarized LLM snapshot, and an investigator using read-only tools. Include healthy cases, incorrect diagnoses, abstention, time, and cost.
3. **Defensive security experiments.** Add synthetic local authentication failures and benign traffic sequences, with separate labels and measured detection precision/recall. Keep all targets in the owned lab.
4. **Constrained remediation research.** Only after diagnosis is evaluated, study explicit policy checks, human approvals, narrowly scoped actions, rollback, and recovery verification within the sandbox.

The lab includes no security attack simulator, routing protocol implementation, packet-level impairment, cloud deployment, or automatic remediation. LLM integration is optional, read-only, and advisory.
