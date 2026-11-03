import json

import pytest
from conftest import firing

from agent.reporting import check_evidence
from agent.rules_baseline import diagnose

CPU = {"worker.cpu": 0.46, "api.slow_fraction": 0.08, "api.latency_p95": 0.21}
LATENCY = {
    "api.slow_fraction": 1.0,
    "api.latency_p50": 0.75,
    "api.latency_p95": 0.97,
    "worker.slow_fraction": 0.0,
}
# Upstream bytes dropped: the worker sees no requests, so its rate is 0 and latency undefined.
TIMEOUT = {
    "api.request_rate": 1.9,
    "api.error_fraction": 1.0,
    "api.slow_fraction": 1.0,
    "api.latency_p95": 1.95,
    "api.worker_calls.success": 0.0,
    "api.worker_calls.timeout": 1.9,
    "worker.request_rate": 0.0,
    "worker.slow_fraction": None,
    "worker.latency_p95": None,
    "worker.error_fraction": None,
}


@pytest.mark.parametrize(
    ("overrides", "cause"),
    [
        ({}, "healthy"),
        (CPU, "cpu_pressure"),
        (LATENCY, "network_latency"),
        (TIMEOUT, "network_connectivity_or_timeout"),
        (
            {"api.worker_calls.success": 2.0, "api.worker_calls.error": 7.8},
            "network_connectivity_or_timeout",
        ),
        ({"worker.up": 0, "api.worker_calls.error": 9.8}, "service_unavailable"),
        ({"proxy.up": 0}, "service_unavailable"),
        ({"api.up": 0}, "service_unavailable"),
    ],
)
def test_modeled_causes(bundle_for, overrides, cause):
    bundle = bundle_for(overrides)
    report = diagnose(bundle)
    assert report.predicted_cause == cause
    assert report.action_taken is False and report.mode == "rules"
    assert report.confidence is None  # rules make no calibrated-confidence claim
    assert report.needs_human_review is (cause != "healthy")
    assert report.evidence and check_evidence(report, bundle) == []


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"api.up": "absent"}, "missing"),
        ({"proxy.up": "error"}, "missing"),
        ({"worker.up": [(60, 1), (55, 1), (50, 1)]}, "missing"),  # stale: stopped 50 s ago
        ({"api.request_rate": [(25, 9.8), (5, 9.8)]}, "missing"),  # sparse: two recent samples
        ({"api.request_rate": 0.0, "api.worker_calls.success": 0.0}, "traffic"),
        ({**LATENCY, "worker.cpu": "absent"}, "CPU"),
        ({"api.slow_fraction": None, "api.latency_p95": None}, "latency"),
        ({**LATENCY, "worker.slow_fraction": None}, "locate"),
        ({**LATENCY, "worker.slow_fraction": 0.9}, "worker itself"),
        ({**TIMEOUT, "worker.cpu": 0.45}, "CPU pressure is also present"),
    ],
)
def test_fails_closed_when_evidence_is_missing_or_conflicting(bundle_for, overrides, reason):
    report = diagnose(bundle_for(overrides))
    assert report.predicted_cause == "unknown_or_ambiguous"
    assert any(reason in text for text in report.limitations), report.limitations


def test_prometheus_unavailable_is_not_healthy(bundle_for):
    bundle = bundle_for(down=True)
    assert all(series.status == "unavailable" for series in bundle.series.values())
    assert bundle.alerts_status == "unavailable"
    report = diagnose(bundle)
    assert report.predicted_cause == "unknown_or_ambiguous"
    assert "Prometheus was unavailable" in report.limitations[0]


def test_cpu_with_slow_api_but_fast_worker_records_overlap(bundle_for):
    report = diagnose(bundle_for({**LATENCY, "worker.cpu": 0.45}))
    assert report.predicted_cause == "cpu_pressure"
    assert any("network delay is not excluded" in text for text in report.limitations)


def test_impact_reports_unknown_rather_than_zero(bundle_for):
    report = diagnose(bundle_for({"api.slow_fraction": None, "api.latency_p95": None}))
    assert "unknown of responses" in report.business_impact
    assert "p95 unknown" in report.business_impact


def test_alerts_are_corroborating_only(bundle_for):
    # A firing alert alone does not create a diagnosis; telemetry decides.
    report = diagnose(bundle_for(alerts=[firing("WorkerCpuPressure")]))
    assert report.predicted_cause == "healthy"


def test_tampered_or_invented_evidence_is_detected(bundle_for):
    bundle = bundle_for(LATENCY)
    report = diagnose(bundle)
    tampered = report.model_copy(
        update={
            "evidence": [
                report.evidence[0].model_copy(update={"observed_value": 123.0}),
                report.evidence[1].model_copy(update={"metric_id": "worker.queue_depth"}),
            ]
        }
    )
    problems = check_evidence(tampered, bundle)
    assert len(problems) == 2 and "not in the evidence bundle" in problems[1]


def test_report_round_trips_as_json(bundle_for):
    report = diagnose(bundle_for(LATENCY))
    data = json.loads(report.model_dump_json())
    assert data["schema_version"] == "1" and data["predicted_cause"] == "network_latency"
    assert data["action_taken"] is False


def test_cli_rediagnoses_a_saved_bundle(bundle_for, tmp_path, monkeypatch, capsys):
    from agent import cli

    bundle = bundle_for(LATENCY)
    path = tmp_path / f"{bundle.incident_id}-bundle.json"
    path.write_text(bundle.model_dump_json())
    monkeypatch.setattr("sys.argv", ["cli", "diagnose", str(path)])
    cli.main()
    saved = json.loads((tmp_path / f"{bundle.incident_id}-report-rules.json").read_text())
    assert saved["predicted_cause"] == "network_latency"
    assert "[rules] network_latency" in capsys.readouterr().out
