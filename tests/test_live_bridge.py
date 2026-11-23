import json
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from agent.catalog import METRICS
from scripts.live_bridge import MARKERS, Handler, snapshot


def fake_query(expression):
    if expression == "ALERTS":
        return [
            {
                "metric": {
                    "alertname": "ApiHighLatency",
                    "severity": "warning",
                    "alertstate": "firing",
                }
            }
        ]
    if expression == METRICS["api.latency_p95"].promql:
        return [{"metric": {}, "value": [0, "NaN"]}]
    if expression == METRICS["worker.rss"].promql:
        raise OSError("refused")
    if expression == MARKERS["network"]:
        return [{"metric": {}, "value": [0, "1"]}]
    return [{"metric": {}, "value": [0, "0.5"]}]


def test_snapshot_keeps_missing_values_null():
    data = snapshot(fake_query)
    assert set(data["metrics"]) == set(METRICS)
    assert data["metrics"]["api.latency_p95"] is None and data["metrics"]["worker.rss"] is None
    assert data["metrics"]["api.request_rate"] == 0.5
    assert data["alerts"] == [
        {"alertname": "ApiHighLatency", "severity": "warning", "state": "firing"}
    ]
    assert data["fault_markers"]["network"] == 1 and "ground truth" in data["fault_markers_note"]


def test_snapshot_reports_prometheus_down():
    def down(expression):
        raise OSError("refused")

    data = snapshot(down)
    assert data["prometheus_available"] is False
    assert all(value is None for value in data["metrics"].values())


@pytest.fixture
def server(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>t</title>")
    handler = partial(Handler, directory=str(tmp_path), query=fake_query)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_serves_site_and_snapshot_read_only(server):
    assert b"<title>t</title>" in urlopen(f"{server}/").read()
    data = json.loads(urlopen(f"{server}/live/snapshot").read())
    assert data["metrics"]["api.request_rate"] == 0.5
    for method in ("POST", "PUT", "DELETE"):
        with pytest.raises(HTTPError) as error:
            urlopen(Request(f"{server}/live/snapshot", method=method, data=b"{}"))
        assert error.value.code == 405
    with pytest.raises(HTTPError):
        urlopen(f"{server}/../compose.yaml")
