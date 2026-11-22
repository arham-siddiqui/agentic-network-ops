"""Export a real evaluation run for the demo site's Recorded mode.

  python -m evaluation.export_site artifacts/evaluation/<timestamp> [--name NAME]

Writes site/data/recorded/<name>.json: each captured incident's evidence bundle, its ground
truth (for the replay's fault band), and the saved rules report. Only measured data is
exported; failed trials are listed by scenario and error so they are not silently dropped.
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def export(run_dir: Path) -> dict:
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    incidents, failed = [], []
    for path in sorted((run_dir / "ground_truth").glob("*.json")):
        trial = json.loads(path.read_text())
        bundle_path = run_dir / "bundles" / path.name
        if trial["status"] != "captured" or not bundle_path.exists():
            failed.append({"scenario": trial["scenario"], "error": trial.get("error")})
            continue
        fault = trial.get("fault") or {}
        end = next(
            (e for e in trial.get("ground_truth_events", []) if e.get("event") == "scenario_end"),
            {},
        )
        reports = {}
        for mode_dir in sorted((run_dir / "predictions").glob("*")):
            report_path = mode_dir / path.name
            if report_path.exists():
                reports[mode_dir.name] = json.loads(report_path.read_text())
        incidents.append(
            {
                "scenario": trial["scenario"],
                "label": trial["label"],
                "ground_truth": {
                    "started_at": fault.get("started_at"),
                    "ended_at": end.get("ended_at"),
                    "params": trial.get("params", {}),
                },
                "alert_detection_delay_s": trial.get("alert_detection_delay_s"),
                "recovery_s": trial.get("recovery_s"),
                "bundle": json.loads(bundle_path.read_text()),
                "reports": reports,
            }
        )
    return {
        "source": "recorded",
        "run": run_dir.name,
        "captured_on": manifest.get("created_at"),
        "lab": {
            "commit": manifest.get("source", {}).get("commit"),
            "environment": manifest.get("environment", {}),
        },
        "incidents": incidents,
        "failed_trials": failed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--name")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "site" / "data" / "recorded")
    args = parser.parse_args()
    data = export(args.run_dir)
    if not data["incidents"]:
        parser.error("no captured incidents to export")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"{args.name or args.run_dir.name}.json"
    out.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    print(f"{len(data['incidents'])} incidents, {len(data['failed_trials'])} failed -> {out}")


if __name__ == "__main__":
    main()
