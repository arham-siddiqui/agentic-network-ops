import asyncio
import hashlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, ValidationError

from lab.fault import SCENARIO_PATH, SOCKET_PATH, CpuFault, serve_control
from lab.metrics import Metrics, RequestMetricsMiddleware


class WorkResult(BaseModel):
    checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    iterations: int = Field(ge=1, le=1000)


def create_app(
    service: str,
    *,
    worker_url: str = "http://worker:8000",
    transport: httpx.AsyncBaseTransport | None = None,
    enable_fault: bool = False,
) -> FastAPI:
    if service not in {"api", "worker"}:
        raise ValueError("SERVICE must be api or worker")
    metrics = Metrics()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            base_url=worker_url,
            timeout=httpx.Timeout(2, connect=0.5),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            app.state.client = client
            controller = None
            server = None
            if enable_fault:
                if service != "worker" or not Path("/.dockerenv").exists():
                    raise RuntimeError("Fault control requires this lab's Docker worker")
                controller = CpuFault(metrics, Path(SCENARIO_PATH))
                server = await serve_control(controller)
            try:
                yield
            finally:
                if server is not None:
                    server.close()
                    await server.wait_closed()
                    await asyncio.to_thread(controller.stop)
                    Path(SOCKET_PATH).unlink(missing_ok=True)

    app = FastAPI(
        title=f"Lab {service}", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.metrics = metrics
    app.add_middleware(RequestMetricsMiddleware, metrics=metrics, service=service)

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": service}

    @app.get("/metrics")
    async def expose_metrics():
        return Response(
            generate_latest(metrics.registry), headers={"Content-Type": CONTENT_TYPE_LATEST}
        )

    if service == "worker":

        @app.get("/work", response_model=WorkResult)
        async def work():
            digest = b"local-lab-work"
            for _ in range(250):
                digest = hashlib.sha256(digest).digest()
            return WorkResult(checksum=digest.hex(), iterations=250)
    else:

        @app.get("/work", response_model=WorkResult)
        async def dispatch():
            try:
                response = await app.state.client.get("/work")
                response.raise_for_status()
            except httpx.TimeoutException as exc:
                metrics.dependency.labels("timeout").inc()
                raise HTTPException(504, "worker request timed out") from exc
            except httpx.HTTPError as exc:
                metrics.dependency.labels("error").inc()
                raise HTTPException(502, "worker unavailable") from exc
            try:
                result = WorkResult.model_validate(response.json())
            except (ValueError, ValidationError) as exc:
                metrics.dependency.labels("invalid_response").inc()
                raise HTTPException(502, "worker returned an invalid response") from exc
            metrics.dependency.labels("success").inc()
            return result

    return app


def from_environment() -> FastAPI:
    return create_app(
        os.getenv("SERVICE", "api"),
        worker_url=os.getenv("WORKER_URL", "http://worker:8000"),
        enable_fault=os.getenv("ENABLE_FAULT_CONTROL", "0") == "1",
    )
