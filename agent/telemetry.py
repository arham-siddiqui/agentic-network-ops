"""Read-only Prometheus access limited to the catalogue, and bounded window summaries.

Live data is captured once into an EvidenceBundle; every diagnosis mode then reads the same
bundle, so modes are compared on identical evidence.
"""

import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from agent.catalog import ALERTS_QUERY, METRICS
from agent.schemas import AlertObservation, EvidenceBundle, Series, TimeWindow

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MIN_LOOKBACK, MAX_LOOKBACK = 60, 900
DEFAULT_STEP = 5
RECENT_SECONDS = 30
MIN_RECENT_SAMPLES = 3
SEVERITIES = {"warning", "critical", "info"}
ALERT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


class PrometheusUnavailable(RuntimeError):
    pass


class PrometheusSource:
    """Range queries for catalogue entries only; there is no method that accepts PromQL."""

    def __init__(self, base_url: str = "http://127.0.0.1:9090", transport=None) -> None:
        url = httpx.URL(base_url)
        if url.scheme != "http" or url.host not in LOOPBACK_HOSTS:
            raise ValueError("the investigator only reads the lab Prometheus on a loopback address")
        self.client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(5, connect=1),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def _range(self, promql: str, start: float, end: float, step: float) -> list[dict]:
        try:
            response = self.client.get(
                "/api/v1/query_range",
                params={"query": promql, "start": start, "end": end, "step": step},
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PrometheusUnavailable(f"Prometheus query failed: {exc}") from exc
        if data.get("status") != "success" or data.get("data", {}).get("resultType") != "matrix":
            raise PrometheusUnavailable("Prometheus returned an unexpected response")
        return data["data"]["result"]

    def metric(self, metric_id: str, start: float, end: float, step: float) -> list[dict]:
        return self._range(METRICS[metric_id].promql, start, end, step)

    def alerts(self, start: float, end: float, step: float) -> list[dict]:
        return self._range(ALERTS_QUERY, start, end, step)


def _points(values: list) -> list[tuple[float, float | None]]:
    points = []
    for timestamp, raw in values:
        value = float(raw)
        points.append((float(timestamp), value if math.isfinite(value) else None))
    return points


def _series(metric_id: str, raw: list[dict], end: float, step: float) -> Series:
    unit = METRICS[metric_id].unit
    if not raw:
        return Series(metric_id=metric_id, unit=unit, status="no_data")
    if len(raw) != 1:
        return Series(
            metric_id=metric_id, unit=unit, status="unavailable", error="expected one series"
        )
    points = _points(raw[0].get("values", []))
    numeric = [t for t, v in points if v is not None]
    if not numeric:
        status = "no_data"
    elif numeric[-1] < end - max(15, 3 * step):
        status = "stale"
    else:
        status = "ok"
    return Series(metric_id=metric_id, unit=unit, status=status, points=points)


def _alerts(raw: list[dict]) -> list[AlertObservation]:
    observed: dict[tuple[str, str], list[float]] = {}
    for item in raw:
        labels = item.get("metric", {})
        name = labels.get("alertname", "")
        severity = labels.get("severity", "unknown")
        times = [float(t) for t, v in item.get("values", []) if float(v) == 1]
        if not times or not ALERT_NAME.fullmatch(name):
            continue  # Label text is untrusted; keep only well-formed alert names.
        key = (name, severity if severity in SEVERITIES else "unknown")
        span = observed.setdefault(key, [times[0], times[-1]])
        span[0], span[1] = min(span[0], times[0]), max(span[1], times[-1])
    return [
        AlertObservation(alertname=n, severity=s, first_seen=a, last_seen=b)
        for (n, s), (a, b) in sorted(observed.items())
    ][:50]


def capture(
    source: PrometheusSource,
    end: datetime | None = None,
    lookback_seconds: int = 120,
    step: float = DEFAULT_STEP,
    incident_id: str | None = None,
) -> EvidenceBundle:
    if not MIN_LOOKBACK <= lookback_seconds <= MAX_LOOKBACK:
        raise ValueError(f"lookback must be {MIN_LOOKBACK}-{MAX_LOOKBACK} seconds")
    end = end or datetime.now(UTC)
    start = end - timedelta(seconds=lookback_seconds)
    s, e = start.timestamp(), end.timestamp()
    series = {}
    for metric_id in METRICS:
        try:
            series[metric_id] = _series(metric_id, source.metric(metric_id, s, e, step), e, step)
        except PrometheusUnavailable as exc:
            series[metric_id] = Series(
                metric_id=metric_id,
                unit=METRICS[metric_id].unit,
                status="unavailable",
                error=str(exc)[:200],
            )
    try:
        alerts, alerts_status = _alerts(source.alerts(s, e, step)), "ok"
    except PrometheusUnavailable:
        alerts, alerts_status = [], "unavailable"
    return EvidenceBundle(
        incident_id=incident_id or uuid.uuid4().hex[:16],
        window=TimeWindow(start=start, end=end),
        step_seconds=step,
        series=series,
        alerts=alerts,
        alerts_status=alerts_status,
    )


@dataclass(frozen=True)
class Summary:
    metric_id: str
    unit: str
    status: str
    lookback_seconds: int
    samples: int
    recent_mean: float | None
    recent_max: float | None
    window_min: float | None
    window_max: float | None
    last: float | None

    @property
    def usable(self) -> bool:
        return self.status == "ok" and self.recent_mean is not None


def summarize(bundle: EvidenceBundle, metric_id: str, lookback_seconds: int | None = None):
    """Statistics over the trailing lookback; fewer than 3 recent samples means no data."""
    series = bundle.series[metric_id]
    end = bundle.window.end.timestamp()
    full = int(round(end - bundle.window.start.timestamp()))
    lookback = full if lookback_seconds is None else min(lookback_seconds, full)
    values = [(t, v) for t, v in series.points if v is not None and t > end - lookback]
    recent = [v for t, v in values if t > end - RECENT_SECONDS]
    status = series.status
    if status == "ok" and len(recent) < MIN_RECENT_SAMPLES:
        status = "no_data"
    usable = status == "ok"
    return Summary(
        metric_id=metric_id,
        unit=series.unit,
        status=status,
        lookback_seconds=lookback,
        samples=len(values),
        recent_mean=sum(recent) / len(recent) if usable else None,
        recent_max=max(recent) if usable else None,
        window_min=min(v for _, v in values) if values else None,
        window_max=max(v for _, v in values) if values else None,
        last=values[-1][1] if values else None,
    )
