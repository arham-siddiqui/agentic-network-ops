# Roadmap

## Status (2026-11-21)

| Milestone | Code | Runtime evidence |
| --- | --- | --- |
| 1. Monitoring lab + CPU experiment | Done (Stage 1) | First Docker run passed (1 run); two startup bugs fixed. Three repetitions still pending |
| Network-path impairment (latency, timeout) | Done | Both passed on the first Docker run (1 run each); promtool fixtures pass |
| 2. Read-only diagnosis: tools interface, rules baseline, report schema 1.1 | Done | Tools checked against the live lab, including during a worker outage; rules correct on the first real evaluation |
| Experimental LLM modes | Moved to the `llm-modes` branch | Scripted provider only; no real-model calls |
| Next: LLM agent on `LabTools` | Not started | Needs model access (API key or a local model); scored with the existing evaluator |
| Labelled evaluation pipeline (incl. worker outage) | Done | 15 valid real trials: rules 12/12 faults, 0/3 healthy misdiagnosed (2 sleep-affected trials excluded and re-run) |
| 3. Defensive security experiments | Not started | — |
| 4. Constrained remediation | Not started | — |


1. **Validate the monitoring lab.** Repeat the CPU experiment on Docker at least three times, preserve healthy/fault/recovery observations, investigate missed or false alerts, and document platform resource settings. Add further faults only after this baseline is dependable.
2. **Read-only diagnosis.** Add structured, bounded telemetry/log queries and evidence-grounded incident reports. Compare a deterministic diagnosis baseline, a summarized LLM snapshot, and an investigator using read-only tools. Include healthy cases, incorrect diagnoses, abstention, time, and cost.
3. **Defensive security experiments.** Add synthetic local authentication failures and benign traffic sequences, with separate labels and measured detection precision/recall. Keep all targets in the owned lab.
4. **Constrained remediation research.** Only after diagnosis is evaluated, study explicit policy checks, human approvals, narrowly scoped actions, rollback, and recovery verification within the sandbox.

The lab includes no security attack simulator, routing protocol implementation, packet-level impairment, cloud deployment, or automatic remediation. LLM integration is optional, read-only, and advisory.
