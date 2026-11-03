import json
from types import SimpleNamespace

import pytest
from conftest import firing
from test_rules_baseline import LATENCY, TIMEOUT

from agent.catalog import FORBIDDEN_TOKENS
from agent.investigator import (
    SYSTEM_PROMPT,
    TOOLS,
    investigate_snapshot,
    investigate_with_tools,
    snapshot,
)
from agent.llm_provider import ProviderError, ScriptedProvider, ToolCall, Turn
from agent.telemetry import summarize


def answer(
    bundle, cause="network_latency", cited=("api.slow_fraction", "worker.slow_fraction"), **kw
):
    body = {
        "predicted_cause": cause,
        "confidence": 0.7,
        "summary": "API responses are slow while worker-side latency is normal.",
        "evidence": [
            {
                "metric_id": m,
                "statistic": "recent_mean",
                "lookback_seconds": 30,
                "observed_value": summarize(bundle, m).recent_mean,
            }
            for m in cited
        ],
        "business_impact": "Most /work responses exceed 250 ms; no errors.",
        "recommended_action": "Inspect the API-to-worker path.",
        "limitations": [],
        **kw,
    }
    return json.dumps(body)


def turn(text=None, calls=(), stop="end_turn", tokens=(1200, 300)):
    return Turn(text, list(calls), stop, tokens[0], tokens[1], "claude-opus-5-5")


def call(name, n=0, **args):
    return ToolCall(f"toolu_{name}_{n}", name, args)


def test_snapshot_mode_produces_grounded_report(bundle_for):
    bundle = bundle_for(LATENCY)
    provider = ScriptedProvider([turn(answer(bundle))])
    report, info = investigate_snapshot(bundle, provider)
    assert report.predicted_cause == "network_latency" and report.mode == "llm_snapshot"
    assert report.needs_human_review and report.action_taken is False
    assert info["schema_valid"] and info["outcome"] == "answered"
    assert info["citation_problems"] == [] and report.limitations == []
    assert (info["input_tokens"], info["output_tokens"]) == (1200, 300)
    assert info["estimated_cost_usd"] == pytest.approx((1200 * 4 + 300 * 20) / 1e6)
    assert "prompt" not in json.dumps(info)  # metadata only, never prompt text


def test_prompts_contain_only_allowlisted_data(bundle_for):
    bundle = bundle_for(TIMEOUT, alerts=[firing("WorkerCallTimeouts")])
    provider = ScriptedProvider([turn(answer(bundle)), turn(answer(bundle))])
    investigate_snapshot(bundle, provider)
    investigate_with_tools(bundle, provider)
    for sent in provider.prompts:
        text = json.dumps(sent)
        assert [t for t in FORBIDDEN_TOKENS if t in text] == []
    # The tool-mode prompt starts from alerts only, not the full snapshot.
    assert "api.slow_fraction" not in provider.prompts[1]["prompt"]
    assert "WorkerCallTimeouts" in provider.prompts[1]["prompt"]


@pytest.mark.parametrize(
    "text",
    [
        None,
        "not json",
        '{"predicted_cause": "network_latency"}',
        json.dumps({"predicted_cause": "packet_loss"}),
    ],
)
def test_invalid_model_output_fails_closed(bundle_for, text):
    bundle = bundle_for(LATENCY)
    report, info = investigate_snapshot(bundle, ScriptedProvider([turn(text)]))
    assert report.predicted_cause == "unknown_or_ambiguous"
    assert info["outcome"] == "invalid_output" and not info["schema_valid"]


def test_out_of_range_fields_fail_closed(bundle_for):
    bundle = bundle_for(LATENCY)
    report, _ = investigate_snapshot(bundle, ScriptedProvider([turn(answer(bundle, confidence=2))]))
    assert report.predicted_cause == "unknown_or_ambiguous"


def test_refusal_and_provider_errors_are_abstentions(bundle_for):
    bundle = bundle_for(LATENCY)
    report, info = investigate_snapshot(bundle, ScriptedProvider([turn(stop="refusal")]))
    assert report.predicted_cause == "unknown_or_ambiguous" and info["outcome"] == "refused"
    report, info = investigate_snapshot(bundle, ScriptedProvider([ProviderError("unreachable")]))
    assert report.predicted_cause == "unknown_or_ambiguous" and info["outcome"] == "error"
    assert "unreachable" in report.limitations[0]


def test_wrong_cited_values_are_flagged_not_trusted(bundle_for):
    bundle = bundle_for(LATENCY)
    body = json.loads(answer(bundle))
    body["evidence"][0]["observed_value"] = 0.01  # telemetry says 1.0
    report, info = investigate_snapshot(bundle, ScriptedProvider([turn(json.dumps(body))]))
    assert report.predicted_cause == "network_latency"  # the claim is kept, but marked
    assert len(info["citation_problems"]) == 1
    assert "do not match the telemetry" in report.limitations[-1]


def test_snapshot_marks_undefined_values_as_null(bundle_for):
    data = snapshot(bundle_for(TIMEOUT))
    worker = next(m for m in data["metrics"] if m["metric_id"] == "worker.slow_fraction")
    assert worker["status"] == "no_data" and worker["recent_mean"] is None


def test_tool_mode_investigates_then_answers(bundle_for):
    bundle = bundle_for(LATENCY)
    provider = ScriptedProvider(
        [
            turn(
                calls=[
                    call("get_slow_fraction", service="api", lookback_seconds=120),
                    call("get_slow_fraction", 1, service="worker", lookback_seconds=120),
                ]
            ),
            turn(calls=[call("get_process_cpu", service="worker", lookback_seconds=60)]),
            turn(answer(bundle)),
        ]
    )
    report, info = investigate_with_tools(bundle, provider)
    assert report.predicted_cause == "network_latency" and report.mode == "llm_tools"
    assert [c["name"] for c in info["tool_calls"]] == [
        "get_slow_fraction",
        "get_slow_fraction",
        "get_process_cpu",
    ]
    first = provider.session_.sent[1]["tool_results"]
    assert {r["tool_use_id"] for r in first} == {
        "toolu_get_slow_fraction_0",
        "toolu_get_slow_fraction_1",
    }
    result = json.loads(first[0]["content"])["results"][0]
    assert result["metric_id"] == "api.slow_fraction" and result["lookback_seconds"] == 120
    assert info["turns"] == 3 and info["citation_problems"] == []


def test_tool_mode_flags_citations_it_never_retrieved(bundle_for):
    bundle = bundle_for(LATENCY)
    provider = ScriptedProvider(
        [
            turn(calls=[call("get_slow_fraction", service="api", lookback_seconds=60)]),
            turn(answer(bundle)),  # also cites worker.slow_fraction
        ]
    )
    _, info = investigate_with_tools(bundle, provider)
    assert info["citation_problems"] == ["worker.slow_fraction: cited without being retrieved"]


@pytest.mark.parametrize(
    "bad",
    [
        call("execute_shell", command="docker exec worker kill 1"),
        call("stop_fault"),
        call("get_process_rss", service="proxy", lookback_seconds=60),
        call("get_request_rate", service="api", lookback_seconds=5),
        call("get_request_rate", service="api", lookback_seconds=60, promql="lab_cpu_fault_active"),
        call("get_request_latency", service="api", percentile="p99", lookback_seconds=60),
    ],
)
def test_invalid_or_unlisted_tools_are_refused(bundle_for, bad):
    bundle = bundle_for(LATENCY)
    provider = ScriptedProvider([turn(calls=[bad]), turn(answer(bundle))])
    _, info = investigate_with_tools(bundle, provider)
    assert info["tool_calls"][0]["ok"] is False
    sent = provider.session_.sent[1]["tool_results"][0]
    assert sent["is_error"] is True and "fault" not in sent["content"]


def test_no_mutating_tools_exist():
    names = {tool["name"] for tool in TOOLS}
    assert all(name.startswith("get_") for name in names)
    assert all(tool["input_schema"]["additionalProperties"] is False for tool in TOOLS)
    assert not any(token in json.dumps(TOOLS) + SYSTEM_PROMPT for token in FORBIDDEN_TOKENS)


def test_tool_budget_is_enforced(bundle_for):
    bundle = bundle_for(LATENCY)
    greedy = [call("get_request_rate", i, service="api", lookback_seconds=60) for i in range(4)]
    provider = ScriptedProvider(
        [turn(calls=greedy), turn(calls=greedy), turn(calls=greedy), turn(calls=greedy)]
    )
    report, info = investigate_with_tools(bundle, provider, max_calls=6)
    assert len(info["tool_calls"]) == 6
    second = provider.session_.sent[2]
    assert sum(r["is_error"] for r in second["tool_results"]) == 2
    assert "budget exhausted" in second["note"]
    assert report.predicted_cause == "unknown_or_ambiguous"
    assert info["outcome"] == "budget_exhausted"
    assert [e.metric_id for e in report.evidence] == ["api.request_rate"]  # partial, grounded


def test_wall_time_and_token_budgets(bundle_for):
    bundle = bundle_for(LATENCY)
    report, info = investigate_with_tools(bundle, ScriptedProvider([]), wall_seconds=0)
    assert "Wall-time budget" in report.limitations[0] and info["turns"] == 0
    provider = ScriptedProvider(
        [turn(calls=[call("get_recent_firing_alerts")], tokens=(70_000, 500))]
    )
    report, info = investigate_with_tools(bundle, provider, token_budget=60_000)
    assert "Token budget" in report.limitations[0] and info["tool_calls"] == []


def test_anthropic_request_shape_without_network():
    anthropic = pytest.importorskip("anthropic")
    from agent.llm_provider import FALLBACK_BETA, AnthropicSession

    sent = []
    content = [
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="tool_use", id="toolu_1", name="get_recent_firing_alerts", input={}),
    ]
    usage = SimpleNamespace(
        input_tokens=900,
        output_tokens=120,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=None,
    )

    def create(**request):
        sent.append(request)
        return SimpleNamespace(
            content=content, stop_reason="tool_use", usage=usage, model="claude-opus-5-5"
        )

    client = SimpleNamespace(
        with_options=lambda **_: SimpleNamespace(
            beta=SimpleNamespace(messages=SimpleNamespace(create=create))
        )
    )
    provider = SimpleNamespace(client=client, model="claude-opus-5-5", effort="medium")
    session = AnthropicSession(provider, "system", "prompt", TOOLS[:1], {"type": "object"}, 16000)
    first = session.send(timeout=30)
    assert first.tool_calls[0].name == "get_recent_firing_alerts"
    request = sent[0]
    assert request["fallbacks"] == "default" and request["betas"] == [FALLBACK_BETA]
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["output_config"]["effort"] == "medium"
    assert all(tool["strict"] for tool in request["tools"])
    assert "tool_choice" not in request and "thinking" not in request
    session.send([{"tool_use_id": "toolu_1", "content": "{}", "is_error": False}], "answer now")
    # Thinking blocks are passed back unchanged, then the tool results and note follow.
    assert session.messages[1]["content"] is content
    assert [b["type"] for b in session.messages[2]["content"]] == ["tool_result", "text"]

    def fail(**_):
        raise anthropic.APIConnectionError(request=None)

    client.with_options = lambda **_: SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(create=fail))
    )
    with pytest.raises(ProviderError, match="unreachable"):
        session.send()
