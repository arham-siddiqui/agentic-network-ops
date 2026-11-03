"""Scoring saved predictions against labels. Pure functions over plain records.

Every trial counts: failed captures and abstentions stay in the denominators they belong to,
and every rate is reported next to its counts.
"""

import statistics

HEALTHY, UNKNOWN = "healthy", "unknown_or_ambiguous"


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def score_mode(records: list[dict]) -> dict:
    """records: one per trial for a single mode, with label, status, and prediction fields."""
    scored = [r for r in records if r["status"] == "captured" and r.get("predicted")]
    faults = [r for r in scored if r["label"] != HEALTHY]
    healthy = [r for r in scored if r["label"] == HEALTHY]
    correct = sum(r["predicted"] == r["label"] for r in faults)
    abstained = sum(r["predicted"] == UNKNOWN for r in faults)
    false_incidents = sum(r["predicted"] not in (HEALTHY, UNKNOWN) for r in healthy)
    supported = sum(r["evidence_count"] > 0 and not r["citation_problems"] for r in scored)
    costs = [r["cost_usd"] for r in scored if r.get("cost_usd") is not None]
    per_class: dict[str, dict] = {}
    for r in scored:
        entry = per_class.setdefault(r["label"], {"n": 0, "correct": 0, "predicted": {}})
        entry["n"] += 1
        entry["correct"] += r["predicted"] == r["label"]
        entry["predicted"][r["predicted"]] = entry["predicted"].get(r["predicted"], 0) + 1
    return {
        "n_trials": len(records),
        "n_scored": len(scored),
        "n_failed_trials": len(records) - len(scored),
        "n_fault": len(faults),
        "correct": correct,
        "root_cause_accuracy": _rate(correct, len(faults)),
        "abstained": abstained,
        "abstention_rate": _rate(abstained, len(faults)),
        "wrong_cause": len(faults) - correct - abstained,
        "n_healthy": len(healthy),
        "false_incidents": false_incidents,
        "false_incident_rate": _rate(false_incidents, len(healthy)),
        "healthy_abstained": sum(r["predicted"] == UNKNOWN for r in healthy),
        "evidence_supported": supported,
        "evidence_support_rate": _rate(supported, len(scored)),
        "median_diagnosis_seconds": _median([r["latency_s"] for r in scored]),
        "input_tokens": sum(r.get("input_tokens") or 0 for r in scored),
        "output_tokens": sum(r.get("output_tokens") or 0 for r in scored),
        "estimated_cost_usd": sum(costs) if costs else None,
        "per_class": per_class,
    }


def score(records: list[dict]) -> dict[str, dict]:
    modes = sorted({r["mode"] for r in records})
    return {mode: score_mode([r for r in records if r["mode"] == mode]) for mode in modes}


def infrastructure(trials: list[dict]) -> dict[str, dict]:
    """Per-scenario alert detection and recovery observations; independent of any investigator."""
    result: dict[str, dict] = {}
    for trial in trials:
        entry = result.setdefault(
            trial["scenario"],
            {"n": 0, "captured": 0, "alert_fired": 0, "detection_s": [], "recovery_s": []},
        )
        entry["n"] += 1
        entry["captured"] += trial["status"] == "captured"
        if trial.get("alert_detection_delay_s") is not None:
            entry["alert_fired"] += 1
            entry["detection_s"].append(trial["alert_detection_delay_s"])
        if trial.get("recovery_s") is not None:
            entry["recovery_s"].append(trial["recovery_s"])
    for entry in result.values():
        entry["median_detection_s"] = _median(entry.pop("detection_s"))
        entry["median_recovery_s"] = _median(entry.pop("recovery_s"))
    return result
