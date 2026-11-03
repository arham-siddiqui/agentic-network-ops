# Experimental method

The evaluation asks one question: given the same unlabeled telemetry, how often does each investigator name the injected cause, how often does it abstain, and what does each answer cost? It does **not** measure whether an LLM detects incidents faster than Prometheus. Every mode starts from the same captured evidence, so detection time is an infrastructure metric and is reported separately.

## Trial protocol

`python -m evaluation.run trials --reps N` (or `make evaluate`) runs each selected scenario `N` times in a seeded random order (default seed `20261103`, recorded in the manifest). Each trial:

1. Requires an idle lab: no CPU or network fault active, both application targets up, no alert firing.
2. Starts bounded load: 10 requests/second, at most 4 in flight, for 180 seconds.
3. Waits 30 seconds of baseline.
4. Injects the scenario's fault through the normal `docker compose exec` control channel. Healthy controls skip this step.
5. Waits 40 seconds, the **decision delay**, which is the same for every scenario including healthy.
6. Captures an evidence bundle covering the previous 120 seconds at a 5-second step through the investigator's allowlisted, read-only source, and saves it under `bundles/`.
7. Waits for the fault to expire, collects its ground-truth records, and measures recovery: the scenario's signal below its alert threshold, both targets up, and no alert firing.
8. Always stops the fault and the load container in `finally`. It writes `ground_truth/<id>.json` even if the trial failed.

| Scenario | Label | Fault | Expected alert |
| --- | --- | --- | --- |
| `healthy` | `healthy` | none | none |
| `cpu` | `cpu_pressure` | worker CPU thread, 60 s | `WorkerCpuPressure` |
| `latency` | `network_latency` | proxy downstream latency 600 ms, 60 s | `ApiHighLatency` |
| `latency_low` (opt-in) | `network_latency` | proxy downstream latency 300 ms, 60 s | `ApiHighLatency` |
| `timeout` | `network_connectivity_or_timeout` | proxy drops API→worker bytes, 60 s | `WorkerCallTimeouts` |

`service_unavailable` exists as a diagnosis but has no automated trial yet. Stopping a container is a different kind of lab action than the bounded fault controllers, and it is left for a later milestone.

## Keeping ground truth away from the investigators

- Bundles contain only the 21 catalogue series and firing alert names. Their file names are opaque random IDs. Tests check that no label, scenario name, run ID, fault marker, or toxic name appears in a bundle.
- Labels, parameters, scenario JSONL records, and timings are written only to `ground_truth/`.
- `diagnose_bundles` receives only the bundle directory. The rules and every LLM mode read the same saved bundles, so they see identical evidence.
- A prediction is written once and never rewritten. Scoring joins predictions with labels only afterwards.
- Failed trials stay in the results: they count in `n_trials` and `n_failed_trials` for every mode.

## Metrics

| Metric | Definition |
| --- | --- |
| Correct (fault windows) | Predicted cause equals the label, over captured fault trials. Reported as `correct/n_fault`. |
| Abstained | `unknown_or_ambiguous` on a fault trial, reported separately and never counted as correct. |
| Wrong cause | A different modeled cause on a fault trial. |
| Healthy misdiagnosed | A fault cause predicted on a healthy control. Abstaining on a healthy control is counted separately (`healthy_abstained`). |
| Evidence supported | The report cites at least one metric, and every cited value matches the bundle (5% relative or 0.005 absolute tolerance, units must match). In `llm_tools` mode, every cited metric must also have been retrieved through a tool. |
| Median diagnosis seconds | Time to produce a schema-valid report from a saved bundle. It excludes capture and the decision delay. |
| Tokens / estimated cost | Summed from API usage for LLM modes, priced from the list prices in `agent/llm_provider.py`. These are estimates. |
| Detection seconds | First ALERTS sample of the expected alert minus the ground-truth start. The 5-second bundle step limits its resolution, and it only counts if the alert fired before the decision time. |
| Recovery seconds | From observed fault expiry until the recovery condition holds, polled every 2 seconds. The investigator repairs nothing, so recovery is never credited to it. |

## Outputs

`artifacts/evaluation/<UTC timestamp>/` (ignored by Git) holds `manifest.json`, `bundles/`, `ground_truth/`, `predictions/<mode>/`, `records.jsonl`, `summary.csv`, and `summary.md`. The latest summary is also copied to `artifacts/evaluation-summary.{csv,md}`. The manifest records the commit, whether the working tree was dirty, Docker and Compose versions, Docker CPU and memory, image tags, per-service limits, load settings, timings, seed, and trial order.

LLM modes can be added later on the same bundles without re-running trials:

```sh
uv sync --frozen --extra llm
make evaluate-score RUN=artifacts/evaluation/<timestamp> MODES=llm_snapshot,llm_tools
```

## Limits of the design

- Three repetitions per class is smoke-level evidence. Report counts with denominators, never a bare percentage.
- The rules use the same thresholds as the alert rules. On scenarios built to cross those thresholds they are expected to do well, so the interesting results are the abstentions, the overlaps, and the LLM modes' behavior on the same data.
- CPU pressure can delay requests before the worker's handler timer starts, which can look like network delay. The rules report this overlap as a limitation rather than resolving it.
- One fixed decision delay (40 seconds) is used. Earlier decisions, before alerts fire, and gradual onsets are not yet sampled.
- The proxy models transport-level delay and black-holing, not packet loss, retransmission, or routing.
