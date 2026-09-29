"""Staging holds proposed tests and interface files between tool calls, then commits them."""
import os
import subprocess
from pathlib import Path

import pytest

from big_brother.runlock import BuildBusy, run_lock
from big_brother.staging import StagingError, Staging, staging_path
from big_brother.suite_lock import SuiteLock

CALC_PYI = "def add(a: int, b: int) -> int: ...\n"
TEST_ADD = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path):
    root = tmp_path / "target"
    (root / "interface").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "interface" / "calc.pyi").write_text(CALC_PYI)
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests" / "test_old.py").write_text("def test_old():\n    pass\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "target")
    SuiteLock(root).lock()
    yield root
    for d, _, _ in os.walk(root):  # let tmp cleanup delete read-only dirs
        os.chmod(d, 0o755)


def test_staged_files_live_in_the_git_directory(repo):
    Staging(repo).propose("tests/test_add.py", TEST_ADD)
    assert staging_path(repo) == repo / ".git" / "big_brother" / "staged"
    assert (staging_path(repo) / "tests" / "test_add.py").read_text() == TEST_ADD
    assert not (repo / "tests" / "test_add.py").exists()


def test_propose_returns_the_red_check_of_every_staged_test(repo):
    staging = Staging(repo)
    staging.propose("tests/test_add.py", TEST_ADD)
    result = staging.propose("tests/test_more.py",
                             "from calc import add\n\n\ndef test_more():\n    assert add(0, 0) == 0\n")
    assert result.status == "red" and result.summary == "red: 2 tests fail correctly"
    assert staging.paths() == ["tests/test_add.py", "tests/test_more.py"]


def test_a_staged_interface_is_used_by_the_red_check(repo):
    staging = Staging(repo)
    staging.propose("interface/shapes.pyi", "def area(w: int, h: int) -> int: ...\n")
    result = staging.propose("tests/test_area.py",
                             "from shapes import area\n\n\ndef test_area():\n    assert area(2, 3) == 6\n")
    assert result.status == "red"


def test_proposing_only_an_interface_checks_that_it_parses(repo):
    staging = Staging(repo)
    ok = staging.propose("interface/shapes.pyi", "def area(w: int, h: int) -> int: ...\n")
    assert (ok.status, ok.summary) == ("ok", "interface ok: 1 committed test not broken")
    bad = staging.propose("interface/broken.pyi", "def area(:\n")
    assert bad.status == "broken" and "interface/broken.pyi" in bad.summary


def test_a_test_that_passes_on_stubs_is_reported(repo):
    result = Staging(repo).propose("tests/test_nothing.py", "def test_nothing():\n    pass\n")
    assert result.status == "passes_on_stubs"


@pytest.mark.parametrize("path", [
    "src/calc.py", "tests/../src/calc.py", "/etc/passwd", "tests/notes.txt",
    "interface/calc.py", "tests", "", "tests/sub/../../src/x.py", "tests/__pycache__/x.py",
])
def test_paths_outside_the_suite_are_refused(repo, path):
    with pytest.raises(StagingError):
        Staging(repo).propose(path, "x = 1\n")
    assert Staging(repo).paths() == []


def test_a_path_through_a_symlink_in_the_target_is_refused(repo):
    os.chmod(repo / "tests", 0o755)
    (repo / "tests" / "linked").symlink_to(repo / "src")
    with pytest.raises(StagingError):
        Staging(repo).propose("tests/linked/test_x.py", TEST_ADD)


def test_oversized_content_is_refused(repo):
    with pytest.raises(StagingError):
        Staging(repo).propose("tests/test_big.py", "x = 1\n" * 100_000)


def test_commit_writes_staged_files_through_the_lock(repo):
    staging = Staging(repo)
    staging.propose("interface/shapes.pyi", "def area(w: int, h: int) -> int: ...\n")
    staging.propose("tests/test_add.py", TEST_ADD)
    commit = staging.commit("add tests for add and area")
    assert commit == git(repo, "rev-parse", "HEAD").strip()
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == [
        "interface/shapes.pyi", "tests/test_add.py"]
    lock = SuiteLock(repo)
    assert lock.base == commit and lock.changes() == []
    assert not os.access(repo / "tests" / "test_add.py", os.W_OK)
    assert staging.paths() == []


def test_commit_with_nothing_staged_is_refused(repo):
    with pytest.raises(StagingError):
        Staging(repo).commit("nothing")


def test_commit_refuses_a_staged_test_that_is_not_red(repo):
    staging = Staging(repo)
    staging.propose("tests/test_nothing.py", "def test_nothing():\n    pass\n")
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(StagingError):
        staging.commit("weak test")
    assert git(repo, "rev-parse", "HEAD") == head
    assert staging.paths() == ["tests/test_nothing.py"]


def test_commit_waits_for_no_build(repo):
    staging = Staging(repo)
    staging.propose("tests/test_add.py", TEST_ADD)
    with run_lock(repo), pytest.raises(BuildBusy):
        staging.commit("during a build")
    assert staging.paths() == ["tests/test_add.py"]


def test_discard_empties_the_staging_area(repo):
    staging = Staging(repo)
    staging.propose("tests/test_add.py", TEST_ADD)
    staging.discard()
    assert staging.paths() == []


def test_propose_and_discard_wait_for_no_build(repo):
    staging = Staging(repo)
    with run_lock(repo):
        with pytest.raises(BuildBusy):
            staging.propose("tests/test_add.py", TEST_ADD)
        with pytest.raises(BuildBusy):
            staging.discard()
    assert staging.paths() == []


def test_a_staged_interface_that_breaks_committed_tests_is_reported(tmp_path):
    root = tmp_path / "target"
    (root / "interface").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "interface" / "calc.pyi").write_text(CALC_PYI)
    (root / "tests" / "test_add.py").write_text(TEST_ADD)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "target")
    SuiteLock(root).lock()
    staging = Staging(root)
    result = staging.propose("interface/calc.pyi", "def plus(a: int, b: int) -> int: ...\n")
    assert result.status == "broken" and "tests/test_add.py" in result.summary
    with pytest.raises(StagingError):
        staging.commit("rename add")
    for d, _, _ in os.walk(root):
        os.chmod(d, 0o755)


def test_an_interface_with_no_tests_anywhere_must_still_make_stubs(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    staging = Staging(root)
    ok = staging.propose("interface/shapes.pyi", "def area(w: int, h: int) -> int: ...\n")
    assert (ok.status, ok.summary) == ("ok", "interface ok: 1 module")
    assert staging.propose("interface/bad.pyi", "def f(:\n").status == "broken"
