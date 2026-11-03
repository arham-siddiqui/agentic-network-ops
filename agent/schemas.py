"""Versioned contracts for agent-visible evidence and incident reports."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Cause = Literal[
    "healthy",
    "cpu_pressure",
    "network_latency",
    "network_connectivity_or_timeout",
    "service_unavailable",
    "unknown_or_ambiguous",
]
CAUSES: tuple[str, ...] = Cause.__args__
Mode = Literal["rules", "llm_snapshot", "llm_tools"]
SeriesStatus = Literal["ok", "no_data", "stale", "unavailable"]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
AlertName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimeWindow(Strict):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("window end must be after start")
        return self


class Series(Strict):
    """Raw points for one allowlisted metric; None marks NaN (undefined), never zero."""

    metric_id: str
    unit: str
    status: SeriesStatus
    points: list[tuple[float, float | None]] = Field(default_factory=list, max_length=400)
    error: str | None = None


class AlertObservation(Strict):
    alertname: AlertName
    severity: Literal["warning", "critical", "info", "unknown"]
    first_seen: float
    last_seen: float


class EvidenceBundle(Strict):
    """Everything an investigator may see for one incident window, and nothing else."""

    schema_version: Literal["1"] = "1"
    incident_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{12,32}$")]
    window: TimeWindow
    step_seconds: float = Field(gt=0, le=60)
    series: dict[str, Series]
    alerts: list[AlertObservation] = Field(default_factory=list, max_length=50)
    alerts_status: SeriesStatus = "ok"


class Evidence(Strict):
    metric_id: str
    lookback_seconds: int = Field(ge=10, le=900)
    statistic: Literal["recent_mean", "recent_max", "window_max", "window_min", "last"]
    observed_value: float | None
    units: str
    source: Literal["prometheus"] = "prometheus"


class IncidentReport(Strict):
    schema_version: Literal["1"] = "1"
    incident_id: str
    mode: Mode
    time_window_utc: TimeWindow
    predicted_cause: Cause
    # An uncalibrated estimate when present; rules leave it empty.
    confidence: float | None = Field(default=None, ge=0, le=1)
    summary: ShortText
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)
    business_impact: ShortText
    recommended_action: ShortText
    action_taken: Literal[False] = False
    needs_human_review: bool = True
    limitations: list[ShortText] = Field(default_factory=list, max_length=10)
