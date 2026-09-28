"""Lock the test suite during a build.

The builder may only write implementation files. SuiteLock records the commit
the suite was locked at, reports any path under the tests directory that differs
from it (modified, added, deleted, staged or committed), and restores the tests
directory to that commit on demand. Files matched by .gitignore do not count.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class DirtyTests(Exception):
    """The tests directory has uncommitted changes, so there is nothing safe to lock."""


class TestsTampered(Exception):
    """Something changed the tests directory during a build; the change was reverted."""

    __test__ = False  # keep pytest from collecting this as a test class

    def __init__(self, paths: list[str]):
        self.paths = paths
        super().__init__("tests changed during build and were reverted: " + ", ".join(paths))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@dataclass(frozen=True)
class SuiteLock:
    repo: Path
    tests_dir: str
    base: str

    @classmethod
    def start(cls, repo: Path | str, tests_dir: str = "tests") -> SuiteLock:
        repo = Path(repo)
        if _git(repo, "status", "--porcelain", "--untracked-files=all", "--", tests_dir).strip():
            raise DirtyTests(f"commit {tests_dir}/ before building")
        return cls(repo, tests_dir, _git(repo, "rev-parse", "HEAD").strip())

    def changes(self) -> list[str]:
        """Paths under the tests directory that differ from the locked commit."""
        diffed = _git(self.repo, "diff", "--name-only", self.base, "--", self.tests_dir).split()
        untracked = _git(self.repo, "ls-files", "--others", "--exclude-standard",
                         "--", self.tests_dir).split()
        return sorted(set(diffed) | set(untracked))

    def revert(self) -> list[str]:
        """Restore the tests directory to the locked commit and return what was changed."""
        changed = self.changes()
        if changed:
            _git(self.repo, "restore", f"--source={self.base}", "--staged", "--worktree",
                 "--", self.tests_dir)
            _git(self.repo, "clean", "-fdq", "--", self.tests_dir)
        return changed

    def enforce(self) -> None:
        """Raise TestsTampered after reverting, if the suite changed."""
        changed = self.revert()
        if changed:
            raise TestsTampered(changed)
