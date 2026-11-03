import json
from datetime import timedelta

import pytest
from conftest import END, firing
from test_investigator import answer, turn
from test_rules_baseline import CPU, LATENCY, TIMEOUT

from agent.catalog import FORBIDDEN_TOKENS
from agent.llm_provider import ScriptedProvider
from evaluation import run as evaluation
from evaluation.scenarios import SCENARIOS
from evaluation.scoring import score_mode

TELEMETRY = {
    "healthy": ({}, []),
    "cpu_pressure": (CPU, [firing("WorkerCpuPressure")]),
    "network_latency": (LATENCY, [firing("ApiHighLatency")]),
    "network_connectivity_or_timeout": (TIMEOUT, [firing("WorkerCallTimeouts")]),
}
LABELS = ("cpu_pressure", "network_latency", "network_connectivity_or_timeout", '"healthy"')


class FakeLab:
    def __init__(self, bundle_for, fail_capture_for=()):
        self.bundle_for, self.fail_capture_for = bundle_for, fail_capture_for
        self.calls, self.current = [], None

    def sleep(self, seconds):
        pass

    def idle(self):
        return True

    def environment(self):
        return {"docker_version": "fake"}

    def start_load(self, seconds):
        self.calls.append("start_load")
        return "load"

    def stop_load(self, handle):
        self.calls.append("stop_load")

    def inject(self, scenario):
        self.calls.append(f"inject:{scenario.name}")
        return {"run_id": "r1", "started_at": (END - timedelta(seconds=40)).isoformat()}

    def stop(self, scenario):
        self.calls.append(f"stop:{scenario.name}")

    def active(self, scenario):
        return False

    def events(self, scenario, run_id):
        return [{"run_id": run_id, "event": "scenario_end", "reason": "duration_elapsed"}]

    def recovered(self, scenario):
        return True

    def capture(self, lookback, incident_id):
        self.current = incident_id
        for name in self.fail_capture_for:
            if name in self.calls[-1]:
                raise RuntimeError("Prometheus query failed")
        overrides, alerts = TELEMETRY[self._label()]
        bundle = self.bundle_for(overrides, alerts)
        return bundle.model_copy(update={"incident_id": incident_id})

    def _label(self):
        last = next((c for c in reversed(self.calls) if c.startswith("inject:")), None)
        if self.calls[-1] == "start_load" or last is None:
            return "healthy"
        return SCENARIOS[last.split(":")[1]].label


def run_all(tmp_path, lab, reps=2, modes=("rules",), provider=None):
    names = ["healthy", "cpu", "latency", "timeout"]
    trials = evaluation.run_trials(lab, names, reps, 7, tmp_path, evaluation.Settings())
    evaluation.diagnose_bundles(
        tmp_path / "bundles", tmp_path / "predictions", modes, provider=provider
    )
    return trials, evaluation.score_run(tmp_path)


@pytest.fixture(autouse=True)
def no_shared_artifacts(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)


def test_end_to_end_trials_with_rules(bundle_for, tmp_path):
    trials, scores = run_all(tmp_path, FakeLab(bundle_for))
    rules = scores["rules"]
    assert (rules["n_trials"], rules["n_fault"], rules["correct"]) == (8, 6, 6)
    assert (rules["n_healthy"], rules["false_incidents"], rules["n_failed_trials"]) == (2, 0, 0)
    assert rules["evidence_supported"] == 8
    latency = next(t for t in trials if t["scenario"] == "latency")
    assert latency["alert_detection_delay_s"] == pytest.approx(20)
    assert latency["ground_truth_events"][0]["reason"] == "duration_elapsed"
    assert (tmp_path / "summary.md").exists() and (tmp_path / "summary.csv").exists()
    assert (tmp_path / "artifacts" / "evaluation-summary.md").exists()
    summary = (tmp_path / "summary.md").read_text()
    assert "| rules | 6/6 |" in summary and "Caveats" in summary


def test_agent_visible_files_carry_no_labels(bundle_for, tmp_path):
    run_all(tmp_path, FakeLab(bundle_for), reps=1)
    for path in (tmp_path / "bundles").glob("*.json"):
        text = path.read_text()
        assert not any(label in text for label in LABELS), path
        assert not any(token in text for token in FORBIDDEN_TOKENS), path
        assert not any(name in path.name for name in SCENARIOS)
    truth = [json.loads(p.read_text()) for p in (tmp_path / "ground_truth").glob("*.json")]
    assert {t["label"] for t in truth} == {"healthy", *LABELS[:3]}


def test_order_is_randomized_reproducibly(bundle_for, tmp_path):
    first = [t["scenario"] for t in run_all(tmp_path / "a", FakeLab(bundle_for))[0]]
    second = [t["scenario"] for t in run_all(tmp_path / "b", FakeLab(bundle_for))[0]]
    assert first == second and first != sorted(first)


def test_failed_trials_are_kept_and_cleaned_up(bundle_for, tmp_path):
    lab = FakeLab(bundle_for, fail_capture_for=("inject:timeout",))
    trials, scores = run_all(tmp_path, lab)
    failed = [t for t in trials if t["status"] == "failed"]
    assert len(failed) == 2 and all("Prometheus query failed" in t["error"] for t in failed)
    assert lab.calls.count("stop:timeout") == 2 and lab.calls.count("stop_load") == 8
    assert scores["rules"]["n_failed_trials"] == 2 and scores["rules"]["n_trials"] == 8


def test_predictions_are_never_rewritten(bundle_for, tmp_path, monkeypatch):
    run_all(tmp_path, FakeLab(bundle_for), reps=1)
    before = {p.name: p.read_text() for p in (tmp_path / "predictions" / "rules").glob("*.json")}
    monkeypatch.setattr(evaluation, "rules_diagnose", lambda bundle: pytest.fail("re-ran"))
    evaluation.diagnose_bundles(tmp_path / "bundles", tmp_path / "predictions", ["rules"])
    after = {p.name: p.read_text() for p in (tmp_path / "predictions" / "rules").glob("*.json")}
    assert before == after


def test_llm_mode_runs_on_the_same_bundles(bundle_for, tmp_path):
    lab = FakeLab(bundle_for)
    run_all(tmp_path, lab, reps=1)
    bundles = sorted((tmp_path / "bundles").glob("*.json"))
    from agent.schemas import EvidenceBundle

    turns = [
        turn(
            answer(EvidenceBundle.model_validate_json(p.read_text()), cause="unknown_or_ambiguous")
        )
        for p in bundles
    ]
    evaluation.diagnose_bundles(
        tmp_path / "bundles",
        tmp_path / "predictions",
        ["llm_snapshot"],
        provider=ScriptedProvider(turns),
    )
    scores = evaluation.score_run(tmp_path)
    llm = scores["llm_snapshot"]
    assert llm["n_scored"] == 4 and llm["abstained"] == 3 and llm["healthy_abstained"] == 1
    assert llm["input_tokens"] == 4 * 1200 and llm["estimated_cost_usd"] > 0
    assert scores["rules"]["n_scored"] == 4  # both modes scored on identical bundles


def test_scoring_keeps_abstentions_and_denominators_separate():
    rows = [
        {"label": "network_latency", "predicted": "network_latency"},
        {"label": "network_latency", "predicted": "unknown_or_ambiguous"},
        {"label": "cpu_pressure", "predicted": "network_latency"},
        {"label": "healthy", "predicted": "cpu_pressure"},
        {"label": "healthy", "predicted": "unknown_or_ambiguous"},
        {"label": "cpu_pressure", "predicted": None, "status": "failed"},
    ]
    for row in rows:
        row.setdefault("status", "captured")
        row.update(mode="rules", evidence_count=1, citation_problems=[], latency_s=0.01)
    result = score_mode(rows)
    assert (result["n_trials"], result["n_scored"], result["n_failed_trials"]) == (6, 5, 1)
    assert (result["correct"], result["abstained"], result["wrong_cause"]) == (1, 1, 1)
    assert result["root_cause_accuracy"] == pytest.approx(1 / 3)
    assert (result["false_incidents"], result["healthy_abstained"]) == (1, 1)
    assert score_mode([])["root_cause_accuracy"] is None
