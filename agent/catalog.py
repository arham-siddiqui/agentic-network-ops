"""The complete allowlist of telemetry an investigator may see.

Every query is server-defined PromQL; callers choose a metric ID, never a query string. Fault
markers, the network fault controller's job, and proxy toxic state are deliberately absent: they
are ground truth for the evaluator only.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    promql: str
    unit: str
    description: str


def _work(job: str, selector: str = "") -> str:
    return f'job="{job}",route="/work"{selector}'


def _quantile(q: str, job: str) -> str:
    return (
        f"histogram_quantile({q}, sum by (le) "
        f"(rate(lab_http_request_duration_seconds_bucket{{{_work(job)}}}[30s])))"
    )


def _slow_fraction(job: str) -> str:
    fast = ',le="0.25"'
    bucket = f"lab_http_request_duration_seconds_bucket{{{_work(job, fast)}}}"
    count = f"lab_http_request_duration_seconds_count{{{_work(job)}}}"
    return f"1 - sum(rate({bucket}[30s])) / sum(rate({count}[30s]))"


def _error_fraction(job: str) -> str:
    requests, failed = "lab_http_requests_total", ',status=~"5.."'
    return (
        f"sum(rate({requests}{{{_work(job, failed)}}}[30s])) "
        f"/ sum(rate({requests}{{{_work(job)}}}[30s]))"
    )


SERVICES = ("api", "worker")
OUTCOMES = ("success", "timeout", "error", "invalid_response")

METRICS: dict[str, Metric] = {
    "proxy.up": Metric(
        'up{job="impairment-proxy"}',
        "bool",
        "1 if Prometheus scraped the API-to-worker proxy's metrics endpoint.",
    ),
}
for _service in SERVICES:
    METRICS.update(
        {
            f"{_service}.up": Metric(
                f'up{{job="{_service}"}}',
                "bool",
                f"1 if Prometheus scraped the {_service} metrics endpoint.",
            ),
            f"{_service}.request_rate": Metric(
                f"sum(rate(lab_http_requests_total{{{_work(_service)}}}[30s]))",
                "requests/s",
                f"Completed /work requests per second at the {_service}.",
            ),
            f"{_service}.error_fraction": Metric(
                _error_fraction(_service),
                "fraction",
                f"Fraction of {_service} /work responses with HTTP 5xx; undefined when idle.",
            ),
            f"{_service}.latency_p50": Metric(
                _quantile("0.50", _service),
                "seconds",
                f"Approximate median {_service} /work response time (histogram estimate).",
            ),
            f"{_service}.latency_p95": Metric(
                _quantile("0.95", _service),
                "seconds",
                f"Approximate p95 {_service} /work response time (histogram estimate).",
            ),
            f"{_service}.slow_fraction": Metric(
                _slow_fraction(_service),
                "fraction",
                f"Fraction of {_service} /work responses slower than 250 ms.",
            ),
            f"{_service}.cpu": Metric(
                f'rate(process_cpu_seconds_total{{job="{_service}"}}[15s])',
                "cores",
                f"CPU cores used by the {_service} process (quota 0.5).",
            ),
            f"{_service}.rss": Metric(
                f'process_resident_memory_bytes{{job="{_service}"}}',
                "bytes",
                f"Resident memory of the {_service} process.",
            ),
        }
    )
for _outcome in OUTCOMES:
    METRICS[f"api.worker_calls.{_outcome}"] = Metric(
        f'sum(rate(lab_worker_calls_total{{job="api",outcome="{_outcome}"}}[30s]))',
        "calls/s",
        f"API calls to the worker per second with outcome {_outcome}.",
    )

# Firing alerts are derived from the same telemetry and contain no scenario labels.
ALERTS_QUERY = 'ALERTS{alertstate="firing"}'

# Tokens that must never appear in an investigator-visible query, prompt, or snapshot.
FORBIDDEN_TOKENS = (
    "fault_active",
    "network-fault-controller",
    "scenario",
    "run_id",
    "toxic",
    "ground_truth",
)
