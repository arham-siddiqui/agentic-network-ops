import json
import signal
import subprocess

import pytest

from scripts.worker_outage import run_outage, validate_duration


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("duration", [0, 121, float("nan"), float("inf"), -5])
def test_duration_is_bounded(duration):
    with pytest.raises(ValueError):
        validate_duration(duration)


def test_outage_stops_then_always_restarts(tmp_path, capsys):
    calls = []
    reason = run_outage(5, lambda *a: calls.append(a[0]), tmp_path / "o.jsonl", FakeClock())
    assert reason == "duration_elapsed" and calls == ["stop", "start"]
    start, end = events(tmp_path / "o.jsonl")
    assert (start["event"], end["event"]) == ("scenario_start", "scenario_end")
    assert start["run_id"] == end["run_id"] and start["scenario"] == "worker_unavailable"
    assert json.loads(capsys.readouterr().out.splitlines()[0])["run_id"] == start["run_id"]


def test_signal_stops_early_and_still_restarts(tmp_path):
    calls = []
    clock = FakeClock()

    def sleep(seconds):
        clock.now += seconds
        signal.raise_signal(signal.SIGTERM)

    clock.sleep = sleep
    reason = run_outage(60, lambda *a: calls.append(a[0]), tmp_path / "o.jsonl", clock)
    assert reason == "stopped" and calls == ["stop", "start"]
    assert events(tmp_path / "o.jsonl")[-1]["reason"] == "stopped"
    assert signal.getsignal(signal.SIGTERM) is not None  # handlers restored


def test_failed_stop_and_failed_restart_are_recorded(tmp_path):
    def stop_fails(action, service):
        if action == "stop":
            raise subprocess.CalledProcessError(1, "docker")

    assert run_outage(5, stop_fails, tmp_path / "a.jsonl", FakeClock()) == "error"

    def start_fails(action, service):
        if action == "start":
            raise subprocess.TimeoutExpired("docker", 60)

    assert run_outage(5, start_fails, tmp_path / "b.jsonl", FakeClock()) == "cleanup_failed"
