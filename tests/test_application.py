import httpx
import pytest
from fastapi.testclient import TestClient

from lab.application import create_app


def test_api_calls_real_worker_application():
    worker = create_app("worker")
    api = create_app("api", transport=httpx.ASGITransport(app=worker))
    with TestClient(api) as client:
        assert client.get("/health").json() == {"status": "ok", "service": "api"}
        result = client.get("/work")
        assert result.status_code == 200
        assert result.json()["iterations"] == 250
        assert len(result.json()["checksum"]) == 64
        metrics = client.get("/metrics").text
        assert 'lab_worker_calls_total{outcome="success"} 1.0' in metrics
        assert 'lab_http_requests_total{method="GET",route="/work",status="200"} 1.0' in metrics
        assert 'lab_http_request_duration_seconds_count{method="GET",route="/work"} 1.0' in metrics
        assert 'route="/health"' not in metrics and 'route="/metrics"' not in metrics
    assert (
        worker.state.metrics.registry.get_sample_value(
            "lab_http_requests_total",
            {"method": "GET", "route": "/work", "status": "200"},
        )
        == 1
    )


@pytest.mark.parametrize(
    ("kind", "status", "outcome"),
    [
        ("timeout", 504, "timeout"),
        ("connect", 502, "error"),
        ("http500", 502, "error"),
        ("redirect", 502, "error"),
        ("invalid_json", 502, "invalid_response"),
        ("invalid_schema", 502, "invalid_response"),
    ],
)
def test_dependency_failures_are_visible(kind, status, outcome):
    def downstream(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if kind == "connect":
            raise httpx.ConnectError("refused", request=request)
        if kind == "http500":
            return httpx.Response(500)
        if kind == "redirect":
            return httpx.Response(302, headers={"location": "https://example.invalid"})
        if kind == "invalid_json":
            return httpx.Response(200, content=b"not JSON")
        return httpx.Response(200, json={"checksum": "invalid", "iterations": 0})

    with TestClient(create_app("api", transport=httpx.MockTransport(downstream))) as client:
        assert client.get("/work").status_code == status
        metrics = client.get("/metrics").text
        assert f'lab_worker_calls_total{{outcome="{outcome}"}} 1.0' in metrics
        assert f'route="/work",status="{status}"' in metrics


def test_unknown_routes_and_methods_have_bounded_labels():
    with TestClient(create_app("worker")) as client:
        for path in ("/unknown/private-value", "/another-value"):
            assert client.request("CUSTOM", path).status_code == 404
        metrics = client.get("/metrics").text
        assert 'method="OTHER",route="unmatched",status="404"} 2.0' in metrics
        assert "private-value" not in metrics and "another-value" not in metrics


def test_unhandled_error_is_counted():
    app = create_app("worker")

    @app.get("/crash")
    async def crash():
        raise RuntimeError("test failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/crash").status_code == 500
        assert 'route="/crash",status="500"} 1.0' in client.get("/metrics").text


def test_fault_control_is_not_an_http_endpoint():
    with TestClient(create_app("worker")) as client:
        assert client.post("/fault/start").status_code == 404
