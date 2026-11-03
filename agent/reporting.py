"""Building incident reports from cited evidence, and checking citations against the bundle."""

import math

from agent.schemas import Evidence, EvidenceBundle, IncidentReport
from agent.telemetry import RECENT_SECONDS, Summary, summarize


class ReportBuilder:
    def __init__(self, bundle: EvidenceBundle, mode: str, summaries: dict[str, Summary]) -> None:
        self.bundle, self.mode, self.summaries = bundle, mode, summaries
        self.evidence: dict[tuple[str, str], Evidence] = {}
        self.limitations: list[str] = []
        self.impact = "Unknown."

    def cite(self, metric_id: str, statistic: str = "recent_mean") -> float | None:
        summary = self.summaries[metric_id]
        value = getattr(summary, statistic)
        self.evidence.setdefault(
            (metric_id, statistic),
            Evidence(
                metric_id=metric_id,
                lookback_seconds=RECENT_SECONDS
                if statistic.startswith("recent")
                else max(10, summary.lookback_seconds),
                statistic=statistic,
                observed_value=value,
                units=summary.unit,
            ),
        )
        return value

    def limit(self, text: str) -> None:
        self.limitations.append(text)

    def finish(self, cause: str, summary: str, actions: dict[str, str]) -> IncidentReport:
        return IncidentReport(
            incident_id=self.bundle.incident_id,
            mode=self.mode,
            time_window_utc=self.bundle.window,
            predicted_cause=cause,
            summary=summary,
            evidence=list(self.evidence.values())[:20],
            business_impact=self.impact,
            recommended_action=actions[cause],
            needs_human_review=cause != "healthy",
            limitations=self.limitations[:10],
        )


def check_evidence(report: IncidentReport, bundle: EvidenceBundle) -> list[str]:
    """Problems with a report's citations: unknown metrics, or values the bundle does not show."""
    problems = []
    for item in report.evidence:
        if item.metric_id not in bundle.series:
            problems.append(f"{item.metric_id}: not in the evidence bundle")
            continue
        lookback = None if not item.statistic.startswith("window") else item.lookback_seconds
        actual = getattr(summarize(bundle, item.metric_id, lookback), item.statistic)
        if actual is None or item.observed_value is None:
            if actual != item.observed_value:
                problems.append(f"{item.metric_id}: cited {item.observed_value}, bundle {actual}")
        elif not math.isclose(actual, item.observed_value, rel_tol=0.05, abs_tol=0.005):
            problems.append(f"{item.metric_id}: cited {item.observed_value}, bundle {actual:.4g}")
        if item.units != bundle.series[item.metric_id].unit:
            problems.append(f"{item.metric_id}: units {item.units!r} do not match")
    return problems
