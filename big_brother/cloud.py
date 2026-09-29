"""Cloud models through the Anthropic SDK, for either role.

`AnthropicModel` is a builder: `chat(messages, on_token)` streams the reply and
returns its text, the same shape as `OllamaClient.chat`. A leading system
message becomes the request's `system` field.

`AnthropicConversation` is a tool-calling test writer's model: `send(text)`
and `results(...)` each return a `Turn` of text and tool calls. The assistant
turn is appended to the history whole, thinking blocks included, so the
history stays append-only.

Requests use the server-side refusal fallback (`fallbacks: "default"`), so a
declined request is retried on a fallback model inside the same call. A
refusal that survives the fallback is a `CloudError`. Credentials come from
the environment (`ANTHROPIC_API_KEY` or an `ant auth login` profile) and are
never logged.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_CLOUD_MODEL = "claude-opus-5-5"
MAX_TOKENS = 64000
EFFORT = "high"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class CloudError(Exception):
    """The cloud model could not answer the request."""


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Turn:
    text: str
    calls: list[ToolCall] = field(default_factory=list)


def _client(client: Any):
    if client is not None:
        return client
    import anthropic
    return anthropic.Anthropic()


def _stream(client: Any, model: str, system: str, messages: list, tools: list | None,
            on_text: Callable[[str], None] | None):
    kw: dict[str, Any] = {
        "model": model, "max_tokens": MAX_TOKENS, "messages": messages,
        "output_config": {"effort": EFFORT},
        "betas": [FALLBACK_BETA], "fallbacks": "default",
    }
    if system:
        kw["system"] = system
    if tools:
        kw["tools"] = tools
    with client.beta.messages.stream(**kw) as stream:
        for text in stream.text_stream:
            if on_text:
                on_text(text)
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise CloudError(f"{model} refused the request")
    return message


def _text(message) -> str:
    return "".join(b.text for b in message.content if b.type == "text")


class AnthropicModel:
    def __init__(self, model: str = DEFAULT_CLOUD_MODEL, client: Any = None):
        self.model = model
        self.client = _client(client)

    def chat(self, messages: list[dict],
             on_token: Callable[[str], None] | None = None) -> str:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        rest = [m for m in messages if m["role"] != "system"]
        return _text(_stream(self.client, self.model, system, rest, None, on_token))


def anthropic_tools(mcp_tools: list[dict]) -> list[dict]:
    """MCP tool definitions (`inputSchema`) as Anthropic tool definitions (`input_schema`)."""
    return [{"name": t["name"], "description": t.get("description") or "",
             "input_schema": t["inputSchema"]} for t in mcp_tools]


class AnthropicConversation:
    def __init__(self, system: str, tools: list[dict], model: str = DEFAULT_CLOUD_MODEL,
                 client: Any = None):
        self.system = system
        self.tools = anthropic_tools(tools)
        self.model = model
        self.client = _client(client)
        self.messages: list[dict] = []

    def send(self, text: str, on_text: Callable[[str], None] | None = None) -> Turn:
        self.messages.append({"role": "user", "content": text})
        return self._turn(on_text)

    def results(self, results: list[tuple[ToolCall, str, bool]],
                on_text: Callable[[str], None] | None = None) -> Turn:
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": error}
            for call, text, error in results]})
        return self._turn(on_text)

    def _turn(self, on_text) -> Turn:
        message = _stream(self.client, self.model, self.system, list(self.messages),
                          self.tools, on_text)
        self.messages.append({"role": "assistant", "content": message.content})
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in message.content
                 if b.type == "tool_use"]
        return Turn(_text(message), calls)
