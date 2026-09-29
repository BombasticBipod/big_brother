"""The builder loop runs the locked suite, asks the model for src/, and stops green or stuck."""
import contextlib
import os
import subprocess
from pathlib import Path

import pytest

from big_brother.builder import (BuildResult, DirtySrc, allowed_paths, build, build_log_path,
                                 main, parse_reply)
from big_brother.ollama import OllamaError
from big_brother.runlock import BuildBusy, run_lock
from big_brother.staging import StagingError
from big_brother.suite_lock import NotLocked, SuiteLock, TestsTampered

INTERFACE = '''def add(a: int, b: int) -> int:
    """Return a plus b."""
'''
TESTS = '''from calc import add


def test_add():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-1, 1) == 0
'''


def reply(body: str, path: str = "src/calc.py") -> str:
    return f"Here you go.\n\nFILE: {path}\n```python\n{body}```\n"


GOOD = reply("def add(a, b):\n    return a + b\n")
BAD = reply("def add(a, b):\n    return a - b\n")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


class FakeModel:
    """Answers from a script of replies and records every prompt."""

    def __init__(self, *replies: str, before=None):
        self.replies = list(replies)
        self.prompts: list[list[dict]] = []
        self.before = before

    def chat(self, messages: list[dict]) -> str:
        self.prompts.append(messages)
        if self.before:
            self.before()
        return self.replies[min(len(self.prompts), len(self.replies)) - 1]

    def text(self, n: int) -> str:
        return "\n".join(m["content"] for m in self.prompts[n])


@pytest.fixture
def target(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    (repo / "interface").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "interface" / "calc.pyi").write_text(INTERFACE)
    (repo / "tests" / "test_calc.py").write_text(TESTS)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "suite")
    SuiteLock(repo).lock()
    return repo


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def run(repo: Path, model, **kw) -> BuildResult:
    kw.setdefault("progress", lambda line: None)
    return build(repo, model, **kw)


# parsing replies

ALLOWED = {"src/calc.py", "src/pkg/util.py"}


def test_parse_reply_extracts_declared_files():
    text = GOOD + reply("X = 1\n", "`src/pkg/util.py`")
    files, refused = parse_reply(text, ALLOWED)
    assert files == {"src/calc.py": "def add(a, b):\n    return a + b\n",
                     "src/pkg/util.py": "X = 1\n"}
    assert refused == []


@pytest.mark.parametrize("path", ["tests/test_calc.py", "interface/calc.pyi", "src/pytest.py",
                                  "src/sitecustomize.py", "src/../tests/test_calc.py",
                                  "/tmp/calc.py", "src/other.py"])
def test_parse_reply_refuses_undeclared_paths(path):
    files, refused = parse_reply(reply("x = 1\n", path), ALLOWED)
    assert files == {}
    assert refused == [path]


def test_parse_reply_takes_a_lone_unlabeled_block_when_one_file_is_declared():
    files, refused = parse_reply("```python\nX = 2\n```", {"src/calc.py"})
    assert files == {"src/calc.py": "X = 2\n"}


def test_parse_reply_ignores_a_lone_block_when_several_files_are_declared():
    assert parse_reply("```python\nX = 2\n```", ALLOWED) == ({}, [])


def test_allowed_paths_follow_the_interface(target):
    with SuiteLock(target).accept("add pkg"):   # the interface is locked with the tests
        (target / "interface" / "pkg").mkdir()
        (target / "interface" / "pkg" / "__init__.pyi").write_text("")
        (target / "interface" / "pkg" / "util.pyi").write_text("X: int\n")
    assert allowed_paths(target) == {"src/calc.py", "src/pkg/__init__.py", "src/pkg/util.py"}


# outcomes

def test_green_on_first_reply_commits_src_only(target):
    before = head(target)
    result = run(target, FakeModel(GOOD))
    assert (result.status, result.tries) == ("green", 1)
    assert result.summary == "green after 1 try: 2 tests pass"
    assert git(target, "diff", "--name-only", before, "HEAD").split() == ["src/calc.py"]
    assert git(target, "status", "--porcelain") == ""
    lock = SuiteLock(target)
    assert lock.is_locked() and lock.changes() == []
    assert not os.access(target / "tests" / "test_calc.py", os.W_OK)


def test_already_green_asks_nothing_and_commits_nothing(target):
    run(target, FakeModel(GOOD))
    before = head(target)
    model = FakeModel(BAD)
    result = run(target, model)
    assert (result.status, result.tries) == ("green", 0)
    assert model.prompts == [] and head(target) == before


def test_second_prompt_carries_failures_and_current_source(target):
    model = FakeModel(BAD, GOOD)
    result = run(target, model)
    assert (result.status, result.tries) == ("green", 2)
    first, second = model.text(0), model.text(1)
    assert "def add(a: int, b: int) -> int" in first      # the interface
    assert "assert add(2, 3) == 5" in first               # the failing test source
    assert "src/calc.py" in first                         # the file to write
    assert "return a - b" in second                       # current source
    assert "assert -1 == 5" in second                     # pytest failure detail


def test_stuck_restores_src_and_reports_failing_tests(target):
    before = head(target)
    result = run(target, FakeModel(BAD), max_tries=3)
    assert (result.status, result.tries) == ("stuck", 3)
    assert result.summary.startswith("stuck after 3 tries: ")
    assert "tests/test_calc.py::test_add [call] AssertionError: assert -1 == 5" in result.summary
    assert len(result.summary) <= 500
    assert not (target / "src" / "calc.py").exists()
    assert head(target) == before and git(target, "status", "--porcelain") == ""


def test_stuck_summary_stays_under_budget(target):
    many = "".join(f"def test_{i}():\n    assert add({i}, 0) == {i} + 1000\n\n" for i in range(40))
    with SuiteLock(target).accept("many"):
        (target / "tests" / "test_many.py").write_text("from calc import add\n\n" + many)
    result = run(target, FakeModel(GOOD), max_tries=1)
    assert result.status == "stuck"
    assert len(result.summary) <= 500 and "more)" in result.summary


def test_refused_paths_are_not_written_and_are_reported_back(target):
    cheat = reply("def test_add():\n    pass\n", "tests/test_calc.py") + reply("", "src/pytest.py")
    model = FakeModel(cheat, GOOD)
    result = run(target, model)
    assert result.status == "green"
    assert not (target / "src" / "pytest.py").exists()
    assert SuiteLock(target).changes() == []
    assert "refused" in model.text(1) and "tests/test_calc.py" in model.text(1)


def test_reply_without_files_is_reported_back(target):
    model = FakeModel("I cannot do that.", GOOD)
    assert run(target, model).status == "green"
    assert "no FILE" in model.text(1)


@pytest.mark.parametrize("body, marker", [
    ("def add(a, b):\n    return SECRET_SYNTAX_MARKER +\n", "SECRET_SYNTAX_MARKER"),
    ("def add(a, b):\n    raise ValueError('SECRET_VALUE_MARKER')\n", "SECRET_VALUE_MARKER"),
    ("SECRET_IMPORT_MARKER = 1\nraise RuntimeError(f'{SECRET_IMPORT_MARKER} at import')\n",
     "SECRET_IMPORT_MARKER"),
    ("def add(a, b):\n    assert False, 'SECRET_ASSERT_MARKER'\n", "SECRET_ASSERT_MARKER"),
])
def test_implementation_text_never_reaches_summary_or_progress(target, body, marker):
    lines: list[str] = []
    result = build(target, FakeModel(reply(body)), max_tries=1, progress=lines.append)
    assert result.status == "stuck"
    assert marker not in result.summary
    assert not any(marker in line for line in lines)


def test_non_assertion_errors_report_type_only(target):
    result = run(target, FakeModel(reply("def add(a, b):\n    raise ValueError('x')\n")),
                 max_tries=1)
    assert "[call] ValueError;" in result.summary or result.summary.endswith("[call] ValueError")


def test_tampering_during_a_try_aborts_and_restores_everything(target):
    test_file = target / "tests" / "test_calc.py"

    def tamper():
        test_file.chmod(0o644)
        test_file.write_text("def test_add():\n    pass\n")

    before = head(target)
    with pytest.raises(TestsTampered):
        run(target, FakeModel(GOOD, before=tamper))
    assert test_file.read_text() == TESTS
    assert not (target / "src" / "calc.py").exists()
    assert head(target) == before


# code under build cannot reach big_brother's own state

def plant(code: str) -> str:
    """A reply whose module-level code runs during the test run, then implements add."""
    return reply(code + "\n\ndef add(a, b):\n    return a + b\n")


def test_code_under_build_does_not_run_inside_the_target(target):
    code = ("import os, pathlib\n"
            "p = pathlib.Path('.git/big_brother/staged/tests/test_evil.py')\n"
            "p.parent.mkdir(parents=True, exist_ok=True)\n"
            "p.write_text('def test_evil():\\n    assert False\\n')\n"
            "pathlib.Path('marker_from_build').write_text('x')\n")
    result = run(target, FakeModel(plant(code)))
    assert result.status == "green"
    assert not (target / ".git" / "big_brother" / "staged").exists()
    assert not (target / "marker_from_build").exists()


def test_rewriting_the_lock_state_during_a_build_is_caught(target):
    state = target / ".git" / "big_brother" / "suite_lock.json"
    test_file = target / "tests" / "test_calc.py"
    code = ("import json, os, pathlib, subprocess\n"
            f"repo = {str(target)!r}\n"
            f"t = pathlib.Path({str(test_file)!r})\n"
            "os.chmod(t.parent, 0o755); os.chmod(t, 0o644)\n"
            "t.write_text('def test_add():\\n    pass\\n')\n"
            "subprocess.run(['git', '-C', repo, 'commit', '-qam', 'weaken'], check=True)\n"
            "head = subprocess.run(['git', '-C', repo, 'rev-parse', 'HEAD'], check=True,\n"
            "                      capture_output=True, text=True).stdout.strip()\n"
            f"pathlib.Path({str(state)!r}).write_text(json.dumps({{'tests': head}}))\n")
    before = SuiteLock(target).base
    with pytest.raises(TestsTampered):
        run(target, FakeModel(plant(code)))
    assert SuiteLock(target).base == before
    assert "assert add(2, 3) == 5" in test_file.read_text()


def test_staged_files_planted_during_a_build_are_caught_and_wiped(target):
    staged = target / ".git" / "big_brother" / "staged" / "tests" / "test_evil.py"
    code = ("import pathlib\n"
            f"p = pathlib.Path({str(staged)!r})\n"
            "p.parent.mkdir(parents=True, exist_ok=True)\n"
            "p.write_text('def test_evil():\\n    assert False\\n')\n")
    with pytest.raises(TestsTampered):
        run(target, FakeModel(plant(code)))
    assert not staged.exists()


def test_build_is_refused_while_suite_files_are_staged(target):
    staged = target / ".git" / "big_brother" / "staged" / "tests" / "test_new.py"
    staged.parent.mkdir(parents=True)
    staged.write_text("def test_new():\n    assert False\n")
    with pytest.raises(StagingError):
        run(target, FakeModel(GOOD))
    assert staged.exists()


def test_model_error_restores_src_and_propagates(target):
    model = FakeModel(BAD)

    def second_call_fails():
        if len(model.prompts) == 2:
            raise OllamaError("down")

    model.before = second_call_fails
    with pytest.raises(OllamaError):
        run(target, model, max_tries=3)
    assert not (target / "src" / "calc.py").exists()


def test_assertion_from_src_reports_type_only(target):
    result = run(target, FakeModel(reply("def add(a, b):\n    assert False, 'x'\n")), max_tries=1)
    assert result.summary.endswith("::test_add_negative [call] AssertionError")


# committed src/

NEW_TEST = "from calc import add\n\n\ndef test_add_zero():\n    assert add(0, 0) == 1\n"


@pytest.fixture
def built(target: Path) -> Path:
    assert run(target, FakeModel(GOOD)).status == "green"
    with SuiteLock(target).accept("impossible test"):
        (target / "tests" / "test_zero.py").write_text(NEW_TEST)
    return target


def test_stuck_keeps_the_committed_src(built):
    before = head(built)
    committed = (built / "src" / "calc.py").read_text()
    result = run(built, FakeModel(BAD), max_tries=2)
    assert result.status == "stuck"
    assert (built / "src" / "calc.py").read_text() == committed
    assert head(built) == before and git(built, "status", "--porcelain") == ""


def test_model_error_keeps_the_committed_src(built):
    before = head(built)
    committed = (built / "src" / "calc.py").read_text()
    model = FakeModel(BAD)

    def second_call_fails():
        if len(model.prompts) == 2:
            raise OllamaError("down")

    model.before = second_call_fails
    with pytest.raises(OllamaError):
        run(built, model, max_tries=3)
    assert (built / "src" / "calc.py").read_text() == committed
    assert head(built) == before and git(built, "status", "--porcelain") == ""


# preconditions

def test_unlocked_suite_is_refused(tmp_path):
    repo = tmp_path / "r"
    (repo / "tests").mkdir(parents=True)
    git(repo, "init", "-q")
    with pytest.raises(NotLocked):
        run(repo, FakeModel(GOOD))


def test_uncommitted_src_is_refused(target):
    (target / "src").mkdir()
    (target / "src" / "calc.py").write_text("# mine\n")
    with pytest.raises(DirtySrc):
        run(target, FakeModel(GOOD))
    assert (target / "src" / "calc.py").read_text() == "# mine\n"


def test_build_is_refused_while_another_run_holds_the_lock(target):
    with run_lock(target):
        with pytest.raises(BuildBusy):
            run(target, FakeModel(GOOD))


def test_suite_with_no_tests_is_stuck_without_asking(target):
    with SuiteLock(target).accept("empty"):
        (target / "tests" / "test_calc.py").write_text("")
    model = FakeModel(GOOD)
    result = run(target, model)
    assert (result.status, result.tries, result.summary) == ("stuck", 0,
                                                             "stuck: no tests collected")
    assert model.prompts == []


def test_tests_run_only_against_the_target_src(target, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    monkeypatch.setenv("PYTHONPATH", str(elsewhere))
    monkeypatch.setenv("PYTEST_ADDOPTS", f"--rootdir={elsewhere}")
    result = run(target, FakeModel(BAD), max_tries=0)
    assert (result.status, result.tries) == ("stuck", 0)


# progress and log

def test_progress_prints_counts_and_log_keeps_detail(target):
    lines: list[str] = []
    build(target, FakeModel(BAD, GOOD), progress=lines.append)
    assert any("try 1/5" in line for line in lines)
    assert any("2 failed" in line for line in lines)
    log = build_log_path(target).read_text()
    assert "return a - b" in log and "assert -1 == 5" in log
    assert git(target, "status", "--porcelain") == ""


# real model (opt-in: uv run pytest -m ollama)

@pytest.mark.ollama
def test_real_model_builds_the_toy_target(target):
    from big_brother.ollama import OllamaClient, ollama_on_demand
    client = OllamaClient()
    with ollama_on_demand(is_up=client.is_up):
        result = build(target, client, max_tries=5)
    print(result)
    assert result.status == "green", result.summary


# command line

def test_main_exit_codes(target, capsys):
    none = contextlib.nullcontext
    assert main([str(target), "--max-tries", "1"], client=FakeModel(BAD), on_demand=none) == 1
    assert "stuck after 1 try" in capsys.readouterr().out
    assert main([str(target)], client=FakeModel(GOOD), on_demand=none) == 0
    assert "green after 1 try" in capsys.readouterr().out
