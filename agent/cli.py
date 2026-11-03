"""Read-only incident investigation for the local lab.

  python -m agent.cli investigate --lookback 120      # capture now, then diagnose
  python -m agent.cli capture --lookback 120          # save an evidence bundle only
  python -m agent.cli diagnose BUNDLE --mode rules    # re-run a mode on a saved bundle

Nothing here can change Docker, the proxy, or the services; output goes to artifacts/.
"""

import argparse
import json
import os
from pathlib import Path

from agent.rules_baseline import diagnose
from agent.schemas import EvidenceBundle, IncidentReport
from agent.telemetry import MAX_LOOKBACK, MIN_LOOKBACK, PrometheusSource, capture

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "artifacts" / "investigations"


def run_mode(bundle: EvidenceBundle, mode: str) -> IncidentReport:
    if mode == "rules":
        return diagnose(bundle)
    raise SystemExit(f"mode {mode!r} is not available yet")


def save(out: Path, name: str, model) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    path.write_text(model.model_dump_json(indent=2) + "\n")
    return path


def show(report: IncidentReport, path: Path) -> None:
    print(f"[{report.mode}] {report.predicted_cause}: {report.summary}")
    print(f"  impact: {report.business_impact}")
    print(f"  recommended: {report.recommended_action}")
    for item in report.evidence:
        value = "unknown" if item.observed_value is None else f"{item.observed_value:.4g}"
        print(f"  evidence: {item.metric_id} {item.statistic}={value} {item.units}")
    for text in report.limitations:
        print(f"  limitation: {text}")
    print(f"  report: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("investigate", "capture"):
        cmd = sub.add_parser(name)
        cmd.add_argument(
            "--lookback", type=int, default=120, help=f"{MIN_LOOKBACK}-{MAX_LOOKBACK} s"
        )
        cmd.add_argument(
            "--prometheus-url",
            default=f"http://127.0.0.1:{os.getenv('PROMETHEUS_PORT', '9090')}",
        )
        cmd.add_argument("--out", type=Path, default=DEFAULT_OUT)
        if name == "investigate":
            cmd.add_argument("--mode", choices=["rules"], default="rules")
    cmd = sub.add_parser("diagnose")
    cmd.add_argument("bundle", type=Path)
    cmd.add_argument("--mode", choices=["rules"], default="rules")
    cmd.add_argument("--out", type=Path)
    args = parser.parse_args()

    if args.command == "diagnose":
        bundle = EvidenceBundle.model_validate(json.loads(args.bundle.read_text()))
        out = args.out or args.bundle.parent
    else:
        try:
            source = PrometheusSource(args.prometheus_url)
            bundle = capture(source, lookback_seconds=args.lookback)
        except ValueError as exc:
            parser.error(str(exc))
        out = args.out
        path = save(out, f"{bundle.incident_id}-bundle.json", bundle)
        print(f"evidence bundle: {path}")
        if args.command == "capture":
            return
    report = run_mode(bundle, args.mode)
    show(report, save(out, f"{bundle.incident_id}-report-{report.mode}.json", report))


if __name__ == "__main__":
    main()
