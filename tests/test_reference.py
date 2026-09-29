"""Reference answers: the test writer's own implementation, checked and kept out of the builder's sight."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from big_brother.reference import ReferenceError, reference_log_path, submit
from big_brother.suite_lock import NotLocked, SuiteLock

CALC_PYI = 'def add(a: int, b: int) -> int:\n    """Return a plus b."""\n'
TEST_ADD = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return a - b\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def target(tmp_path: Path):
    repo = tmp_path / "target"
    (repo / "interface").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "interface" / "calc.pyi").write_text(CALC_PYI)
    (repo / "tests" / "test_add.py").write_text(TEST_ADD)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "suite")
    SuiteLock(repo).lock()
    yield repo
    for d, _, _ in os.walk(repo):  # let tmp cleanup delete read-only dirs
        os.chmod(d, 0o755)


def records(repo: Path) -> list[dict]:
    path = reference_log_path(repo)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_green_reference_is_recorded_with_what_rebuilds_its_prompt(target):
    result = submit(target, {"src/calc.py": GOOD}, requirement_id=1, trigger="all")
    assert result.green and result.summary == "reference green: 1 tests pass"
    [rec] = records(target)
    head = git(target, "rev-parse", "HEAD").strip()
    assert rec["requirement_id"] == 1 and rec["trigger"] == "all"
    assert rec["files"] == {"src/calc.py": GOOD}
    assert rec["suite_commit"] == head and rec["src_commit"] == head
    assert (rec["green"], rec["passed"], rec["failed"]) == (True, 1, 0)


def test_the_reference_is_stored_outside_the_working_tree(target):
    submit(target, {"src/calc.py": GOOD}, requirement_id=1, trigger="all")
    assert reference_log_path(target).is_relative_to(target / ".git" / "big_brother")
    assert not (target / "src").exists()
    assert git(target, "status", "--porcelain", "--untracked-files=all") == ""


def test_the_reference_runs_without_the_builders_src(target):
    (target / "src").mkdir()
    (target / "src" / "calc.py").write_text(GOOD)
    git(target, "add", "src")
    git(target, "commit", "-qm", "builder src")
    result = submit(target, {"src/calc.py": BAD}, requirement_id=1, trigger="stuck")
    assert not result.green
    assert result.summary.startswith("reference red: ")
    assert "tests/test_add.py::test_add" in result.summary
    assert records(target)[0]["green"] is False
    assert (target / "src" / "calc.py").read_text() == GOOD


@pytest.mark.parametrize("path", ["src/sitecustomize.py", "src/pytest.py", "tests/test_x.py",
                                  "src/../calc.py", "calc.py"])
def test_only_files_the_interface_declares_are_accepted(target, path):
    with pytest.raises(ReferenceError, match="interface declares"):
        submit(target, {"src/calc.py": GOOD, path: "x = 1\n"}, requirement_id=1, trigger="all")
    assert records(target) == []


def test_an_empty_reference_is_refused(target):
    with pytest.raises(ReferenceError):
        submit(target, {}, requirement_id=1, trigger="all")


def test_an_oversized_file_is_refused(target):
    with pytest.raises(ReferenceError, match="characters"):
        submit(target, {"src/calc.py": "#" * 100_001}, requirement_id=1, trigger="all")
    assert records(target) == []


def test_an_unlocked_suite_is_refused(target):
    (target / ".git" / "big_brother" / "suite_lock.json").unlink()
    with pytest.raises(NotLocked):
        submit(target, {"src/calc.py": GOOD}, requirement_id=1, trigger="all")


def test_the_red_summary_stays_within_budget(target):
    result = submit(target, {"src/calc.py": BAD}, requirement_id=1, trigger="all")
    assert len(result.summary) <= 500
