import json
import threading

import httpx
import pytest

from lab.network_fault import (
    PROXY_NAME,
    UPSTREAM,
    NetworkFault,
    ToxiproxyAdmin,
    build_toxic,
)


class FakeToxiproxy:
    """In-memory stand-in for the admin endpoints used by the controller."""

    def __init__(self, upstream=UPSTREAM, enabled=True):
        self.upstream, self.enabled = upstream, enabled
        self.toxics: dict[str, dict] = {}
        self.fail_deletes = 0
        self.down = False
        self.requests: list[tuple[str, str]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        base = f"/proxies/{PROXY_NAME}"
        path = request.url.path
        if not path.startswith(base):
            return httpx.Response(404, json={"error": "proxy not found"})
        if request.method == "GET" and path == base:
            return httpx.Response(
                200,
                json={"name": PROXY_NAME, "upstream": self.upstream, "enabled": self.enabled},
            )
        if path == f"{base}/toxics":
            if request.method == "GET":
                return httpx.Response(200, json=list(self.toxics.values()))
            toxic = json.loads(request.content)
            if toxic["name"] in self.toxics:
                return httpx.Response(409, json={"error": "toxic already exists"})
            self.toxics[toxic["name"]] = toxic
            return httpx.Response(200, json=toxic)
        name = path.rsplit("/", 1)[-1]
        if request.method == "DELETE":
            if self.fail_deletes:
                self.fail_deletes -= 1
                return httpx.Response(500)
            if self.toxics.pop(name, None) is None:
                return httpx.Response(404, json={"error": "toxic not found"})
            return httpx.Response(204)
        return httpx.Response(405)


def controller(tmp_path, fake):
    admin = ToxiproxyAdmin("http://proxy-admin:8474", transport=httpx.MockTransport(fake.handle))
    return NetworkFault(admin, tmp_path / "network.jsonl", retry_delay=0)


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def marker(fault):
    return fault.registry.get_sample_value("lab_network_fault_active")


@pytest.mark.parametrize(
    ("scenario", "latency", "jitter"),
    [
        ("loss", None, None),
        (None, None, None),
        ("latency", 49, 0),
        ("latency", 1501, 0),
        ("latency", 1400, 101),
        ("latency", 600, 251),
        ("latency", 600, -1),
        ("latency", 600.5, 0),
        ("latency", True, 0),
        ("latency", "600", 0),
        ("latency", float("nan"), 0),
        ("timeout", 600, None),
        ("timeout", None, 10),
    ],
)
def test_toxic_parameters_are_bounded(scenario, latency, jitter):
    with pytest.raises(ValueError):
        build_toxic(scenario, latency, jitter)


def test_toxic_definitions_are_fixed():
    assert build_toxic("latency") == {
        "name": "lab_latency",
        "type": "latency",
        "stream": "downstream",
        "toxicity": 1.0,
        "attributes": {"latency": 600, "jitter": 0},
    }
    assert build_toxic("latency", 1250, 250)["attributes"] == {"latency": 1250, "jitter": 250}
    assert build_toxic("timeout") == {
        "name": "lab_timeout",
        "type": "timeout",
        "stream": "upstream",
        "toxicity": 1.0,
        "attributes": {"timeout": 0},
    }


def test_latency_run_applies_then_removes_toxic_on_expiry(tmp_path):
    fake = FakeToxiproxy()
    fault = controller(tmp_path, fake)
    started = fault.start("latency", 1, latency_ms=700)
    assert started["run"]["scenario"] == "network_latency"
    assert fake.toxics["lab_latency"]["attributes"]["latency"] == 700
    assert marker(fault) == 1
    fault._thread.join(timeout=3)
    assert fake.toxics == {}
    assert marker(fault) == 0
    start, end = events(fault.record_path)
    assert (start["event"], end["event"], end["reason"]) == (
        "scenario_start",
        "scenario_end",
        "duration_elapsed",
    )
    assert start["run_id"] == end["run_id"] and start["toxic"]["type"] == "latency"


def test_early_stop_removes_toxic_and_is_idempotent(tmp_path):
    fake = FakeToxiproxy()
    fault = controller(tmp_path, fake)
    fault.start("timeout", 60)
    assert list(fake.toxics) == ["lab_timeout"]
    with pytest.raises(ValueError, match="already active"):
        fault.start("latency", 60)
    stopped = fault.stop()
    assert stopped["active"] is False and fake.toxics == {}
    assert fault.stop()["removed_toxics"] == []
    assert events(fault.record_path)[-1]["reason"] == "stopped"


def test_stop_clears_stale_toxics_and_start_refuses_to_stack(tmp_path):
    fake = FakeToxiproxy()
    fake.toxics["lab_latency"] = {"name": "lab_latency"}
    fault = controller(tmp_path, fake)
    assert fault.status()["proxy_toxics"] == ["lab_latency"]
    with pytest.raises(ValueError, match="already has toxics"):
        fault.start("latency", 10)
    assert not fault.record_path.exists()  # nothing was injected, so no ground truth
    assert fault.stop()["removed_toxics"] == ["lab_latency"]
    assert fake.toxics == {}


def test_misconfigured_proxy_is_rejected(tmp_path):
    fault = controller(tmp_path, FakeToxiproxy(upstream="example.invalid:80"))
    with pytest.raises(RuntimeError, match="not an enabled path"):
        fault.start("latency", 10)
    assert marker(fault) == 0 and not fault.record_path.exists()


def test_unreachable_proxy_fails_start_and_reports_status(tmp_path):
    fake = FakeToxiproxy()
    fake.down = True
    fault = controller(tmp_path, fake)
    with pytest.raises(RuntimeError, match="proxy admin API error"):
        fault.start("latency", 10)
    assert "proxy_error" in fault.status()
    assert fault.status()["active"] is False


def test_revert_retries_then_records_cleanup_failure(tmp_path):
    fake = FakeToxiproxy()
    fault = controller(tmp_path, fake)
    fake.fail_deletes = 2
    fault.start("latency", 1)
    fault._thread.join(timeout=3)
    assert fake.toxics == {} and events(fault.record_path)[-1]["reason"] == "duration_elapsed"

    fake.fail_deletes = 3
    fault.start("latency", 1)
    fault._thread.join(timeout=3)
    assert "lab_latency" in fake.toxics
    assert events(fault.record_path)[-1]["reason"] == "cleanup_failed"
    assert marker(fault) == 0
    assert fault.stop()["removed_toxics"] == ["lab_latency"]


def test_reconcile_removes_leftovers_and_records_them(tmp_path):
    fake = FakeToxiproxy()
    fake.toxics["lab_timeout"] = {"name": "lab_timeout"}
    fault = controller(tmp_path, fake)
    assert fault.reconcile() == ["lab_timeout"]
    assert events(fault.record_path)[0]["event"] == "stale_toxics_removed"
    assert fault.reconcile() == []


def test_reconcile_retries_while_proxy_starts(tmp_path):
    fake = FakeToxiproxy()
    fake.down = True
    fault = controller(tmp_path, fake)
    with pytest.raises(RuntimeError):
        fault.reconcile(attempts=2)
    fake.down = False
    assert fault.reconcile(attempts=2) == []


def test_requests_reject_unknown_fields_and_only_touch_one_proxy(tmp_path):
    fake = FakeToxiproxy()
    fault = controller(tmp_path, fake)
    with pytest.raises(ValueError, match="unknown request fields"):
        fault.start_request({"action": "start", "scenario": "latency", "upstream": "evil:80"})
    fault.start_request({"action": "start", "scenario": "latency", "duration": 1})
    fault._thread.join(timeout=3)
    assert all(path.startswith(f"/proxies/{PROXY_NAME}") for _, path in fake.requests)


def test_stop_waits_for_slow_revert(tmp_path):
    release = threading.Event()
    fake = FakeToxiproxy()
    original = fake.handle

    def slow_delete(request):
        if request.method == "DELETE":
            release.wait(timeout=2)
        return original(request)

    admin = ToxiproxyAdmin("http://proxy-admin:8474", transport=httpx.MockTransport(slow_delete))
    fault = NetworkFault(admin, tmp_path / "network.jsonl", retry_delay=0)
    fault.start("latency", 60)
    threading.Timer(0.2, release.set).start()
    assert fault.stop()["active"] is False
    assert fake.toxics == {}
