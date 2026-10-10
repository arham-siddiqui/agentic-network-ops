"""A bounded CPU thread in the worker PID; controls use a container-local Unix socket."""

import asyncio
import json
import math
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from lab.metrics import Metrics

DEFAULT_DURATION = 60
MAX_DURATION = 120
SOCKET_PATH = "/tmp/lab-control.sock"
SCENARIO_PATH = "/var/lib/lab/scenarios.jsonl"


def validate_duration(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("duration must be a number")
    if not math.isfinite(value) or not 1 <= value <= MAX_DURATION:
        raise ValueError(f"duration must be between 1 and {MAX_DURATION} seconds")
    return float(value)


def burn_cpu(stop: threading.Event, deadline: float) -> None:
    # No child process: Linux process_cpu_seconds_total accounts for this thread too.
    value = 1
    while not stop.is_set() and time.monotonic() < deadline:
        for _ in range(10_000):
            value = (value * 1664525 + 1013904223) & 0xFFFFFFFF


class CpuFault:
    def __init__(self, metrics: Metrics, record_path: Path, burner=burn_cpu) -> None:
        self.metrics, self.record_path, self.burner = metrics, record_path, burner
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run: dict | None = None

    def _record(self, event: dict) -> None:
        line = json.dumps(event, allow_nan=False)
        with self.record_path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
        print(line, flush=True)

    def status(self) -> dict:
        with self._lock:
            return {"active": self._run is not None, "run": self._run}

    def start(self, duration: object = DEFAULT_DURATION) -> dict:
        seconds = validate_duration(duration)
        with self._lock:
            if self._run is not None:
                raise ValueError("CPU fault already active; stop it or wait for expiry")
            self._stop.clear()
            run = {
                "run_id": str(uuid.uuid4()),
                "scenario": "worker_cpu_pressure",
                "started_at": datetime.now(UTC).isoformat(),
                "duration_seconds": seconds,
                "max_duration_seconds": MAX_DURATION,
                "method": "in_process_cpu_thread",
                "cpu_limit_cores": 0.5,
            }
            self._record({**run, "event": "scenario_start"})
            self._run = run
            self.metrics.fault_active.set(1)
            self._thread = threading.Thread(
                target=self._execute,
                args=(run, time.monotonic() + seconds),
                daemon=True,
                name="bounded-cpu-fault",
            )
            self._thread.start()
            return {"active": True, "run": run}

    def _execute(self, run: dict, deadline: float) -> None:
        reason = "duration_elapsed"
        try:
            self.burner(self._stop, deadline)
            if self._stop.is_set():
                reason = "stopped"
        except Exception:
            reason = "error"
            raise
        finally:
            with self._lock:
                self.metrics.fault_active.set(0)
                self._run = None
                self._record(
                    {
                        **run,
                        "event": "scenario_end",
                        "reason": reason,
                        "ended_at": datetime.now(UTC).isoformat(),
                    }
                )

    def stop(self) -> dict:
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=3)
            if thread.is_alive():
                raise RuntimeError("CPU fault did not stop within the shutdown deadline")
        return self.status()


async def serve_control(controller: CpuFault, path: str = SOCKET_PATH):
    async def handle(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=2)
            request = json.loads(line)
            action = request["action"]
            if action == "start":
                result = controller.start(request.get("duration", DEFAULT_DURATION))
            elif action == "stop":
                result = await asyncio.to_thread(controller.stop)
            elif action == "status":
                result = controller.status()
            else:
                raise ValueError("unknown action")
            response = {"ok": True, **result}
        except (ValueError, KeyError, TypeError, TimeoutError, RuntimeError, OSError) as exc:
            response = {"ok": False, "error": str(exc)}
        try:
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    Path(path).unlink(missing_ok=True)
    server = await asyncio.start_unix_server(handle, path=path, limit=1024)
    os.chmod(path, 0o600)
    return server
