"""Bounded HTTP labels and Linux process metrics, including all process threads."""

import json
import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, ProcessCollector


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        ProcessCollector(registry=self.registry)
        self.requests = Counter(
            "lab_http_requests_total",
            "Completed HTTP requests; excludes health and metrics.",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "lab_http_request_duration_seconds",
            "HTTP response completion latency.",
            ["method", "route"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
            registry=self.registry,
        )
        self.dependency = Counter(
            "lab_worker_calls_total",
            "API calls to worker, including failures.",
            ["outcome"],
            registry=self.registry,
        )
        for outcome in ("success", "timeout", "error", "invalid_response"):
            self.dependency.labels(outcome)
        self.fault_active = Gauge(
            "lab_cpu_fault_active",
            "Ground-truth marker only: 1 while CPU fault runs.",
            registry=self.registry,
        )


class RequestMetricsMiddleware:
    def __init__(self, app, metrics: Metrics, service: str) -> None:
        self.app, self.metrics, self.service = app, metrics, service

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] in {"/health", "/metrics"}:
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            elapsed = time.perf_counter() - started
            route = getattr(scope.get("route"), "path", "unmatched")
            method = scope["method"]
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                method = "OTHER"
            self.metrics.requests.labels(method, route, str(status)).inc()
            self.metrics.latency.labels(method, route).observe(elapsed)
            print(
                json.dumps(
                    {
                        "event": "http_request",
                        "service": self.service,
                        "timestamp": time.time(),
                        "method": method,
                        "route": route,
                        "status": status,
                        "duration_seconds": elapsed,
                    }
                ),
                flush=True,
            )
