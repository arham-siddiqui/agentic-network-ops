"""Read-only checks from inside Compose; generates 30 small local work requests."""

import json
import math
import os
import time

import httpx


def wait_until(check, *, timeout=60):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (httpx.HTTPError, ValueError, KeyError, AssertionError) as exc:
            last_error = exc
        time.sleep(1)
    raise RuntimeError(f"Smoke check timed out: {last_error or check.__name__}")


def main():
    with httpx.Client(timeout=5, trust_env=False) as client:

        def get(url, **kwargs):
            response = client.get(url, **kwargs)
            response.raise_for_status()
            return response.json()

        def query(expr):
            data = get("http://prometheus:9090/api/v1/query", params={"query": expr})
            assert data["status"] == "success"
            return data["data"]["result"]

        for service in ("api", "worker"):
            wait_until(lambda s=service: get(f"http://{s}:8000/health")["status"] == "ok")
        wait_until(lambda: len(query('up{job=~"api|worker"} == 1')) == 2)
        control_up = 'up{job=~"impairment-proxy|network-fault-controller"} == 1'
        wait_until(lambda: len(query(control_up)) == 2)

        def unreachable(url):
            try:
                client.get(url, timeout=2)
            except httpx.TransportError:
                return True
            return False

        # The lab network (where this container, the API, and the worker live) must not reach
        # the proxy admin API or the network fault controller.
        for url in (
            "http://impairment-proxy:8474/proxies",
            "http://proxy-admin:8474/proxies",
            "http://network-fault-controller:9101/metrics",
        ):
            assert unreachable(url), f"fault-control endpoint reachable from lab network: {url}"
        get("http://api:8000/work")
        wait_until(lambda: len(query('lab_http_requests_total{route="/work",status="200"}')) == 2)
        for _ in range(30):
            result = get("http://api:8000/work")
            assert result["iterations"] == 250 and len(result["checksum"]) == 64
            time.sleep(0.1)
        for expr in (
            'rate(process_cpu_seconds_total{job=~"api|worker"}[15s])',
            'process_resident_memory_bytes{job=~"api|worker"}',
            "histogram_quantile(0.95, sum by (job, le) "
            '(rate(lab_http_request_duration_seconds_bucket{route="/work"}[30s])))',
            # Healthy-mode traffic really traverses the proxy in both directions.
            "sum by (direction) "
            '(rate(toxiproxy_proxy_sent_bytes_total{proxy="api_to_worker"}[30s])) > 0',
        ):

            def measured(expression=expr):
                values = query(expression)
                return len(values) == 2 and all(math.isfinite(float(x["value"][1])) for x in values)

            wait_until(measured)
        auth = ("admin", os.getenv("GRAFANA_ADMIN_PASSWORD", "local-lab-only"))
        dashboard = wait_until(
            lambda: get(
                "http://grafana:3000/api/dashboards/uid/local-lab",
                auth=auth,
            )
        )
        assert len(dashboard["dashboard"]["panels"]) == 12
        wait_until(
            lambda: (
                get(
                    "http://grafana:3000/api/datasources/uid/prometheus/health",
                    auth=auth,
                )["status"]
                == "OK"
            )
        )
        print(
            json.dumps(
                {
                    "result": "passed",
                    "services_up": 2,
                    "dashboard_uid": "local-lab",
                    "process_metrics": True,
                    "proxy_path": True,
                    "fault_control_isolated": True,
                }
            )
        )


if __name__ == "__main__":
    main()
