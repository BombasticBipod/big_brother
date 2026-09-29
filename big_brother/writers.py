"""The test writer is a socket: Claude Code headless, or any tool-calling model.

Every writer gets the same thing: the target, the goal, how to start the
big_brother MCP server, and `emit(text)`, which writes to the writer's window.
Every writer reaches the target only through the server's tools, so none of
them can read `src/`:

- `ClaudeCodeWriter` runs `claude -p` in the target with every built-in tool
  disabled (`--tools ""`), only the big_brother server loaded
  (`--strict-mcp-config`) and only its tools allowed (`dontAsk` refuses the
  rest). Its stream-json events are rendered for the window.
- `AgentWriter` is a plain tool-calling loop for any other model: it lists the
  server's tools, hands them to a conversation (`OllamaConversation`, or
  `cloud.AnthropicConversation`), runs each tool call through an MCP client
  and feeds the results back, until the model answers in text or the turn cap.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import anyio

from big_brother.cloud import ToolCall, Turn
from big_brother.ollama import OllamaClient

SHOWN = 300   # characters of one tool call or result shown in the writer window
PREFIX = "mcp__big_brother__"

Emit = Callable[[str], None]


@dataclass
class WriterResult:
    status: str        # "finished", "turn cap" or "failed"
    turns: int = 0
    detail: str = ""


class Writer(Protocol):
    def run(self, target: Path, goal: str, server: dict, emit: Emit) -> WriterResult: ...


WRITER_SYSTEM = ("You are the test writer. You work only through the big_brother tools and never "
                 "see implementation code. Keep each test small and about one behavior.")


def writer_prompt(goal: str) -> str:
    return (
        "You are the test writer for this project. Work only through the big_brother tools; "
        "you never see src/, a local builder writes it from your tests.\n\n"
        f"Goal: {goal}\n\n"
        "1. Split the goal into small requirements, each one behavior, and record each with "
        "add_requirement.\n"
        "2. Loop: next_requirement; get_interface; propose_interface if the interface needs "
        "a change; propose_test until the check says red; commit_tests with the requirement "
        "id; build; read feedback and add tests for gaps and surviving mutants.\n"
        "3. Stop when next_requirement reports nothing left, then say what was done.")


def _clip(text: str, limit: int = SHOWN) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _call_line(name: str, args: dict) -> str:
    return f"\n> {name.removeprefix(PREFIX)}({_clip(json.dumps(args), SHOWN)})\n"


def _result_line(text: str, error: bool = False) -> str:
    return f"\n< {'error: ' if error else ''}{_clip(text)}\n"


# Claude Code

def claude_argv(prompt: str, server: dict, model: str | None) -> list[str]:
    config = {"mcpServers": {"big_brother": {"type": "stdio", **server}}}
    argv = ["claude", "-p", prompt,
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--mcp-config", json.dumps(config), "--strict-mcp-config",
            "--tools", "", "--allowedTools", f"{PREFIX}*", "--permission-mode", "dontAsk"]
    if model:
        argv += ["--model", model]
    return argv


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(c.get("text", "") for c in content or [] if isinstance(c, dict))


def render_event(event: dict) -> str:
    """One stream-json event as window text; events with nothing to show render as ''."""
    kind = event.get("type")
    if kind == "stream_event":
        delta = event.get("event", {}).get("delta", {})
        return delta.get("text", "") if delta.get("type") == "text_delta" else ""
    blocks = event.get("message", {}).get("content", []) if kind in ("assistant", "user") else []
    if kind == "assistant":
        return "".join(_call_line(b["name"], b.get("input", {})) for b in blocks
                       if b.get("type") == "tool_use")
    if kind == "user":
        return "".join(_result_line(_result_text(b.get("content")), b.get("is_error", False))
                       for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result")
    if kind == "result":
        return f"\n[writer {event.get('subtype')} after {event.get('num_turns')} turns]\n"
    return ""


def _launch(argv: list[str], cwd: Path):
    return subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True)


class ClaudeCodeWriter:
    def __init__(self, model: str | None = None, launch: Callable = _launch):
        self.model = model
        self.launch = launch

    def run(self, target: Path, goal: str, server: dict, emit: Emit) -> WriterResult:
        proc = self.launch(claude_argv(writer_prompt(goal), server, self.model), target)
        status, turns = "failed", 0
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                emit(line)
                continue
            if event.get("type") == "result":
                status = "finished" if event.get("subtype") == "success" else "failed"
                turns = event.get("num_turns") or 0
            text = render_event(event)
            if text:
                emit(text)
        code = proc.wait()
        return WriterResult(status if code == 0 else "failed", turns, f"exit {code}")


# any tool-calling model

class Conversation(Protocol):
    def send(self, text: str, on_text: Emit | None = None) -> Turn: ...
    def results(self, results: list[tuple[ToolCall, str, bool]],
                on_text: Emit | None = None) -> Turn: ...


@asynccontextmanager
async def _mcp_connect(server: dict):
    from mcp import Client, StdioServerParameters
    async with Client(StdioServerParameters(command=server["command"],
                                            args=server["args"])) as client:
        yield client


class AgentWriter:
    def __init__(self, make_chat: Callable[[str, list[dict]], Conversation],
                 connect: Callable = _mcp_connect, max_turns: int = 200):
        self.make_chat = make_chat
        self.connect = connect
        self.max_turns = max_turns

    def run(self, target: Path, goal: str, server: dict, emit: Emit) -> WriterResult:
        return anyio.run(self._run, goal, server, emit)

    async def _run(self, goal: str, server: dict, emit: Emit) -> WriterResult:
        async with self.connect(server) as client:
            listed = (await client.list_tools()).tools
            tools = [{"name": t.name, "description": t.description or "",
                      "inputSchema": t.input_schema} for t in listed]
            chat = self.make_chat(WRITER_SYSTEM, tools)
            turn = chat.send(writer_prompt(goal), on_text=emit)
            turns, calls = 1, 0
            while turn.calls:
                results = []
                for call in turn.calls:
                    if calls >= self.max_turns:
                        emit("\n[writer stopped: turn cap]\n")
                        return WriterResult("turn cap", turns)
                    calls += 1
                    emit(_call_line(call.name, call.args))
                    result = await client.call_tool(call.name, call.args)
                    text = "".join(getattr(c, "text", "") for c in result.content)
                    emit(_result_line(text, result.is_error))
                    results.append((call, text, bool(result.is_error)))
                turn = chat.results(results, on_text=emit)
                turns += 1
            emit("\n[writer finished]\n")
            return WriterResult("finished", turns)


class OllamaConversation:
    """A tool-calling conversation with a local model through Ollama's `/api/chat`."""

    def __init__(self, system: str, tools: list[dict], client: OllamaClient):
        self.client = client
        self.tools = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description") or "",
            "parameters": t["inputSchema"]}} for t in tools]
        self.messages: list[dict] = [{"role": "system", "content": system}]

    def send(self, text: str, on_text: Emit | None = None) -> Turn:
        self.messages.append({"role": "user", "content": text})
        return self._turn(on_text)

    def results(self, results: list[tuple[ToolCall, str, bool]],
                on_text: Emit | None = None) -> Turn:
        for call, text, _error in results:
            self.messages.append({"role": "tool", "tool_name": call.name, "content": text})
        return self._turn(on_text)

    def _turn(self, on_text: Emit | None) -> Turn:
        message = self.client.chat_message(self.messages, tools=self.tools)
        self.messages.append(message)
        text = message.get("content") or ""
        if text and on_text:
            on_text(text)
        calls = [ToolCall(f"call{len(self.messages)}_{i}", c["function"]["name"],
                          c["function"].get("arguments") or {})
                 for i, c in enumerate(message.get("tool_calls") or [])]
        return Turn(text, calls)
