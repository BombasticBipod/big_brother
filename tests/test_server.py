"""The MCP server gives the test writer short results and never shows it implementation text."""
import contextlib
import os
import subprocess
from pathlib import Path

import anyio
import pytest
from mcp import Client

from big_brother.ledger import Ledger
from big_brother.red_check import SUMMARY_BUDGET
from big_brother.server import INTERFACE_BUDGET, make_server, progress_path
from big_brother.staging import Staging, staging_path
from big_brother.suite_lock import SuiteLock

CALC_PYI = 'def add(a: int, b: int) -> int:\n    """Return a plus b."""\n'
TEST_ADD = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
SECRET = "REAL_SOURCE_MARKER"
GOOD = f"FILE: src/calc.py\n```python\ndef add(a, b):\n    return a + b  # {SECRET}\n```\n"
BAD = f"FILE: src/calc.py\n```python\ndef add(a, b):\n    return a - b  # {SECRET}\n```\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


class FakeModel:
    def __init__(self, *replies: str):
        self.replies, self.calls = list(replies), 0

    def chat(self, messages):
        self.calls += 1
        return self.replies[min(self.calls, len(self.replies)) - 1]


@contextlib.contextmanager
def no_ollama():
    yield


@pytest.fixture
def target(tmp_path: Path):
    repo = tmp_path / "target"
    (repo / "interface").mkdir(parents=True)
    (repo / "interface" / "calc.pyi").write_text(CALC_PYI)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "interface")
    SuiteLock(repo).lock()
    Ledger(repo).add("add two numbers")
    yield repo
    for d, _, _ in os.walk(repo):  # let tmp cleanup delete read-only dirs
        os.chmod(d, 0o755)


def call(server, tool: str, **args) -> tuple[bool, str]:
    """Call one tool in-process; return (is_error, text)."""
    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool, args)
    result = anyio.run(go)
    return result.is_error, "".join(c.text for c in result.content)


def serve(target: Path, *replies: str):
    return make_server(target, model=FakeModel(*(replies or (GOOD,))), on_demand=no_ollama)


def tool_names(server) -> set[str]:
    async def go():
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}
    return anyio.run(go)


# the tools

def test_the_server_offers_exactly_the_designed_tools(target):
    assert tool_names(serve(target)) == {
        "next_requirement", "get_interface", "propose_test", "propose_interface",
        "discard_staged", "commit_tests", "build", "feedback"}


def test_next_requirement(target):
    assert call(serve(target), "next_requirement") == (False, "1 [open] add two numbers")


def test_next_requirement_when_everything_is_done(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    assert call(serve(repo), "next_requirement") == (False, "no open requirements")


def test_get_interface_returns_the_pyi(target):
    assert call(serve(target), "get_interface", module="calc") == (False, CALC_PYI)


def test_get_interface_lists_modules_when_none_is_named(target):
    assert call(serve(target), "get_interface", module="") == (False, "modules: calc")


def test_get_interface_shows_staged_changes(target):
    Staging(target).propose("interface/calc.pyi", CALC_PYI + "def sub(a: int, b: int) -> int: ...\n")
    error, text = call(serve(target), "get_interface", module="calc")
    assert not error and "def sub" in text


@pytest.mark.parametrize("module", ["../src/calc", "/etc/passwd", "missing", "calc.pyi"])
def test_get_interface_refuses_anything_but_a_module_name(target, module):
    error, text = call(serve(target), "get_interface", module=module)
    assert error and SECRET not in text


def test_propose_test_stages_and_reports_red(target):
    error, text = call(serve(target), "propose_test", path="tests/test_add.py", content=TEST_ADD)
    assert (error, text) == (False, "red: 1 test fails correctly")
    assert (staging_path(target) / "tests" / "test_add.py").exists()
    assert not (target / "tests" / "test_add.py").exists()


@pytest.mark.parametrize("tool,path", [
    ("propose_test", "src/calc.py"), ("propose_test", "interface/calc.pyi"),
    ("propose_test", "tests/../src/calc.py"), ("propose_interface", "tests/test_x.py"),
    ("propose_interface", "src/calc.pyi"), ("propose_test", "/tmp/test_x.py"),
])
def test_writes_outside_the_allowed_directory_are_refused(target, tool, path):
    error, text = call(serve(target), tool, path=path, content="x = 1\n")
    assert error
    assert Staging(target).paths() == []
    assert not (target / "src").exists()


def test_discard_staged(target):
    call(serve(target), "propose_test", path="tests/test_add.py", content=TEST_ADD)
    assert call(serve(target), "discard_staged") == (False, "discarded 1 staged file")
    assert Staging(target).paths() == []


def test_commit_tests_commits_and_marks_the_requirement(target):
    server = serve(target)
    call(server, "propose_test", path="tests/test_add.py", content=TEST_ADD)
    error, text = call(server, "commit_tests", message="test add", requirement_id=1)
    head = git(target, "rev-parse", "HEAD").strip()
    assert (error, text) == (False, f"committed {head[:12]} for requirement 1")
    assert Ledger(target).get(1).status == "tested"
    assert Ledger(target).get(1).suite_commit == head


def test_commit_tests_refuses_an_unknown_requirement_before_committing(target):
    server = serve(target)
    call(server, "propose_test", path="tests/test_add.py", content=TEST_ADD)
    head = git(target, "rev-parse", "HEAD")
    error, text = call(server, "commit_tests", message="test add", requirement_id=9)
    assert error and "9" in text
    assert git(target, "rev-parse", "HEAD") == head
    assert Staging(target).paths() == ["tests/test_add.py"]


def committed(target: Path, *replies: str):
    server = serve(target, *replies)
    call(server, "propose_test", path="tests/test_add.py", content=TEST_ADD)
    call(server, "commit_tests", message="test add", requirement_id=1)
    return server


def test_green_build_marks_tested_requirements_done(target):
    error, text = call(committed(target, GOOD), "build", max_tries=2)
    assert (error, text) == (False, "green after 1 try: 1 tests pass")
    req = Ledger(target).get(1)
    assert (req.status, req.src_commit) == ("done", git(target, "rev-parse", "HEAD").strip())


def test_stuck_build_counts_against_the_requirement(target):
    error, text = call(committed(target, BAD), "build", max_tries=2)
    assert not error and text.startswith("stuck after 2 tries")
    assert Ledger(target).get(1).stuck_builds == 1


def test_build_progress_goes_to_a_file_not_to_stdout(target, capfd):
    server = committed(target, GOOD)
    capfd.readouterr()
    call(server, "build", max_tries=2)
    out, _ = capfd.readouterr()
    assert out == ""
    lines = progress_path(target).read_text().splitlines()
    assert any("try 1/2" in line for line in lines)


def test_feedback_reports_within_budget(target):
    server = committed(target, GOOD)
    call(server, "build", max_tries=2)
    error, text = call(server, "feedback")
    assert not error and text.startswith("feedback: ") and len(text) <= SUMMARY_BUDGET


def test_no_result_carries_implementation_text(target):
    server = committed(target, BAD, GOOD)
    texts = [call(server, "build", max_tries=1)[1], call(server, "build", max_tries=2)[1],
             call(server, "feedback")[1], call(server, "next_requirement")[1],
             call(server, "get_interface", module="calc")[1]]
    for text in texts:
        assert SECRET not in text and "return a" not in text
        assert len(text) <= max(SUMMARY_BUDGET, INTERFACE_BUDGET)


def test_tool_errors_are_short(target):
    error, text = call(serve(target), "propose_test", path="src/" + "x" * 2000 + ".py",
                       content="x = 1\n")
    assert error and len(text) <= SUMMARY_BUDGET


def test_the_server_speaks_stdio_as_a_subprocess(target):
    import sys
    from mcp import StdioServerParameters

    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "big_brother.server", str(target)])

    async def go():
        async with Client(params) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            result = await client.call_tool("next_requirement", {})
            return names, result.content[0].text
    names, text = anyio.run(go)
    assert "build" in names and text == "1 [open] add two numbers"


def test_ollama_starts_once_per_session_and_stops_when_it_ends(target):
    events: list[str] = []

    @contextlib.contextmanager
    def counting():
        events.append("start")
        try:
            yield
        finally:
            events.append("stop")

    server = make_server(target, model=FakeModel(BAD, GOOD), on_demand=counting)
    call(server, "propose_test", path="tests/test_add.py", content=TEST_ADD)
    call(server, "commit_tests", message="test add", requirement_id=1)

    async def go():
        async with Client(server) as client:
            await client.call_tool("build", {"max_tries": 1})
            await client.call_tool("build", {"max_tries": 2})
            events.append("builds done")
            await client.call_tool("next_requirement", {})
    anyio.run(go)
    assert events == ["start", "builds done", "stop"]


def test_a_session_without_builds_never_starts_ollama(target):
    events: list[str] = []

    @contextlib.contextmanager
    def counting():
        events.append("start")
        yield

    call(make_server(target, model=FakeModel(GOOD), on_demand=counting), "next_requirement")
    assert events == []
