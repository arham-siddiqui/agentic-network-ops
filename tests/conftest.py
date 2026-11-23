from datetime import UTC, datetime

import httpx
import pytest

from agent.catalog import ALERTS_QUERY, METRICS
from agent.telemetry import PrometheusSource, capture

END = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
PROMQL_TO_ID = {metric.promql: metric_id for metric_id, metric in METRICS.items()}

HEALTHY = {
    "api.up": 1,
    "worker.up": 1,
    "proxy.up": 1,
    "api.request_rate": 9.8,
    "worker.request_rate": 9.8,
    "api.error_fraction": 0.0,
    "worker.error_fraction": 0.0,
    "api.latency_p50": 0.004,
    "api.latency_p95": 0.009,
    "worker.latency_p50": 0.002,
    "worker.latency_p95": 0.004,
    "api.slow_fraction": 0.0,
    "worker.slow_fraction": 0.0,
    "api.cpu": 0.04,
    "worker.cpu": 0.05,
    "api.rss": 6.1e7,
    "worker.rss": 5.9e7,
    "api.worker_calls.success": 9.8,
    "api.worker_calls.timeout": 0.0,
    "api.worker_calls.error": 0.0,
    "api.worker_calls.invalid_response": 0.0,
}


class FakePrometheus:
    """Answers range queries for catalogue PromQL with constant series.

    A metric's value may be a number, None (NaN), "absent" (empty result), "error" (HTTP 500),
    or a list of (seconds_before_end, value) points.
    """

    def __init__(self, values: dict, alerts: list[dict] | None = None, down=False) -> None:
        self.values, self.alerts, self.down = values, alerts or [], down
        self.queries: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        query = request.url.params["query"]
        self.queries.append(query)
        start, end = float(request.url.params["start"]), float(request.url.params["end"])
        step = float(request.url.params["step"])
        if query == ALERTS_QUERY:
            return self._matrix(self.alerts)
        value = self.values.get(PROMQL_TO_ID[query], "absent")
        if value == "error":
            return httpx.Response(500, text="boom")
        if value == "absent":
            return self._matrix([])
        if isinstance(value, list):
            points = [[end - ago, "NaN" if v is None else str(v)] for ago, v in value]
        else:
            count = int((end - start) / step) + 1
            text = "NaN" if value is None else str(value)
            points = [[start + i * step, text] for i in range(count)]
        return self._matrix([{"metric": {}, "values": points}])

    @staticmethod
    def _matrix(result):
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": result}}
        )


def firing(name: str, severity: str = "warning") -> dict:
    ts = END.timestamp()
    return {
        "metric": {"alertname": name, "alertstate": "firing", "severity": severity},
        "values": [[ts - 20, "1"], [ts - 10, "1"], [ts, "1"]],
    }


@pytest.fixture
def bundle_for():
    def build(overrides=None, alerts=None, down=False, lookback=120):
        fake = FakePrometheus({**HEALTHY, **(overrides or {})}, alerts, down)
        source = PrometheusSource(transport=httpx.MockTransport(fake.handle))
        return capture(source, END, lookback, incident_id="0123456789abcdef")

    return build
