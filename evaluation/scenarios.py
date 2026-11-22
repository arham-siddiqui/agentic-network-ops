"""Labelled scenario manifest. Labels live here and in ground-truth files, never in bundles."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Scenario:
    name: str
    label: str
    control: str | None  # "cpu", "network", or None for the healthy control
    start_args: tuple[str, ...] = ()
    alert: str | None = None  # the alert this scenario is expected to fire, if any
    params: dict = field(default_factory=dict)
    demo: str | None = None  # scripts.demo scenario whose signal/threshold defines recovery
    affected: str = "none"  # ground-truth affected service, scored against reports


SCENARIOS = {
    s.name: s
    for s in (
        Scenario("healthy", "healthy", None),
        Scenario(
            "cpu",
            "cpu_pressure",
            "cpu",
            (),
            "WorkerCpuPressure",
            {"duration_s": 60},
            "cpu",
            "worker",
        ),
        Scenario(
            "latency",
            "network_latency",
            "network",
            ("--scenario", "latency", "--latency-ms", "600"),
            "ApiHighLatency",
            {"duration_s": 60, "latency_ms": 600},
            "latency",
            "api_worker_path",
        ),
        Scenario(
            "latency_low",
            "network_latency",
            "network",
            ("--scenario", "latency", "--latency-ms", "300"),
            "ApiHighLatency",
            {"duration_s": 60, "latency_ms": 300},
            "latency",
            "api_worker_path",
        ),
        Scenario(
            "timeout",
            "network_connectivity_or_timeout",
            "network",
            ("--scenario", "timeout"),
            "WorkerCallTimeouts",
            {"duration_s": 60},
            "timeout",
            "api_worker_path",
        ),
        # The worker container is stopped and restarted by scripts/worker_outage.py.
        Scenario(
            "worker_down",
            "service_unavailable",
            "worker_outage",
            ("--duration", "60"),
            "ServiceUnavailable",
            {"duration_s": 60},
            "worker_down",
            "worker",
        ),
    )
}
DEFAULT_SCENARIOS = ("healthy", "cpu", "latency", "timeout", "worker_down")
