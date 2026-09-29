"""Test writer backends: Claude Code headless, or any tool-calling model driving the MCP tools."""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

from big_brother.cloud import ToolCall, Turn
from big_brother.ollama import OllamaClient
from big_brother.writers import (AgentWriter, ClaudeCodeWriter, OllamaConversation,
                                 claude_argv, render_event, writer_prompt)
from test_ollama import FakeOllama

SERVER = {"command": "/usr/bin/python3", "args": ["-m", "big_brother.server", "/t"]}


# Claude Code

def test_claude_argv_gives_only_the_big_brother_tools():
    argv = claude_argv("do it", SERVER, model=None)
    assert argv[:3] == ["claude", "-p", "do it"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--allowedTools") + 1] == "mcp__big_brother__*"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--include-partial-messages" in argv and "--verbose" in argv
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    assert config == {"mcpServers": {"big_brother": {"type": "stdio", **SERVER}}}
    assert "--model" not in argv


def test_claude_argv_passes_a_model():
    argv = claude_argv("x", SERVER, model="sonnet")
    assert argv[argv.index("--model") + 1] == "sonnet"


def test_render_event_shows_text_tool_calls_results_and_the_end():
    delta = {"type": "stream_event",
             "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}}
    call = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "name": "mcp__big_brother__propose_test",
         "input": {"path": "tests/test_a.py", "content": "x" * 500}}]}}
    result = {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": [{"type": "text", "text": "red: 1 test fails"}]}]}}
    end = {"type": "result", "subtype": "success", "num_turns": 7}
    assert render_event(delta) == "hi"
    shown = render_event(call)
    assert shown.startswith("\n> propose_test(") and len(shown) < 400
    assert "hi" not in shown
    assert render_event(result) == "\n< red: 1 test fails\n"
    assert "success" in render_event(end) and "7" in render_event(end)
    assert render_event({"type": "system"}) == ""


def test_claude_code_writer_streams_rendered_events(tmp_path):
    lines = [json.dumps({"type": "stream_event", "event": {
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": "working"}}}),
        "not json", json.dumps({"type": "result", "subtype": "success", "num_turns": 1})]
    seen, runs = [], []

    def launch(argv, cwd):
        runs.append((argv, cwd))
        return NS(stdout=iter(line + "\n" for line in lines), wait=lambda: 0)

    result = ClaudeCodeWriter(launch=launch).run(tmp_path, "the goal", SERVER, seen.append)
    assert "working" in "".join(seen)
    assert runs[0][1] == tmp_path and "the goal" in runs[0][0][2]
    assert result.status == "finished"


def test_writer_prompt_carries_the_goal_and_the_stop_rule():
    prompt = writer_prompt("a calculator")
    assert "a calculator" in prompt
    assert "add_requirement" in prompt and "next_requirement" in prompt
    assert "src/" in prompt


# any tool-calling model

class FakeMcp:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        return NS(tools=[NS(name="next_requirement", description="next",
                            input_schema={"type": "object", "properties": {}})])

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return NS(content=[NS(text=f"{name} ok")], is_error=False)


class ScriptedChat:
    """A conversation that asks for one tool per turn from a script, then answers in text."""

    def __init__(self, script):
        self.script = list(script)
        self.fed = []

    def _next(self, on_text):
        if on_text:
            on_text("thinking ")
        if not self.script:
            if on_text:
                on_text("all done")
            return Turn("all done")
        name = self.script.pop(0)
        return Turn("", [ToolCall(f"id{len(self.script)}", name, {"x": 1})])

    def send(self, text, on_text=None):
        self.fed.append(text)
        return self._next(on_text)

    def results(self, results, on_text=None):
        self.fed.append(results)
        return self._next(on_text)


def fake_connect(mcp):
    @asynccontextmanager
    async def connect(params):
        yield mcp
    return connect


def test_agent_writer_runs_tool_calls_until_the_model_answers_in_text(tmp_path):
    mcp, chats = FakeMcp(), []

    def make_chat(system, tools):
        assert tools == [{"name": "next_requirement", "description": "next",
                          "inputSchema": {"type": "object", "properties": {}}}]
        chats.append(ScriptedChat(["next_requirement", "feedback"]))
        return chats[-1]

    seen = []
    result = AgentWriter(make_chat, connect=fake_connect(mcp)).run(tmp_path, "goal", SERVER,
                                                                   seen.append)
    assert mcp.calls == [("next_requirement", {"x": 1}), ("feedback", {"x": 1})]
    assert result.status == "finished" and result.turns == 3
    shown = "".join(seen)
    assert "> next_requirement" in shown and "< next_requirement ok" in shown
    assert "all done" in shown
    assert "goal" in chats[0].fed[0]


def test_agent_writer_stops_at_the_turn_cap(tmp_path):
    mcp = FakeMcp()
    writer = AgentWriter(lambda s, t: ScriptedChat(["next_requirement"] * 50),
                         connect=fake_connect(mcp), max_turns=4)
    result = writer.run(tmp_path, "goal", SERVER, lambda text: None)
    assert result.status == "turn cap" and len(mcp.calls) == 4


# Ollama conversation

def test_ollama_conversation_sends_function_tools_and_feeds_results_back():
    calls = [{"function": {"name": "next_requirement", "arguments": {"a": 1}}}]
    server = FakeOllama(reply="", tool_calls=calls)
    try:
        chat = OllamaConversation("sys", [{"name": "next_requirement", "description": "next",
                                           "inputSchema": {"type": "object"}}],
                                  OllamaClient(host=server.host))
        turn = chat.send("goal")
        assert [(c.name, c.args) for c in turn.calls] == [("next_requirement", {"a": 1})]
        chat.results([(turn.calls[0], "1: add", False)])
    finally:
        server.close()
    first, second = server.requests[-2][2], server.requests[-1][2]
    assert first["tools"] == [{"type": "function", "function": {
        "name": "next_requirement", "description": "next", "parameters": {"type": "object"}}}]
    assert first["messages"][0] == {"role": "system", "content": "sys"}
    assert second["messages"][-1] == {"role": "tool", "tool_name": "next_requirement",
                                      "content": "1: add"}
