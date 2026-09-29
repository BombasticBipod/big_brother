"""Keep the test suite locked at all times.

The suite is the contract the test writer owns: the tests directory and the
interface directory together. A locked suite is read-only on disk, and its
state (the commit it was locked at) lives in the git directory, so the lock
outlasts any one process. The only way to change the suite is `accept()`,
which unlocks, lets the writer edit, commits both directories and locks again
at the new commit.

The state file keys the commit by the tests directory, as it did before the
lock covered the interface, so a lock made then still reads as locked and now
guards both directories.

File permissions stop accidental writes. They do not stop a process that
chmods its way in, so `changes()` compares both directories with the locked
commit (modified, added, deleted, staged or committed) and `enforce()` reverts
anything it finds. Files matched by .gitignore do not count.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from big_brother.runlock import run_lock

WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


class DirtyTests(Exception):
    """The tests directory has uncommitted changes, so there is nothing safe to lock."""


class NotLocked(Exception):
    """The operation needs a locked suite and this one is not locked."""


class TestsTampered(Exception):
    """Something changed the locked tests directory; the change was reverted."""

    __test__ = False  # keep pytest from collecting this as a test class

    def __init__(self, paths: list[str]):
        self.paths = paths
        super().__init__("locked tests were changed and have been reverted: " + ", ".join(paths))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


class SuiteLock:
    def __init__(self, repo: Path | str, tests_dir: str = "tests",
                 interface_dir: str = "interface"):
        self.repo = Path(repo)
        self.tests_dir = tests_dir
        self.dirs = [tests_dir, interface_dir]
        git_dir = Path(_git(self.repo, "rev-parse", "--absolute-git-dir").strip())
        self._state_file = git_dir / "big_brother" / "suite_lock.json"
        self._pinned: str | None = None

    # state

    def _state(self) -> dict[str, str]:
        try:
            return json.loads(self._state_file.read_text())
        except FileNotFoundError:
            return {}

    def _save_base(self, base: str) -> None:
        state = self._state()
        state[self.tests_dir] = base
        self._state_file.parent.mkdir(exist_ok=True)
        self._state_file.write_text(json.dumps(state, indent=2) + "\n")

    @property
    def base(self) -> str:
        if self._pinned is not None:
            return self._pinned
        try:
            return self._state()[self.tests_dir]
        except KeyError:
            raise NotLocked(f"{self.tests_dir}/ is not locked") from None

    def pin(self) -> None:
        """Hold the locked commit in memory, so rewriting the state file cannot move it.

        A build pins its lock: code under build runs with the user's permissions
        and could otherwise commit a test change and point the state at it.
        """
        self._pinned = None
        self._pinned = self.base

    def _state_moved(self) -> bool:
        if self._pinned is None:
            return False
        if self._state().get(self.tests_dir) != self._pinned:
            self._save_base(self._pinned)
            return True
        return False

    def is_locked(self) -> bool:
        return self.tests_dir in self._state()

    # permissions

    def _present(self) -> list[str]:
        """The suite directories git can take as a pathspec: on disk or in the index."""
        return [d for d in self.dirs
                if (self.repo / d).exists() or _git(self.repo, "ls-files", "--", d).strip()]

    def _set_writable(self, writable: bool) -> None:
        paths: list[Path] = []
        for d in self.dirs:
            root = self.repo / d
            if not root.is_dir() or root.is_symlink():
                continue
            paths.append(root)
            for dirpath, dirnames, filenames in os.walk(root):
                paths += [Path(dirpath, n) for n in dirnames + filenames]
        # Unlock parents before children and lock children before parents.
        for path in paths if writable else reversed(paths):
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            path.chmod(mode | stat.S_IWUSR if writable else mode & ~WRITE_BITS)

    # locking

    def lock(self) -> None:
        """Lock the suite at HEAD. Both suite directories must be fully committed."""
        if _git(self.repo, "status", "--porcelain", "--untracked-files=all",
                "--", *self.dirs).strip():
            raise DirtyTests(f"commit {' and '.join(d + '/' for d in self.dirs)} before locking")
        self._save_base(_git(self.repo, "rev-parse", "HEAD").strip())
        self._set_writable(False)

    def changes(self) -> list[str]:
        """Paths under the suite directories that differ from the locked commit."""
        diffed = _git(self.repo, "diff", "--name-only", self.base, "--", *self.dirs).split()
        untracked = _git(self.repo, "ls-files", "--others", "--exclude-standard",
                         "--", *self.dirs).split()
        return sorted(set(diffed) | set(untracked))

    def _restore(self) -> None:
        self._set_writable(True)
        diffed = _git(self.repo, "diff", "--name-only", self.base, "--", *self.dirs).split()
        if diffed:
            _git(self.repo, "restore", f"--source={self.base}", "--staged", "--worktree",
                 "--", *diffed)
        present = self._present()
        if present:
            _git(self.repo, "clean", "-fdq", "--", *present)
        self._set_writable(False)

    def revert(self) -> list[str]:
        """Restore the tests directory to the locked commit and return what was changed."""
        changed = self.changes()
        if changed:
            self._restore()
        return changed

    def enforce(self) -> None:
        """Raise TestsTampered after reverting, if the locked suite or a pinned state changed."""
        moved = self._state_moved()
        changed = self.revert()
        if moved:
            changed.append(str(self._state_file.relative_to(self.repo)))
        if changed:
            raise TestsTampered(changed)

    @contextmanager
    def accept(self, message: str) -> Iterator[None]:
        """Unlock for the block, then commit both suite directories and lock again.

        If the block or the commit fails, the changes are discarded and the suite stays at
        the old commit. Tampering found before unlocking is reverted and raised. Raises
        BuildBusy, before unlocking, if a build is running.
        """
        with run_lock(self.repo):
            self.enforce()
            self._set_writable(True)
            try:
                yield
                present = self._present()
                if present:
                    _git(self.repo, "add", "-A", "--", *present)
                staged = subprocess.run(["git", "-C", str(self.repo), "diff", "--cached",
                                         "--quiet", "--", *self.dirs]).returncode
                if staged:
                    _git(self.repo, "commit", "-qm", message, "--", *present)
            except BaseException:
                self._restore()
                raise
            self.lock()
