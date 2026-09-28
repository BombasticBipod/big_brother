"""The suite lock detects and reverts any change to tests/ made during a build."""
import subprocess
from pathlib import Path

import pytest

from big_brother.suite_lock import DirtyTests, SuiteLock, TestsTampered


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): assert True\n")
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def test_clean_build_reports_no_changes(repo):
    lock = SuiteLock.start(repo)
    (repo / "src" / "a.py").write_text("x = 2\n")
    assert lock.changes() == []
    lock.enforce()


def test_start_refuses_uncommitted_tests(repo):
    (repo / "tests" / "test_a.py").write_text("changed\n")
    with pytest.raises(DirtyTests):
        SuiteLock.start(repo)


def test_start_refuses_untracked_test_file(repo):
    (repo / "tests" / "test_new.py").write_text("new\n")
    with pytest.raises(DirtyTests):
        SuiteLock.start(repo)


def test_detects_modified_added_and_deleted(repo):
    (repo / "tests" / "test_b.py").write_text("b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add b")
    lock = SuiteLock.start(repo)
    (repo / "tests" / "test_a.py").write_text("tampered\n")
    (repo / "tests" / "test_b.py").unlink()
    (repo / "tests" / "conftest.py").write_text("sneaky\n")
    assert lock.changes() == ["tests/conftest.py", "tests/test_a.py", "tests/test_b.py"]


def test_detects_staged_and_committed_changes(repo):
    lock = SuiteLock.start(repo)
    (repo / "tests" / "test_a.py").write_text("tampered\n")
    git(repo, "add", "-A")
    assert lock.changes() == ["tests/test_a.py"]
    git(repo, "commit", "-qm", "builder commits tests")
    assert lock.changes() == ["tests/test_a.py"]


def test_enforce_reverts_tests_and_keeps_src(repo):
    lock = SuiteLock.start(repo)
    (repo / "src" / "a.py").write_text("x = 2\n")
    (repo / "tests" / "test_a.py").write_text("tampered\n")
    (repo / "tests" / "conftest.py").write_text("sneaky\n")
    with pytest.raises(TestsTampered) as err:
        lock.enforce()
    assert err.value.paths == ["tests/conftest.py", "tests/test_a.py"]
    assert (repo / "tests" / "test_a.py").read_text() == "def test_a(): assert True\n"
    assert not (repo / "tests" / "conftest.py").exists()
    assert (repo / "src" / "a.py").read_text() == "x = 2\n"
    assert lock.changes() == []


def test_revert_restores_deleted_and_removes_committed_additions(repo):
    lock = SuiteLock.start(repo)
    (repo / "tests" / "test_a.py").unlink()
    (repo / "tests" / "test_c.py").write_text("c\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "builder rewrites suite")
    assert lock.revert() == ["tests/test_a.py", "tests/test_c.py"]
    assert (repo / "tests" / "test_a.py").exists()
    assert not (repo / "tests" / "test_c.py").exists()
    assert lock.changes() == []


def test_ignored_cache_files_do_not_count(repo):
    (repo / ".gitignore").write_text("__pycache__/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "ignore")
    lock = SuiteLock.start(repo)
    (repo / "tests" / "__pycache__").mkdir()
    (repo / "tests" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    assert lock.changes() == []


def test_custom_tests_dir(repo):
    (repo / "spec").mkdir()
    (repo / "spec" / "s.py").write_text("s\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "spec")
    lock = SuiteLock.start(repo, tests_dir="spec")
    (repo / "tests" / "test_a.py").write_text("not locked here\n")
    (repo / "spec" / "s.py").write_text("tampered\n")
    assert lock.changes() == ["spec/s.py"]
