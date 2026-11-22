"""Provider-independent, read-only investigation tools over the live lab.

  get_service_metrics(service, time_window)       metric summaries from the Prometheus catalogue
  get_service_logs(service, time_window, limit)   bounded, sanitized structured request logs
  get_active_alerts()                             firing and pending Prometheus alerts

Each returns a JSON-serializable dict, so any future agent framework can wrap them unchanged.
Inputs are validated against fixed services, windows, and limits. Every source is read-only.
Failures come back as explicit statuses, never as empty results that look healthy. Fault
controls, fault markers, and scenario records are unreachable from here: the metric catalogue
excludes them and the log reader keeps only allowlisted request and lifecycle events.

  python -m agent.tools metrics worker --window 300
  python -m agent.tools logs api --window 120 --limit 20
  python -m agent.tools alerts
"""

import argparse
import json
import math
import os
import re
import statistics
import subprocess
from collections import Counter, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.catalog import METRICS
from agent.telemetry import PrometheusSource, PrometheusUnavailable, capture, summarize

ROOT = Path(__file__).resolve().parents[1]
MIN_WINDOW, MAX_WINDOW = 60, 900
METRIC_SERVICES = {
    "api": tuple(m for m in METRICS if m.startswith("api.")),
    "worker": tuple(m for m in METRICS if m.startswith("worker.")),
    "proxy": ("proxy.up",),
}
LOG_SERVICES = ("api", "worker")
DEFAULT_LOG_LIMIT, MAX_LOG_LIMIT = 50, 200
MAX_ERROR_RECORDS, MAX_LIFECYCLE, MAX_ALERTS = 20, 20, 50
MAX_LINES_KEPT, MAX_BYTES_READ = 5000, 16 * 1024 * 1024
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "OTHER"}
ROUTE = re.compile(r"(/[A-Za-z0-9_{}/-]{0,63}|unmatched)")
LABEL_VALUE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
ALERT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
# Fixed Uvicorn lifecycle phrases mapped to fixed event names; raw text is never returned.
LIFECYCLE = {
    "Started server process": "process_started",
    "Application startup complete": "startup_complete",
    "Shutting down": "shutting_down",
    "Application shutdown complete": "shutdown_complete",
    "Finished server process": "process_finished",
}


class ToolInputError(ValueError):
    pass


class LogsUnavailable(RuntimeError):
    pass


def _choice(name: str, value: object, allowed) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ToolInputError(f"{name} must be one of: {', '.join(allowed)}")
    return value


def _bounded_int(name: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolInputError(f"{name} must be a whole number between {low} and {high}")
    return value


class DockerLogSource:
    """Container stdout via `docker compose logs`, with fixed arguments and bounded reads."""

    def read(self, service: str, since: datetime, until: datetime) -> tuple[list[str], bool]:
        command = [
            *("docker", "compose", "logs", "--no-color", "--no-log-prefix", "--timestamps"),
            *("--since", since.strftime("%Y-%m-%dT%H:%M:%SZ")),
            *("--until", until.strftime("%Y-%m-%dT%H:%M:%SZ")),
            service,
        ]
        lines: deque[str] = deque(maxlen=MAX_LINES_KEPT)
        total, truncated = 0, False
        try:
            with subprocess.Popen(
                command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            ) as process:
                for line in process.stdout:
                    total += len(line)
                    if total > MAX_BYTES_READ:
                        truncated = True
                        process.kill()
                        break
                    lines.append(line.rstrip("\n"))
                process.wait(timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            raise LogsUnavailable(f"log read failed: {type(exc).__name__}") from exc
        if process.returncode not in (0, None) and not truncated and not lines:
            raise LogsUnavailable(f"docker compose logs exited with {process.returncode}")
        return list(lines), truncated or len(lines) == MAX_LINES_KEPT


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


def parse_log_lines(lines: list[str], service: str):
    """Keep only well-formed request records and fixed lifecycle events; count the rest."""
    records, lifecycle, dropped = [], [], 0
    for line in lines:
        stamp, _, message = line.partition(" ")
        try:
            event = json.loads(message)
        except ValueError:
            event = None
        if isinstance(event, dict):
            record = _request_record(event, service)
            if record is not None:
                records.append(record)
                continue
        else:
            name = next((v for k, v in LIFECYCLE.items() if k in message), None)
            if name is not None and stamp[:4].isdigit():
                lifecycle.append({"timestamp": stamp[:30], "event": name})
                continue
        dropped += 1  # includes scenario records, tracebacks, and anything unexpected
    return records, lifecycle, dropped


def _request_record(event: dict, service: str) -> dict | None:
    if event.get("event") != "http_request" or event.get("service") != service:
        return None
    method, route, status = event.get("method"), event.get("route"), event.get("status")
    timestamp, duration = event.get("timestamp"), event.get("duration_seconds")
    valid = (
        method in METHODS
        and isinstance(route, str)
        and ROUTE.fullmatch(route)
        and isinstance(status, int)
        and not isinstance(status, bool)
        and 100 <= status <= 599
        and isinstance(timestamp, (int, float))
        and math.isfinite(timestamp)
        and isinstance(duration, (int, float))
        and math.isfinite(duration)
        and 0 <= duration <= 3600
    )
    if not valid:
        return None
    return {
        "timestamp": _iso(timestamp),
        "method": method,
        "route": route,
        "status": status,
        "duration_seconds": round(float(duration), 6),
    }


def _metric_summary(bundle, metric_id: str) -> dict:
    s = summarize(bundle, metric_id)
    return {
        "unit": s.unit,
        "status": s.status,
        "recent_mean": s.recent_mean,
        "recent_max": s.recent_max,
        "window_min": s.window_min,
        "window_max": s.window_max,
        "last": s.last,
        "samples": s.samples,
    }


class LabTools:
    def __init__(self, prometheus: PrometheusSource | None = None, logs=None, now=None) -> None:
        self.prometheus = prometheus or PrometheusSource(
            f"http://127.0.0.1:{os.getenv('PROMETHEUS_PORT', '9090')}"
        )
        self.logs = logs or DockerLogSource()
        self.now = now or (lambda: datetime.now(UTC))

    def get_service_metrics(self, service: str, time_window: int = 300) -> dict:
        service = _choice("service", service, tuple(METRIC_SERVICES))
        window = _bounded_int("time_window", time_window, MIN_WINDOW, MAX_WINDOW)
        bundle = capture(self.prometheus, self.now(), window, metric_ids=METRIC_SERVICES[service])
        metrics = {m: _metric_summary(bundle, m) for m in METRIC_SERVICES[service]}
        statuses = {m["status"] for m in metrics.values()}
        status = (
            "unavailable"
            if statuses == {"unavailable"}
            else "ok"
            if statuses == {"ok"}
            else "partial"
        )
        return {
            "service": service,
            "time_window_seconds": window,
            "window": {
                "start": bundle.window.start.isoformat(),
                "end": bundle.window.end.isoformat(),
            },
            "status": status,
            "metrics": metrics,
        }

    def get_service_logs(
        self, service: str, time_window: int = 300, limit: int = DEFAULT_LOG_LIMIT
    ) -> dict:
        service = _choice("service", service, LOG_SERVICES)
        window = _bounded_int("time_window", time_window, MIN_WINDOW, MAX_WINDOW)
        limit = _bounded_int("limit", limit, 1, MAX_LOG_LIMIT)
        end = self.now()
        start = end - timedelta(seconds=window)
        result = {
            "service": service,
            "time_window_seconds": window,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
        }
        try:
            lines, truncated = self.logs.read(service, start, end)
        except LogsUnavailable as exc:
            return {**result, "status": "unavailable", "error": str(exc)}
        records, lifecycle, dropped = parse_log_lines(lines, service)
        durations = sorted(r["duration_seconds"] for r in records)
        errors = [r for r in records if r["status"] >= 500]
        return {
            **result,
            "status": "ok",
            "summary": {
                "requests": len(records),
                "by_status": dict(Counter(str(r["status"]) for r in records)),
                "by_route": dict(Counter(r["route"] for r in records)),
                "duration_p50_seconds": statistics.median(durations) if durations else None,
                "duration_p95_seconds": (
                    durations[min(len(durations) - 1, math.ceil(0.95 * len(durations)) - 1)]
                    if durations
                    else None
                ),
                "first": records[0]["timestamp"] if records else None,
                "last": records[-1]["timestamp"] if records else None,
            },
            "records": records[-limit:],
            "error_records": errors[-MAX_ERROR_RECORDS:],
            "lifecycle": lifecycle[-MAX_LIFECYCLE:],
            "lines_read": len(lines),
            "dropped_lines": dropped,
            "truncated": truncated,
        }

    def get_active_alerts(self) -> dict:
        try:
            raw = self.prometheus.active_alerts()
        except PrometheusUnavailable as exc:
            return {"status": "unavailable", "error": str(exc), "firing": [], "pending": []}
        out: dict[str, list] = {"firing": [], "pending": []}
        for alert in raw:
            labels = alert.get("labels", {}) if isinstance(alert, dict) else {}
            name, state = labels.get("alertname", ""), alert.get("state")
            if state not in out or not ALERT_NAME.fullmatch(str(name)):
                continue  # label text is untrusted; keep only well-formed alerts
            entry = {"alertname": name, "severity": "unknown", "active_since": None}
            for key in ("severity", "job", "instance"):
                if LABEL_VALUE.fullmatch(str(labels.get(key, ""))):
                    entry[key] = labels[key]
            if isinstance(alert.get("activeAt"), str) and len(alert["activeAt"]) <= 40:
                entry["active_since"] = alert["activeAt"]
            out[state].append(entry)
        for state in out:
            out[state] = sorted(out[state], key=lambda a: a["alertname"])[:MAX_ALERTS]
        return {"status": "ok", **out}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("metrics")
    cmd.add_argument("service", choices=tuple(METRIC_SERVICES))
    cmd.add_argument("--window", type=int, default=300)
    cmd = sub.add_parser("logs")
    cmd.add_argument("service", choices=LOG_SERVICES)
    cmd.add_argument("--window", type=int, default=300)
    cmd.add_argument("--limit", type=int, default=20)
    sub.add_parser("alerts")
    args = parser.parse_args()
    tools = LabTools()
    try:
        if args.command == "metrics":
            result = tools.get_service_metrics(args.service, args.window)
        elif args.command == "logs":
            result = tools.get_service_logs(args.service, args.window, args.limit)
        else:
            result = tools.get_active_alerts()
    except ToolInputError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
