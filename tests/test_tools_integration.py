"""Integration checks against the running Compose lab: LAB_INTEGRATION=1 make integration."""

import json
import os
import time

import httpx
import pytest

from agent.catalog import FORBIDDEN_TOKENS
from agent.tools import LabTools

pytestmark = pytest.mark.skipif(
    os.getenv("LAB_INTEGRATION") != "1", reason="needs the running lab: make integration"
)


@pytest.fixture(scope="module")
def lab():
    # Enough traffic that latency percentiles are defined even on an otherwise idle lab.
    with httpx.Client(timeout=5, trust_env=False) as client:
        for _ in range(30):
            client.get("http://127.0.0.1:8000/work").raise_for_status()
            time.sleep(0.1)
    time.sleep(6)  # at least two 2 s scrapes after the traffic
    return LabTools()


@pytest.mark.parametrize("service", ["api", "worker", "proxy"])
def test_live_metrics_are_available(lab, service):
    result = lab.get_service_metrics(service, 120)
    assert result["metrics"][f"{service}.up"]["status"] == "ok"
    assert result["metrics"][f"{service}.up"]["last"] == 1


def test_live_api_metrics_cover_cpu_memory_latency_throughput_errors(lab):
    metrics = lab.get_service_metrics("api", 120)["metrics"]
    for metric_id in ("api.cpu", "api.rss", "api.latency_p95", "api.request_rate"):
        assert metrics[metric_id]["status"] == "ok", metric_id
    # Zero errors is reported as 0, not as missing data.
    assert metrics["api.error_fraction"]["status"] == "ok"


@pytest.mark.parametrize("service", ["api", "worker"])
def test_live_logs_are_structured_and_free_of_ground_truth(lab, service):
    result = lab.get_service_logs(service, 120, limit=20)
    assert result["status"] == "ok" and result["summary"]["requests"] > 0
    assert all(
        set(r) == {"timestamp", "method", "route", "status", "duration_seconds"}
        for r in result["records"]
    )
    text = json.dumps(result)
    assert "run_id" not in text and not any(t in text for t in FORBIDDEN_TOKENS)


def test_live_alerts_respond(lab):
    result = lab.get_active_alerts()
    assert result["status"] == "ok"
    assert isinstance(result["firing"], list) and isinstance(result["pending"], list)
