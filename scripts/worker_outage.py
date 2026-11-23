"""Bounded worker outage: stop the worker container, then restart it after --duration seconds.

  python3 scripts/worker_outage.py --duration 60

Experiment machinery for operators and the evaluator, never an investigator tool. Prints the run
record as its first stdout line, appends start/end ground truth to artifacts/worker-outages.jsonl,
and always restarts the worker in `finally`, including on Ctrl-C or SIGTERM. Standard library only.
"""

import argparse
import json
import math
import signal
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORD_PATH = ROOT / "artifacts" / "worker-outages.jsonl"
MAX_DURATION = 120


class Interrupted(Exception):
    pass


def validate_duration(value: float) -> float:
    if not math.isfinite(value) or not 1 <= value <= MAX_DURATION:
        raise ValueError(f"duration must be between 1 and {MAX_DURATION} seconds")
    return float(value)


def compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True, timeout=60
    )


def record(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\n")


def run_outage(duration: float, run_compose=compose, record_path=RECORD_PATH, clock=time) -> str:
    seconds = validate_duration(duration)
    run = {
        "run_id": str(uuid.uuid4()),
        "scenario": "worker_unavailable",
        "started_at": datetime.now(UTC).isoformat(),
        "duration_seconds": seconds,
        "max_duration_seconds": MAX_DURATION,
        "method": "docker_compose_stop_start",
    }

    def interrupt(signum, frame):
        raise Interrupted

    previous = {s: signal.signal(s, interrupt) for s in (signal.SIGTERM, signal.SIGINT)}
    reason = "duration_elapsed"
    record(record_path, {**run, "event": "scenario_start"})
    print(json.dumps(run), flush=True)
    try:
        deadline = clock.monotonic() + seconds
        run_compose("stop", "worker")
        while clock.monotonic() < deadline:
            clock.sleep(min(1.0, max(0.0, deadline - clock.monotonic())))
    except Interrupted:
        reason = "stopped"
    except (OSError, subprocess.SubprocessError):
        reason = "error"
    finally:
        # Nothing may interrupt the restart.
        for s in previous:
            signal.signal(s, signal.SIG_IGN)
        try:
            run_compose("start", "worker")
        except (OSError, subprocess.SubprocessError):
            reason = "cleanup_failed"
        record(
            record_path,
            {
                **run,
                "event": "scenario_end",
                "reason": reason,
                "ended_at": datetime.now(UTC).isoformat(),
            },
        )
        for s, handler in previous.items():
            signal.signal(s, handler)
    return reason


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=60)
    args = parser.parse_args()
    try:
        reason = run_outage(args.duration)
    except ValueError as exc:
        parser.error(str(exc))
    if reason in ("error", "cleanup_failed"):
        raise SystemExit(f"worker outage ended with {reason}; check `docker compose ps worker`")


if __name__ == "__main__":
    main()
