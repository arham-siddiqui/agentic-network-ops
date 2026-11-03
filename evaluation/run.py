"""Labelled trials, diagnosis on saved bundles, and scoring.

  python -m evaluation.run trials --reps 3              # run trials, diagnose with rules, score
  python -m evaluation.run diagnose RUN_DIR --modes llm_snapshot,llm_tools
  python -m evaluation.run score RUN_DIR

Layout of RUN_DIR (under ignored artifacts/evaluation/):
  bundles/<id>.json         agent-visible evidence only (opaque IDs, no labels)
  ground_truth/<id>.json    labels, parameters, scenario records, timings (evaluator only)
  predictions/<mode>/<id>.json and <id>.run.json
  manifest.json, records.jsonl, summary.csv, summary.md

Run it alone against an otherwise idle lab; concurrent experiments invalidate trials.
"""

import argparse
import json
import os
import random
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent.investigator import investigate_snapshot, investigate_with_tools
from agent.llm_provider import DEFAULT_MODEL, AnthropicProvider, ProviderError
from agent.reporting import check_evidence
from agent.rules_baseline import diagnose as rules_diagnose
from agent.schemas import EvidenceBundle, IncidentReport
from evaluation.scenarios import DEFAULT_SCENARIOS, SCENARIOS
from evaluation.scoring import infrastructure, score
from evaluation.summarize import write_summary

ROOT = Path(__file__).resolve().parents[1]
MODES = ("rules", "llm_snapshot", "llm_tools")


@dataclass(frozen=True)
class Settings:
    baseline_seconds: int = 30
    decision_delay_seconds: int = 40  # after injection; same offset for healthy controls
    lookback_seconds: int = 120
    load_seconds: int = 180
    cooldown_seconds: int = 15
    expiry_timeout_seconds: int = 90
    recovery_timeout_seconds: int = 60


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        data.model_dump_json(indent=2)
        if hasattr(data, "model_dump_json")
        else json.dumps(data, indent=2)
    )
    path.write_text(text + "\n")


def _wait(lab, check, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        lab.sleep(2)


def detection_delay(bundle: EvidenceBundle, scenario, fault: dict | None) -> float | None:
    """Observed first firing of the expected alert minus ground-truth start (step-limited)."""
    if scenario.alert is None or fault is None:
        return None
    started = datetime.fromisoformat(fault["started_at"]).timestamp()
    seen = [a.first_seen for a in bundle.alerts if a.alertname == scenario.alert]
    seen = [t for t in seen if t >= started]
    return min(seen) - started if seen else None


def run_trial(lab, scenario, run_dir: Path, settings: Settings) -> dict:
    incident_id = uuid.uuid4().hex[:16]
    trial = {
        "incident_id": incident_id,
        "scenario": scenario.name,
        "label": scenario.label,
        "params": scenario.params,
        "status": "failed",
        "started_at": _now(),
    }
    load, owns_fault = None, False
    try:
        if not lab.idle():
            raise RuntimeError("lab is not idle (fault active, target down, or alert firing)")
        load = lab.start_load(settings.load_seconds)
        lab.sleep(settings.baseline_seconds)
        if scenario.control is not None:
            trial["fault"] = lab.inject(scenario)
            owns_fault = True
        lab.sleep(settings.decision_delay_seconds)
        bundle = lab.capture(settings.lookback_seconds, incident_id)
        _write(run_dir / "bundles" / f"{incident_id}.json", bundle)
        trial.update(
            status="captured",
            captured_at=bundle.window.end.isoformat(),
            alerts_seen=sorted({a.alertname for a in bundle.alerts}),
            alert_detection_delay_s=detection_delay(bundle, scenario, trial.get("fault")),
        )
        if owns_fault:
            if not _wait(lab, lambda: not lab.active(scenario), settings.expiry_timeout_seconds):
                raise RuntimeError("fault did not expire on schedule")
            trial["ground_truth_events"] = lab.events(scenario, trial["fault"]["run_id"])
        ended = time.monotonic()
        recovered = _wait(lab, lambda: lab.recovered(scenario), settings.recovery_timeout_seconds)
        trial["recovery_s"] = time.monotonic() - ended if recovered else None
        trial["recovered"] = recovered
    except Exception as exc:  # preserve the failed trial; never drop it from the results
        trial["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if owns_fault:
            try:
                lab.stop(scenario)
            except Exception as exc:
                trial.setdefault("cleanup_errors", []).append(str(exc))
        if load is not None:
            try:
                lab.stop_load(load)
            except Exception as exc:
                trial.setdefault("cleanup_errors", []).append(str(exc))
        trial["ended_at"] = _now()
        _write(run_dir / "ground_truth" / f"{incident_id}.json", trial)
    return trial


def _git() -> dict:
    def git(*args):
        try:
            return subprocess.run(
                ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10, check=True
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def run_trials(lab, names, reps: int, seed: int, run_dir: Path, settings: Settings) -> list:
    order = [SCENARIOS[name] for name in names for _ in range(reps)]
    random.Random(seed).shuffle(order)
    _write(
        run_dir / "manifest.json",
        {
            "created_at": _now(),
            "source": _git(),
            "environment": lab.environment(),
            "settings": asdict(settings),
            "load": {"rate_per_second": 10, "concurrency": 4, "seconds": settings.load_seconds},
            "seed": seed,
            "order": [s.name for s in order],
            "scenarios": {s.name: asdict(s) for s in order},
        },
    )
    trials = []
    for index, scenario in enumerate(order, 1):
        print(f"[{index}/{len(order)}] {scenario.name}", flush=True)
        trials.append(run_trial(lab, scenario, run_dir, settings))
        print(f"    {trials[-1]['status']} {trials[-1].get('error', '')}".rstrip(), flush=True)
        _wait(lab, lab.idle, settings.recovery_timeout_seconds)
        lab.sleep(settings.cooldown_seconds)
    return trials


def diagnose_bundles(bundle_dir: Path, out_dir: Path, modes, model=DEFAULT_MODEL, provider=None):
    """Reads only agent-visible bundles; ground truth is not available to this function."""
    for path in sorted(bundle_dir.glob("*.json")):
        bundle = EvidenceBundle.model_validate_json(path.read_text())
        for mode in modes:
            target = out_dir / mode / path.name
            if target.exists():
                continue  # predictions are written once and never rewritten
            started = time.monotonic()
            if mode == "rules":
                report, info = rules_diagnose(bundle), {}
                info["latency_seconds"] = time.monotonic() - started
            else:
                if provider is None:
                    provider = AnthropicProvider(model)
                run = investigate_snapshot if mode == "llm_snapshot" else investigate_with_tools
                report, info = run(bundle, provider)
            _write(target, report)
            _write(target.with_suffix(".run.json"), info)


def records(run_dir: Path) -> list[dict]:
    rows = []
    mode_dirs = sorted(p for p in (run_dir / "predictions").glob("*") if p.is_dir())
    for path in sorted((run_dir / "ground_truth").glob("*.json")):
        trial = json.loads(path.read_text())
        bundle_path = run_dir / "bundles" / path.name
        bundle = (
            EvidenceBundle.model_validate_json(bundle_path.read_text())
            if bundle_path.exists()
            else None
        )
        for mode_dir in mode_dirs:
            row = {
                "incident_id": trial["incident_id"],
                "scenario": trial["scenario"],
                "label": trial["label"],
                "status": trial["status"],
                "mode": mode_dir.name,
                "predicted": None,
                "evidence_count": 0,
                "citation_problems": [],
                "latency_s": None,
            }
            prediction = mode_dir / path.name
            if bundle is not None and prediction.exists():
                report = IncidentReport.model_validate_json(prediction.read_text())
                info = json.loads(prediction.with_suffix(".run.json").read_text())
                problems = set(check_evidence(report, bundle))
                problems |= set(info.get("citation_problems", []))
                row.update(
                    predicted=report.predicted_cause,
                    evidence_count=len(report.evidence),
                    citation_problems=sorted(problems),
                    latency_s=info.get("latency_seconds"),
                    input_tokens=info.get("input_tokens"),
                    output_tokens=info.get("output_tokens"),
                    cost_usd=info.get("estimated_cost_usd"),
                    llm_outcome=info.get("outcome"),
                )
            rows.append(row)
    return rows


def score_run(run_dir: Path) -> dict:
    rows = records(run_dir)
    with (run_dir / "records.jsonl").open("w") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    trials = [json.loads(p.read_text()) for p in sorted((run_dir / "ground_truth").glob("*.json"))]
    scores, infra = score(rows), infrastructure(trials)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    write_summary(run_dir, scores, infra, manifest, ROOT / "artifacts")
    return scores


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("trials")
    cmd.add_argument("--scenarios", default=",".join(DEFAULT_SCENARIOS))
    cmd.add_argument("--reps", type=int, choices=range(1, 11), default=3)
    cmd.add_argument("--seed", type=int, default=20261103)
    cmd.add_argument("--modes", default="rules")
    cmd.add_argument("--model", default=DEFAULT_MODEL)
    cmd.add_argument(
        "--prometheus-url", default=f"http://127.0.0.1:{os.getenv('PROMETHEUS_PORT', '9090')}"
    )
    cmd = sub.add_parser("diagnose")
    cmd.add_argument("run_dir", type=Path)
    cmd.add_argument("--modes", required=True)
    cmd.add_argument("--model", default=DEFAULT_MODEL)
    cmd = sub.add_parser("score")
    cmd.add_argument("run_dir", type=Path)
    args = parser.parse_args()

    if args.command in ("trials", "diagnose"):
        modes = args.modes.split(",")
        if unknown := set(modes) - set(MODES):
            parser.error(f"unknown modes: {sorted(unknown)}")
    if args.command == "trials":
        from evaluation.lab import ComposeLab

        names = args.scenarios.split(",")
        if unknown := set(names) - set(SCENARIOS):
            parser.error(f"unknown scenarios: {sorted(unknown)}")
        run_dir = ROOT / "artifacts" / "evaluation" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_trials(
            ComposeLab(args.prometheus_url), names, args.reps, args.seed, run_dir, Settings()
        )
    else:
        run_dir = args.run_dir
    if args.command in ("trials", "diagnose"):
        try:
            diagnose_bundles(run_dir / "bundles", run_dir / "predictions", modes, args.model)
        except ProviderError as exc:
            raise SystemExit(f"LLM mode unavailable: {exc}") from exc
    scores = score_run(run_dir)
    for mode, result in scores.items():
        print(
            f"{mode}: {result['correct']}/{result['n_fault']} fault windows correct, "
            f"{result['abstained']} abstained, {result['false_incidents']}/{result['n_healthy']} "
            f"healthy windows misdiagnosed, {result['n_failed_trials']} failed trials"
        )
    print(f"summary: {run_dir / 'summary.md'}")


if __name__ == "__main__":
    main()
