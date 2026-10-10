# Roadmap

1. **Validate the monitoring lab.** Repeat the CPU experiment on Docker at least three times, preserve healthy/fault/recovery observations, investigate missed or false alerts, and document platform resource settings. Add further faults only after this baseline is dependable.
2. **Read-only diagnosis.** Add structured, bounded telemetry/log queries and evidence-grounded incident reports. Compare a deterministic diagnosis baseline, a summarized LLM snapshot, and an investigator using read-only tools. Include healthy cases, incorrect diagnoses, abstention, time, and cost.
3. **Defensive security experiments.** Add synthetic local authentication failures and benign traffic sequences, with separate labels and measured detection precision/recall. Keep all targets in the owned lab.
4. **Constrained remediation research.** Only after diagnosis is evaluated, study explicit policy checks, human approvals, narrowly scoped actions, rollback, and recovery verification within the sandbox.

Stage 1 includes no LLM API integration, security attack simulator, routing protocol implementation, cloud deployment, or automatic remediation.
