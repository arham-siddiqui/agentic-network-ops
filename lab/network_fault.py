"""Bounded API->worker network impairment through Toxiproxy, controlled over a Unix socket.

Runs as its own container on the control network. Only this process talks to the proxy's admin
API, and only with the fixed proxy name and toxic definitions below; there is no option for an
arbitrary proxy, upstream, or toxic type.
"""

import asyncio
import json
import math
import signal
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from prometheus_client import CollectorRegistry, Gauge, start_http_server

from lab.fault import DEFAULT_DURATION, BoundedScenario, serve_control, validate_duration

ADMIN_URL = "http://proxy-admin:8474"
PROXY_NAME = "api_to_worker"
UPSTREAM = "worker:8000"
SOCKET_PATH = "/tmp/lab-network-control.sock"
SCENARIO_PATH = "/var/lib/lab/network-scenarios.jsonl"
METRICS_PORT = 9101
SCENARIOS = ("latency", "timeout")
DEFAULT_LATENCY_MS = 600
MIN_LATENCY_MS = 50
# Keeps latency + jitter at least 500 ms under the API's 2-second worker-call timeout, so the
# latency scenario stays a slow-response regime and never turns into the timeout regime.
MAX_DELAY_MS = 1500
MAX_JITTER_MS = 250
REQUEST_KEYS = {"action", "scenario", "duration", "latency_ms", "jitter_ms"}


def _milliseconds(name: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or value != int(value) or not low <= value <= high:
        raise ValueError(f"{name} must be a whole number between {low} and {high}")
    return int(value)


def build_toxic(scenario: object, latency_ms: object = None, jitter_ms: object = None) -> dict:
    """Return one of the two fixed toxic definitions, with validated bounded attributes."""
    if scenario == "latency":
        latency = _milliseconds(
            "latency_ms",
            DEFAULT_LATENCY_MS if latency_ms is None else latency_ms,
            MIN_LATENCY_MS,
            MAX_DELAY_MS,
        )
        jitter = _milliseconds("jitter_ms", 0 if jitter_ms is None else jitter_ms, 0, MAX_JITTER_MS)
        if latency + jitter > MAX_DELAY_MS:
            raise ValueError(f"latency_ms + jitter_ms must not exceed {MAX_DELAY_MS}")
        # Downstream = worker -> API response bytes; each response chunk is held for the delay.
        return {
            "name": "lab_latency",
            "type": "latency",
            "stream": "downstream",
            "toxicity": 1.0,
            "attributes": {"latency": latency, "jitter": jitter},
        }
    if scenario == "timeout":
        if latency_ms is not None or jitter_ms is not None:
            raise ValueError("the timeout scenario takes no latency or jitter")
        # Upstream = API -> worker request bytes are dropped; the connection stays open, so the
        # API's own read timeout produces the failure.
        return {
            "name": "lab_timeout",
            "type": "timeout",
            "stream": "upstream",
            "toxicity": 1.0,
            "attributes": {"timeout": 0},
        }
    raise ValueError(f"scenario must be one of: {', '.join(SCENARIOS)}")


class ToxiproxyAdmin:
    """The few admin calls this lab needs, always scoped to the one configured proxy."""

    def __init__(self, base_url: str = ADMIN_URL, transport=None) -> None:
        self.client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(3, connect=1),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def _call(self, method: str, path: str, *, allow_missing=False, **kwargs) -> httpx.Response:
        try:
            response = self.client.request(method, path, **kwargs)
            if allow_missing and response.status_code == 404:
                return response
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"proxy admin API error: {exc}") from exc
        return response

    def check_proxy(self) -> None:
        proxy = self._call("GET", f"/proxies/{PROXY_NAME}").json()
        if proxy.get("upstream") != UPSTREAM or proxy.get("enabled") is not True:
            raise RuntimeError(f"proxy {PROXY_NAME} is not an enabled path to {UPSTREAM}")

    def toxic_names(self) -> list[str]:
        return sorted(t["name"] for t in self._call("GET", f"/proxies/{PROXY_NAME}/toxics").json())

    def add_toxic(self, toxic: dict) -> None:
        self._call("POST", f"/proxies/{PROXY_NAME}/toxics", json=toxic)

    def remove_toxic(self, name: str) -> None:
        # Idempotent: an already-removed toxic is the desired end state.
        self._call("DELETE", f"/proxies/{PROXY_NAME}/toxics/{name}", allow_missing=True)

    def clear_toxics(self) -> list[str]:
        names = self.toxic_names()
        for name in names:
            self.remove_toxic(name)
        return names


def hold_until_deadline(stop, deadline: float) -> None:
    stop.wait(max(0.0, deadline - time.monotonic()))


class NetworkFault(BoundedScenario):
    # Covers revert's three bounded admin calls and retry delays.
    stop_timeout = 15.0

    def __init__(self, admin: ToxiproxyAdmin, record_path: Path, retry_delay: float = 1) -> None:
        self.registry = CollectorRegistry()
        marker = Gauge(
            "lab_network_fault_active",
            "Ground-truth marker only: 1 while a network impairment is applied.",
            registry=self.registry,
        )
        super().__init__(marker, record_path)
        self.admin, self.retry_delay = admin, retry_delay

    def start(
        self,
        scenario: object,
        duration: object = DEFAULT_DURATION,
        latency_ms: object = None,
        jitter_ms: object = None,
    ) -> dict:
        seconds = validate_duration(duration)
        toxic = build_toxic(scenario, latency_ms, jitter_ms)
        fields = {
            "scenario": f"network_{scenario}",
            "duration_seconds": seconds,
            "method": f"toxiproxy_{toxic['type']}_{toxic['stream']}",
            "proxy": PROXY_NAME,
            "upstream": UPSTREAM,
            "toxic": toxic,
        }

        def apply() -> None:
            self.admin.check_proxy()
            if existing := self.admin.toxic_names():
                # Never stack on a leftover impairment; it would corrupt this run's ground truth.
                raise ValueError(f"proxy already has toxics {existing}; run stop to clear them")
            self.admin.add_toxic(toxic)

        def revert() -> None:
            for attempt in range(3):
                try:
                    self.admin.remove_toxic(toxic["name"])
                    return
                except RuntimeError:
                    if attempt == 2:
                        raise
                    time.sleep(self.retry_delay)

        return self._launch(fields, seconds, hold_until_deadline, apply=apply, revert=revert)

    def start_request(self, request: dict) -> dict:
        if unknown := set(request) - REQUEST_KEYS:
            raise ValueError(f"unknown request fields: {sorted(unknown)}")
        return self.start(
            request.get("scenario"),
            request.get("duration", DEFAULT_DURATION),
            request.get("latency_ms"),
            request.get("jitter_ms"),
        )

    def status(self) -> dict:
        result = super().status()
        try:
            result["proxy_toxics"] = self.admin.toxic_names()
        except RuntimeError as exc:
            result["proxy_error"] = str(exc)
        return result

    def stop(self) -> dict:
        super().stop()
        # Stopping with nothing active still clears leftovers, so stop is the stale-toxic fix.
        with self._lock:
            removed = self.admin.clear_toxics() if self._run is None else []
        return {**self.status(), "removed_toxics": removed}

    def reconcile(self, attempts: int = 30) -> list[str]:
        """Clear toxics left by an earlier controller process; retries while the proxy starts."""
        for attempt in range(attempts):
            try:
                removed = self.admin.clear_toxics()
            except RuntimeError:
                if attempt == attempts - 1:
                    raise
                time.sleep(self.retry_delay)
                continue
            if removed:
                self._record(
                    {
                        "event": "stale_toxics_removed",
                        "toxics": removed,
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
            return removed
        return []


async def serve() -> None:
    controller = NetworkFault(ToxiproxyAdmin(), Path(SCENARIO_PATH))
    start_http_server(METRICS_PORT, registry=controller.registry)
    try:
        await asyncio.to_thread(controller.reconcile)
    except RuntimeError as exc:
        # Keep serving so status can report the problem; starts will fail until it is fixed.
        print(json.dumps({"event": "reconcile_failed", "error": str(exc)}), flush=True)
    server = await serve_control(controller, SOCKET_PATH)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    try:
        await stopping.wait()
    finally:
        server.close()
        await server.wait_closed()
        try:
            await asyncio.to_thread(controller.stop)
        except RuntimeError as exc:
            print(json.dumps({"event": "shutdown_cleanup_failed", "error": str(exc)}), flush=True)
        Path(SOCKET_PATH).unlink(missing_ok=True)


def main() -> None:
    if not Path("/.dockerenv").exists():
        raise SystemExit("Network fault control requires this lab's Docker controller container")
    asyncio.run(serve())


if __name__ == "__main__":
    main()
