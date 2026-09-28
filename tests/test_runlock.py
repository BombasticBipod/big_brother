"""Runs take turns: a build and a suite change never overlap."""
import subprocess
import sys
from pathlib import Path

import pytest

from big_brother.runlock import BuildBusy, run_lock
from big_brother.suite_lock import SuiteLock


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_second_holder_in_same_process_is_refused(repo):
    with run_lock(repo):
        with pytest.raises(BuildBusy):
            with run_lock(repo):
                pass


def test_lock_is_released_after_the_block(repo):
    with run_lock(repo):
        pass
    with run_lock(repo):
        pass


def test_lock_is_released_when_the_block_raises(repo):
    with pytest.raises(RuntimeError):
        with run_lock(repo):
            raise RuntimeError("boom")
    with run_lock(repo):
        pass


def test_other_process_is_refused_while_held(repo):
    probe = ("import sys\nfrom big_brother.runlock import BuildBusy, run_lock\n"
             "try:\n    with run_lock(sys.argv[1]):\n        pass\n"
             "except BuildBusy:\n    sys.exit(3)\n")
    with run_lock(repo):
        held = subprocess.run([sys.executable, "-c", probe, str(repo)]).returncode
    free = subprocess.run([sys.executable, "-c", probe, str(repo)]).returncode
    assert (held, free) == (3, 0)


def test_accept_is_refused_while_a_build_runs(repo):
    lock = SuiteLock(repo)
    lock.lock()
    with run_lock(repo):
        with pytest.raises(BuildBusy):
            with lock.accept("change"):
                (repo / "tests" / "test_b.py").write_text("def test_b():\n    pass\n")
    assert not (repo / "tests" / "test_b.py").exists()
    assert lock.is_locked()


def test_lock_file_lives_in_the_git_dir(repo):
    with run_lock(repo):
        pass
    assert (repo / ".git" / "big_brother" / "build.lock").exists()
    assert git(repo, "status", "--porcelain") == ""
