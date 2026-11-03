"""CSV and Markdown summaries. Every rate is printed with its numerator and denominator."""

import csv
import shutil
from pathlib import Path

COLUMNS = (
    "mode",
    "n_trials",
    "n_scored",
    "n_failed_trials",
    "n_fault",
    "correct",
    "root_cause_accuracy",
    "abstained",
    "abstention_rate",
    "wrong_cause",
    "n_healthy",
    "false_incidents",
    "false_incident_rate",
    "healthy_abstained",
    "evidence_supported",
    "evidence_support_rate",
    "median_diagnosis_seconds",
    "input_tokens",
    "output_tokens",
    "estimated_cost_usd",
)


def _fmt(value, digits=2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def _ratio(numerator, denominator) -> str:
    return f"{numerator}/{denominator}" if denominator else "n/a"


def write_summary(run_dir: Path, scores: dict, infra: dict, manifest: dict, artifacts: Path):
    with (run_dir / "summary.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        for mode, result in scores.items():
            writer.writerow({"mode": mode, **{c: result.get(c) for c in COLUMNS[1:]}})

    source = manifest.get("source", {})
    lines = [
        f"# Evaluation summary: {run_dir.name}",
        "",
        f"Commit `{source.get('commit') or 'unknown'}`"
        f"{' (uncommitted changes)' if source.get('dirty') else ''}; "
        f"seed {manifest.get('seed', 'n/a')}; order: {', '.join(manifest.get('order', []))}.",
        "",
        "## Diagnosis by mode",
        "",
        "| Mode | Correct (fault windows) | Abstained | Wrong cause | Healthy misdiagnosed "
        "| Evidence supported | Failed trials | Median diagnosis s | Tokens in/out | Est. cost |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for mode, r in scores.items():
        cost = r["estimated_cost_usd"]
        lines.append(
            f"| {mode} | {_ratio(r['correct'], r['n_fault'])} | "
            f"{_ratio(r['abstained'], r['n_fault'])} | {_ratio(r['wrong_cause'], r['n_fault'])} | "
            f"{_ratio(r['false_incidents'], r['n_healthy'])} | "
            f"{_ratio(r['evidence_supported'], r['n_scored'])} | {r['n_failed_trials']} | "
            f"{_fmt(r['median_diagnosis_seconds'], 3)} | "
            f"{r['input_tokens']}/{r['output_tokens']} | "
            f"{'n/a' if cost is None else f'${cost:.4f}'} |"
        )
    lines += ["", "## Predictions by true label", ""]
    for mode, r in scores.items():
        lines.append(f"- **{mode}**")
        for label, entry in sorted(r["per_class"].items()):
            predicted = ", ".join(f"{k}: {v}" for k, v in sorted(entry["predicted"].items()))
            lines.append(f"  - {label}: {entry['correct']}/{entry['n']} correct ({predicted})")
    lines += [
        "",
        "## Infrastructure observations (independent of any investigator)",
        "",
        "| Scenario | Trials | Captured | Expected alert seen by decision time "
        "| Median detection s | Median recovery s |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, e in sorted(infra.items()):
        lines.append(
            f"| {name} | {e['n']} | {e['captured']} | {e['alert_fired']} | "
            f"{_fmt(e['median_detection_s'], 1)} | {_fmt(e['median_recovery_s'], 1)} |"
        )
    lines += [
        "",
        "## Caveats",
        "",
        "- Small counts are smoke-level evidence, not statistically robust accuracy claims.",
        "- Detection times come from alert first-seen samples at the bundle step (5 s), "
        "relative to ground-truth start; recovery times are 2 s polling observations.",
        "- Diagnosis seconds are measured on saved bundles; they exclude capture and waiting.",
        "- LLM confidence values are uncalibrated. Token costs are estimates from list prices.",
        "- The investigator is read-only; recovery is the fault expiring, not a repair.",
        "",
    ]
    (run_dir / "summary.md").write_text("\n".join(lines))
    artifacts.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(run_dir / "summary.csv", artifacts / "evaluation-summary.csv")
    shutil.copyfile(run_dir / "summary.md", artifacts / "evaluation-summary.md")
