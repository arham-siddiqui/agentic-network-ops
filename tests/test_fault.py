import asyncio
import json
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from lab.fault import CpuFault, burn_cpu, serve_control, validate_duration
from lab.metrics import Metrics


@pytest.mark.parametrize("duration", [0, -1, 121, float("inf"), float("nan"), "60", None, True])
def test_duration_rejects_invalid_input(duration):
    with pytest.raises(ValueError):
        validate_duration(duration)


@pytest.mark.parametrize("duration", [1, 60, 120])
def test_duration_accepts_bounds(duration):
    assert validate_duration(duration) == duration


def test_fault_stop_records_ground_truth_and_resets_metric(tmp_path):
    entered = threading.Event()

    def idle_until_stopped(stop, deadline):
        entered.set()
        assert stop.wait(timeout=3)

    metrics = Metrics()
    controller = CpuFault(metrics, tmp_path / "events.jsonl", burner=idle_until_stopped)
    started = controller.start(60)
    assert entered.wait(timeout=1)
    assert metrics.registry.get_sample_value("lab_cpu_fault_active") == 1
    try:
        with pytest.raises(ValueError, match="already active"):
            controller.start(60)
    finally:
        assert controller.stop() == {"active": False, "run": None}
    assert metrics.registry.get_sample_value("lab_cpu_fault_active") == 0
    events = [json.loads(line) for line in controller.record_path.read_text().splitlines()]
    assert [e["event"] for e in events] == ["scenario_start", "scenario_end"]
    assert events[1]["reason"] == "stopped"
    assert events[1]["run_id"] == started["run"]["run_id"]
    assert events[1]["ended_at"] >= events[0]["started_at"]


def test_automatic_expiry_resets_state(tmp_path):
    controller = CpuFault(Metrics(), tmp_path / "events.jsonl", burner=lambda stop, deadline: None)
    controller.start(1)
    controller._thread.join(timeout=1)
    assert not controller.status()["active"]
    assert '"reason": "duration_elapsed"' in controller.record_path.read_text()
    # Another run is allowed after expiry.
    controller.start(1)
    controller._thread.join(timeout=1)
    assert len(controller.record_path.read_text().splitlines()) == 4


def test_burner_obeys_deadline_without_sleeping(monkeypatch):
    times = iter([0, 0.5, 1])
    monkeypatch.setattr("lab.fault.time.monotonic", lambda: next(times))
    burn_cpu(threading.Event(), deadline=1)
    assert next(times, None) is None


def test_burner_obeys_early_stop():
    stop = threading.Event()
    stop.set()
    burn_cpu(stop, deadline=float("inf"))


def test_control_socket_accepts_commands_and_rejects_bad_input(tmp_path):
    async def exercise(path):
        controller = CpuFault(Metrics(), tmp_path / "events.jsonl", burner=lambda stop, end: None)
        server = await serve_control(controller, path)
        try:
            assert Path(path).stat().st_mode & 0o777 == 0o600
            for request, ok in [
                (b'{"action":"status"}\n', True),
                (b'{"action":"start","duration":999}\n', False),
                (b'{"action":"unknown"}\n', False),
                (b"not json\n", False),
            ]:
                reader, writer = await asyncio.open_unix_connection(path)
                writer.write(request)
                await writer.drain()
                result = json.loads(await reader.readline())
                assert result["ok"] is ok
                writer.close()
                await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    # macOS has a short Unix socket path limit; pytest's default temp path can exceed it.
    with TemporaryDirectory(prefix="lab-socket-", dir="/tmp") as directory:
        asyncio.run(exercise(str(Path(directory) / "control.sock")))
