"""Role specs: which backend plays the builder and which plays the test writer.

A spec is `kind` or `kind:model`:
- `claude-code[:model]`: Claude Code headless. Test writer only.
- `ollama[:model]`: a local model through Ollama (default `qwen2.5-coder:3b`).
- `anthropic[:model]`: a cloud model through the Anthropic API
  (default `claude-opus-5-5`).

Specs are checked before a run starts anything, so a typo never leaves Ollama
running or a window open.
"""
from __future__ import annotations

from typing import Any

from big_brother.cloud import DEFAULT_CLOUD_MODEL, AnthropicConversation, AnthropicModel
from big_brother.ollama import DEFAULT_MODEL, OllamaClient
from big_brother.writers import AgentWriter, ClaudeCodeWriter, OllamaConversation

KINDS = {"builder": ("ollama", "anthropic"), "writer": ("claude-code", "ollama", "anthropic")}
DEFAULTS = {"ollama": DEFAULT_MODEL, "anthropic": DEFAULT_CLOUD_MODEL, "claude-code": None}


class RoleError(ValueError):
    """A role spec names no backend that can play that role."""


def parse_role(spec: str, role: str) -> tuple[str, str | None]:
    kind, sep, model = spec.partition(":")
    if kind not in KINDS[role]:
        raise RoleError(f"{role} {spec!r}: use one of {', '.join(KINDS[role])}, "
                        f"optionally followed by :MODEL")
    if sep and not model:
        raise RoleError(f"{role} {spec!r}: model name missing after ':'")
    return kind, model or DEFAULTS[kind]


def uses_ollama(*specs_and_roles: tuple[str, str]) -> bool:
    return any(parse_role(spec, role)[0] == "ollama" for spec, role in specs_and_roles)


def make_builder(spec: str, client: Any = None):
    kind, model = parse_role(spec, "builder")
    if kind == "ollama":
        return OllamaClient(model=model)
    return AnthropicModel(model=model, client=client)


def make_writer(spec: str, max_turns: int = 200, client: Any = None):
    kind, model = parse_role(spec, "writer")
    if kind == "claude-code":
        return ClaudeCodeWriter(model=model)
    if kind == "ollama":
        local = OllamaClient(model=model)
        return AgentWriter(lambda system, tools: OllamaConversation(system, tools, local),
                           max_turns=max_turns)
    return AgentWriter(lambda system, tools: AnthropicConversation(system, tools, model=model,
                                                                   client=client),
                       max_turns=max_turns)
