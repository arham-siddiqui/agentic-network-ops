"""LLM providers behind one small session interface: a scripted fake and the Anthropic API.

The investigator owns the loop and every budget (tool calls, wall time, tokens); a provider only
turns one request into one Turn. Credentials are resolved by the SDK from the host environment
and never pass through this code, prompts, or artifacts.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

DEFAULT_MODEL = "claude-opus-5-5"
# USD per million input/output tokens, for labelled estimates only (no prompt caching is used).
PRICES_PER_MTOK = {"claude-opus-5-5": (4.0, 20.0)}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass(frozen=True)
class Turn:
    text: str | None
    tool_calls: list[ToolCall]
    stop_reason: str
    input_tokens: int
    output_tokens: int
    model: str


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    if model not in PRICES_PER_MTOK:
        return None
    price_in, price_out = PRICES_PER_MTOK[model]
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


class AnthropicSession:
    def __init__(self, provider, system, prompt, tools, schema, max_tokens) -> None:
        self.provider, self.system, self.tools = provider, system, tools
        self.schema, self.max_tokens = schema, max_tokens
        self.messages: list[dict] = [{"role": "user", "content": prompt}]

    def send(self, tool_results=None, note=None, timeout: float = 30) -> Turn:
        import anthropic

        if tool_results is not None or note:
            content = [
                {
                    "type": "tool_result",
                    "tool_use_id": r["tool_use_id"],
                    "content": r["content"],
                    "is_error": r["is_error"],
                }
                for r in tool_results or []
            ]
            if note:
                content.append({"type": "text", "text": note})
            self.messages.append({"role": "user", "content": content})
        request = {
            "model": self.provider.model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "messages": self.messages,
            "output_config": {
                "effort": self.provider.effort,
                "format": {"type": "json_schema", "schema": self.schema},
            },
            # A classifier decline is re-run on Anthropic's recommended model for its category.
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
            "timeout": max(1.0, timeout),
        }
        if self.tools:
            request["tools"] = [{**tool, "strict": True} for tool in self.tools]
        client = self.provider.client.with_options(max_retries=1)
        try:
            response = client.beta.messages.create(**request)
        except anthropic.APITimeoutError as exc:
            raise ProviderError("model request timed out") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("model API rate limited") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"model API error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("model API unreachable") from exc
        except anthropic.AnthropicError as exc:  # e.g. no credentials configured
            raise ProviderError(f"model client error: {type(exc).__name__}") from exc
        # Append the full content so thinking blocks are passed back unchanged.
        self.messages.append({"role": "assistant", "content": response.content})
        return Turn(
            text=next((b.text for b in response.content if b.type == "text"), None),
            tool_calls=[
                ToolCall(b.id, b.name, dict(b.input))
                for b in response.content
                if b.type == "tool_use"
            ],
            stop_reason=response.stop_reason or "unknown",
            input_tokens=response.usage.input_tokens
            + (response.usage.cache_read_input_tokens or 0)
            + (response.usage.cache_creation_input_tokens or 0),
            output_tokens=response.usage.output_tokens,
            model=response.model,
        )


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "medium") -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise ProviderError("install the optional SDK: uv sync --extra llm") from exc
        self.model, self.effort = model, effort
        try:
            self.client = anthropic.Anthropic()
        except anthropic.AnthropicError as exc:
            raise ProviderError(f"model client unavailable: {type(exc).__name__}") from exc

    def session(self, system, prompt, tools, schema, max_tokens=16000) -> AnthropicSession:
        return AnthropicSession(self, system, prompt, tools, schema, max_tokens)


@dataclass
class ScriptedSession:
    respond: Callable[[list], Turn]
    sent: list = field(default_factory=list)

    def send(self, tool_results=None, note=None, timeout: float = 30) -> Turn:
        self.sent.append({"tool_results": tool_results, "note": note})
        return self.respond(self.sent)


class ScriptedProvider:
    """Deterministic stand-in for tests: replays turns and records everything it was sent."""

    name = "scripted"

    def __init__(self, turns: list[Turn | Exception], model: str = "scripted") -> None:
        self.turns, self.model = list(turns), model
        self.prompts: list[dict] = []

    def session(self, system, prompt, tools, schema, max_tokens=16000) -> ScriptedSession:
        self.prompts.append({"system": system, "prompt": prompt, "tools": tools})

        def respond(_sent):
            if not self.turns:
                raise ProviderError("script exhausted")
            turn = self.turns.pop(0)
            if isinstance(turn, Exception):
                raise turn
            return turn

        self.session_ = ScriptedSession(respond)
        return self.session_
