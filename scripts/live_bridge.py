"""Serve the demo site locally with a read-only live view of the lab. Standard library only.

  python3 -m scripts.live_bridge            # http://127.0.0.1:8088

GET /live/snapshot returns the current value of every catalogue metric (the same fixed PromQL
the investigator uses), the firing/pending alerts, and the two fault markers, labelled as
ground truth. There is no query passthrough and no fault control: inject faults from a terminal.
"""

import argparse
import json
import math
import os
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

from agent.catalog import METRICS

ROOT = Path(__file__).resolve().parents[1]
OPENER = build_opener(ProxyHandler({}))
ALERTS = "ALERTS"
# Ground truth for display only; the investigator's catalogue never includes these.
MARKERS = {
    "cpu": 'lab_cpu_fault_active{job="worker"}',
    "network": 'lab_network_fault_active{job="network-fault-controller"}',
}


def prometheus_query(base: str, expression: str) -> list[dict]:
    url = f"{base}/api/v1/query?{urlencode({'query': expression})}"
    with OPENER.open(url, timeout=3) as response:
        data = json.load(response)
    if data.get("status") != "success":
        raise RuntimeError("Prometheus query failed")
    return data["data"]["result"]


def _value(result: list[dict]) -> float | None:
    if len(result) != 1:
        return None
    value = float(result[0]["value"][1])
    return value if math.isfinite(value) else None


def snapshot(query) -> dict:
    """query(expression) -> Prometheus vector result. Missing data stays null, never zero."""
    metrics = {}
    for metric_id, metric in METRICS.items():
        try:
            metrics[metric_id] = _value(query(metric.promql))
        except (OSError, ValueError, RuntimeError):
            metrics[metric_id] = None
    try:
        alerts = sorted(
            (
                {
                    "alertname": item["metric"].get("alertname", "")[:64],
                    "severity": item["metric"].get("severity", "unknown")[:16],
                    "state": item["metric"].get("alertstate", "unknown")[:16],
                }
                for item in query(ALERTS)
            ),
            key=lambda a: (a["alertname"], a["state"]),
        )
        available = True
    except (OSError, ValueError, RuntimeError):
        alerts, available = [], False
    markers = {}
    for name, expression in MARKERS.items():
        try:
            markers[name] = _value(query(expression))
        except (OSError, ValueError, RuntimeError):
            markers[name] = None
    return {
        "at": time.time(),
        "prometheus_available": available,
        "metrics": metrics,
        "alerts": alerts,
        "fault_markers": markers,
        "fault_markers_note": "ground truth for display only; not an alert or diagnosis input",
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, query, **kwargs):
        self.query = query
        super().__init__(*args, **kwargs)

    def do_GET(self):  # noqa: N802 - http.server naming
        if self.path.split("?", 1)[0] == "/live/snapshot":
            body = json.dumps(snapshot(self.query)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802
        self.send_error(405, "read-only")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def log_message(self, format, *args):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--site-dir", type=Path, default=ROOT / "site")
    parser.add_argument(
        "--prometheus-url", default=f"http://127.0.0.1:{os.getenv('PROMETHEUS_PORT', '9090')}"
    )
    args = parser.parse_args()
    if not (args.site_dir / "index.html").exists():
        parser.error(f"no index.html in {args.site_dir}")
    handler = partial(
        Handler,
        directory=str(args.site_dir),
        query=partial(prometheus_query, args.prometheus_url),
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"Live demo: http://127.0.0.1:{args.port}/  (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
