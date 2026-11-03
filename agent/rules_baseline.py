"""Deterministic diagnosis over an evidence bundle: the baseline every LLM mode is scored against.

Thresholds match the checked-in alert rules. The rules fail closed: when evidence needed to tell
causes apart is missing or contradictory, the verdict is unknown_or_ambiguous.
"""

from agent.reporting import ReportBuilder
from agent.schemas import EvidenceBundle, IncidentReport
from agent.telemetry import summarize

CPU_PRESSURE_CORES = 0.30  # WorkerCpuPressure
SLOW_FRACTION = 0.25  # ApiHighLatency
TIMEOUT_SHARE = 0.20  # WorkerCallTimeouts
ERROR_SHARE = 0.20  # ApiHighErrorRate
MIN_TRAFFIC = 0.2  # requests/s, ApiHighLatency's traffic guard
DOWN_BELOW = 0.5  # mean scrape success over the last 30 s

ACTIONS = {
    "healthy": "No action needed. Keep monitoring.",
    "cpu_pressure": (
        "Find what is consuming worker CPU and whether it is expected load; consider capacity "
        "only after a human review. Do not restart or rescale automatically from this report."
    ),
    "network_latency": (
        "Inspect the API-to-worker network path (proxy and links) for added delay; the worker "
        "itself responds quickly. Do not change routing from this report alone."
    ),
    "network_connectivity_or_timeout": (
        "Test connectivity on the API-to-worker path and confirm the worker receives requests; "
        "check the proxy and network before restarting services."
    ),
    "service_unavailable": (
        "Check the named component's container status and logs, then restore it with human "
        "approval."
    ),
    "unknown_or_ambiguous": (
        "Escalate to a human operator; gather the missing or conflicting evidence listed under "
        "limitations before acting."
    ),
}


def _fraction(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.0%}"


def diagnose(bundle: EvidenceBundle) -> IncidentReport:
    s = {metric_id: summarize(bundle, metric_id) for metric_id in bundle.series}
    report = ReportBuilder(bundle, "rules", s)

    def mean(metric_id: str) -> float | None:
        return s[metric_id].recent_mean

    traffic, api_slow = mean("api.request_rate"), mean("api.slow_fraction")
    p95, api_errors = mean("api.latency_p95"), mean("api.error_fraction")
    report.impact = (
        f"API /work traffic {'unknown' if traffic is None else f'{traffic:.1f} req/s'}; "
        f"{_fraction(api_slow)} of responses slower than 250 ms "
        f"(p95 {'unknown' if p95 is None else f'{p95:.2f} s'}); "
        f"{_fraction(api_errors)} returned HTTP 5xx."
    )

    if all(series.status == "unavailable" for series in bundle.series.values()):
        report.limit("Prometheus was unavailable; no telemetry could be read.")
        return report.finish("unknown_or_ambiguous", "No telemetry available.", ACTIONS)

    required = ("api.up", "worker.up", "proxy.up", "api.request_rate")
    missing = [metric_id for metric_id in required if not s[metric_id].usable]
    if missing:
        report.limit(f"Required evidence missing or stale: {', '.join(missing)}.")
        return report.finish("unknown_or_ambiguous", "Core health evidence is missing.", ACTIONS)

    down = [
        name
        for name, metric_id in (("API", "api.up"), ("worker", "worker.up"), ("proxy", "proxy.up"))
        if report.cite(metric_id) < DOWN_BELOW
    ]
    if down:
        return report.finish(
            "service_unavailable",
            f"Metrics scrapes are failing for: {', '.join(down)}.",
            ACTIONS,
        )

    if report.cite("api.request_rate") < MIN_TRAFFIC:
        report.limit("Too little /work traffic in the last 30 s to judge latency or errors.")
        return report.finish("unknown_or_ambiguous", "Insufficient traffic to assess.", ACTIONS)

    calls = {
        outcome: mean(f"api.worker_calls.{outcome}") or 0.0
        for outcome in ("success", "timeout", "error", "invalid_response")
    }
    total = sum(calls.values())
    timeout_share = calls["timeout"] / total if total > 0 else 0.0
    error_share = (calls["error"] + calls["invalid_response"]) / total if total > 0 else 0.0
    worker_cpu = mean("worker.cpu")
    cpu_high = worker_cpu is not None and worker_cpu >= CPU_PRESSURE_CORES
    if worker_cpu is not None:
        report.cite("worker.cpu")

    if timeout_share >= TIMEOUT_SHARE or error_share >= ERROR_SHARE:
        timing_out = timeout_share >= TIMEOUT_SHARE
        kind = "timing out" if timing_out else "failing"
        report.cite("api.worker_calls.timeout" if timing_out else "api.worker_calls.error")
        report.cite("api.worker_calls.success")
        if s["worker.request_rate"].usable:
            report.cite("worker.request_rate")
        if cpu_high:
            report.limit("Worker CPU pressure is also present; it could cause the failures.")
            return report.finish(
                "unknown_or_ambiguous",
                f"API calls to the worker are {kind} while worker CPU is high.",
                ACTIONS,
            )
        return report.finish(
            "network_connectivity_or_timeout",
            f"{max(timeout_share, error_share):.0%} of API calls to the worker are {kind} while "
            "every component's metrics endpoint is up.",
            ACTIONS,
        )

    if worker_cpu is None:
        report.limit("Worker CPU is unavailable, so CPU pressure cannot be ruled in or out.")
        return report.finish("unknown_or_ambiguous", "Worker CPU evidence is missing.", ACTIONS)

    if cpu_high:
        if api_slow is not None:
            report.cite("api.slow_fraction")
        worker_slow = mean("worker.slow_fraction")
        api_only = (worker_slow or 0) < SLOW_FRACTION
        if api_slow is not None and api_slow >= SLOW_FRACTION and api_only:
            report.limit(
                "The API is slow while worker-side latency is normal. CPU queueing before the "
                "worker's handler and path delay look alike here; network delay is not excluded."
            )
        return report.finish(
            "cpu_pressure",
            f"Worker process is using {worker_cpu:.2f} cores against a 0.5-core quota.",
            ACTIONS,
        )

    if api_slow is None:
        report.limit("API latency is undefined (no histogram observations in the last 30 s).")
        return report.finish("unknown_or_ambiguous", "API latency evidence is missing.", ACTIONS)
    report.cite("api.slow_fraction")

    if api_slow >= SLOW_FRACTION:
        report.cite("api.latency_p95")
        worker_slow = mean("worker.slow_fraction")
        if worker_slow is None:
            report.limit("Worker-side latency is unavailable, so the delay cannot be located.")
            return report.finish("unknown_or_ambiguous", "API responses are slow.", ACTIONS)
        report.cite("worker.slow_fraction")
        if s["worker.latency_p95"].usable:
            report.cite("worker.latency_p95")
        if worker_slow >= SLOW_FRACTION:
            report.limit("The worker itself is slow with normal CPU; not a modeled cause.")
            return report.finish(
                "unknown_or_ambiguous", "Both API and worker responses are slow.", ACTIONS
            )
        return report.finish(
            "network_latency",
            f"{api_slow:.0%} of API responses exceed 250 ms while only {worker_slow:.0%} of "
            "worker-side responses do, so the delay is between the API and the worker.",
            ACTIONS,
        )

    if s["api.error_fraction"].usable:
        report.cite("api.error_fraction")
    return report.finish(
        "healthy",
        "All components are up; latency, errors, and worker CPU are within alert thresholds.",
        ACTIONS,
    )
