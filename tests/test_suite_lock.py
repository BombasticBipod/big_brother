"""The suite stays locked at all times; it unlocks only to accept committed changes."""
import os
import subprocess
from pathlib import Path

import pytest

from big_brother.suite_lock import DirtyTests, NotLocked, SuiteLock, TestsTampered


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def writable(path: Path) -> bool:
    return os.access(path, os.W_OK)


@pytest.fixture
def repo(tmp_path: Path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): assert True\n")
    (tmp_path / "tests" / "unit" / "test_u.py").write_text("u\n")
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    yield tmp_path
    for d, _, _ in os.walk(tmp_path):  # let tmp cleanup delete read-only dirs
        os.chmod(d, 0o755)


@pytest.fixture
def lock(repo) -> SuiteLock:
    lock = SuiteLock(repo)
    lock.lock()
    return lock


def tamper(repo: Path, path: str, text: str) -> None:
    """Simulate a builder that forces its way past file permissions."""
    target = repo / path
    os.chmod(target.parent, 0o755)
    if target.exists():
        os.chmod(target, 0o644)
    target.write_text(text)


# locking

def test_lock_makes_tests_read_only_and_leaves_src_alone(repo, lock):
    assert lock.is_locked()
    assert not writable(repo / "tests" / "test_a.py")
    assert not writable(repo / "tests")
    assert not writable(repo / "tests" / "unit")
    assert writable(repo / "src" / "a.py")
    with pytest.raises(PermissionError):
        (repo / "tests" / "test_new.py").write_text("x\n")


def test_lock_state_survives_a_new_instance(repo, lock):
    again = SuiteLock(repo)
    assert again.is_locked()
    assert again.base == lock.base


def test_fresh_repo_is_not_locked(repo):
    assert not SuiteLock(repo).is_locked()
    with pytest.raises(NotLocked):
        SuiteLock(repo).changes()


def test_lock_refuses_uncommitted_tests(repo):
    (repo / "tests" / "test_a.py").write_text("changed\n")
    with pytest.raises(DirtyTests):
        SuiteLock(repo).lock()
    (repo / "tests" / "test_a.py").write_text("def test_a(): assert True\n")
    (repo / "tests" / "test_new.py").write_text("new\n")
    with pytest.raises(DirtyTests):
        SuiteLock(repo).lock()


# detection and revert

def test_src_changes_are_not_tampering(repo, lock):
    (repo / "src" / "a.py").write_text("x = 2\n")
    assert lock.changes() == []
    lock.enforce()


def test_detects_forced_modify_add_delete(repo, lock):
    tamper(repo, "tests/test_a.py", "tampered\n")
    tamper(repo, "tests/conftest.py", "sneaky\n")
    os.chmod(repo / "tests" / "unit", 0o755)
    (repo / "tests" / "unit" / "test_u.py").unlink()
    assert lock.changes() == ["tests/conftest.py", "tests/test_a.py", "tests/unit/test_u.py"]


def test_detects_committed_tampering(repo, lock):
    tamper(repo, "tests/test_a.py", "tampered\n")
    git(repo, "commit", "-qam", "builder commits tests")
    assert lock.changes() == ["tests/test_a.py"]


def test_enforce_reverts_relocks_and_keeps_src(repo, lock):
    (repo / "src" / "a.py").write_text("x = 2\n")
    tamper(repo, "tests/test_a.py", "tampered\n")
    tamper(repo, "tests/conftest.py", "sneaky\n")
    with pytest.raises(TestsTampered) as err:
        lock.enforce()
    assert err.value.paths == ["tests/conftest.py", "tests/test_a.py"]
    assert (repo / "tests" / "test_a.py").read_text() == "def test_a(): assert True\n"
    assert not (repo / "tests" / "conftest.py").exists()
    assert (repo / "src" / "a.py").read_text() == "x = 2\n"
    assert lock.changes() == []
    assert not writable(repo / "tests" / "test_a.py")
    assert not writable(repo / "tests")


def test_revert_undoes_committed_rewrite(repo, lock):
    tamper(repo, "tests/test_c.py", "c\n")
    os.chmod(repo / "tests" / "test_a.py", 0o644)
    (repo / "tests" / "test_a.py").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "builder rewrites suite")
    assert lock.revert() == ["tests/test_a.py", "tests/test_c.py"]
    assert (repo / "tests" / "test_a.py").exists()
    assert not (repo / "tests" / "test_c.py").exists()


def test_ignored_cache_files_do_not_count(repo, lock):
    os.chmod(repo / "tests", 0o755)
    (repo / "tests" / "__pycache__").mkdir()
    (repo / "tests" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    assert lock.changes() == []


# accepting changes

def test_accept_unlocks_commits_and_relocks(repo, lock):
    old_base = lock.base
    with lock.accept("add test_b"):
        assert writable(repo / "tests")
        (repo / "tests" / "test_b.py").write_text("def test_b(): pass\n")
        (repo / "tests" / "test_a.py").write_text("def test_a(): assert 1\n")
    assert lock.is_locked()
    assert lock.base != old_base
    assert lock.changes() == []
    assert not writable(repo / "tests" / "test_b.py")
    assert git(repo, "log", "-1", "--format=%s").strip() == "add test_b"
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == [
        "tests/test_a.py", "tests/test_b.py"]


def test_accept_commits_only_tests(repo, lock):
    (repo / "src" / "a.py").write_text("x = 2\n")
    with lock.accept("tests only"):
        (repo / "tests" / "test_b.py").write_text("b\n")
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["tests/test_b.py"]
    assert "src/a.py" in git(repo, "status", "--porcelain")


def test_accept_with_no_changes_makes_no_commit(repo, lock):
    head = git(repo, "rev-parse", "HEAD")
    with lock.accept("nothing"):
        pass
    assert git(repo, "rev-parse", "HEAD") == head
    assert lock.is_locked()


def test_accept_relocks_without_commit_on_error(repo, lock):
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(RuntimeError):
        with lock.accept("half done"):
            (repo / "tests" / "test_b.py").write_text("b\n")
            raise RuntimeError("writer crashed")
    assert git(repo, "rev-parse", "HEAD") == head
    assert not (repo / "tests" / "test_b.py").exists()
    assert lock.is_locked()
    assert not writable(repo / "tests")


def test_accept_refuses_when_suite_was_tampered(repo, lock):
    tamper(repo, "tests/test_a.py", "tampered\n")
    with pytest.raises(TestsTampered):
        with lock.accept("smuggle"):
            pass
    assert (repo / "tests" / "test_a.py").read_text() == "def test_a(): assert True\n"


def test_custom_tests_dir(repo):
    (repo / "spec").mkdir()
    (repo / "spec" / "s.py").write_text("s\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "spec")
    lock = SuiteLock(repo, tests_dir="spec")
    lock.lock()
    assert writable(repo / "tests" / "test_a.py")
    tamper(repo, "spec/s.py", "tampered\n")
    assert lock.changes() == ["spec/s.py"]
    assert not SuiteLock(repo).is_locked()
