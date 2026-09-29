"""Hold proposed suite files between tool calls, check them, then commit them through the lock.

The suite lock only unlocks inside one `accept()` block, but the test writer
proposes files over several tool calls. Proposed files therefore wait in
`.git/big_brother/staged/`, mirroring their target paths (`tests/...`,
`interface/...`), and never touch the target until `commit()`.

- `propose(path, content)` refuses anything outside the tests directory
  (`.py`) or the interface directory (`.pyi`), any path through a symlink in
  the target, and oversized content. It then checks everything staged and
  returns the result.
- `check()` runs the red check with the staged files laid over the committed
  ones. Staged tests must be red. When an interface file is staged, the
  committed tests run too and must not be broken, because a changed signature
  would otherwise surface only as a stuck build. With no tests to run, the
  merged interface must still turn into stubs.
- `commit(message)` re-checks, then copies the staged files in inside
  `SuiteLock.accept()`, which commits both directories and relocks.

Staging changes only while no build runs: `propose` and `discard` take the run
lock, and `commit` takes it through `accept()`. A build refuses to start while
anything is staged, so a staged file that appears during a build was planted.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from big_brother.red_check import RedResult, _summarize, red_check
from big_brother.runlock import run_lock
from big_brother.stubs import InterfaceError, make_stubs
from big_brother.suite_lock import SuiteLock

CONTENT_BUDGET = 100_000   # characters in one proposed file


class StagingError(Exception):
    """A proposed path or content is refused, or the staged suite is not ready to commit."""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def staging_path(repo: Path | str) -> Path:
    return Path(_git(Path(repo), "rev-parse", "--absolute-git-dir").strip()) / "big_brother" / "staged"


def _is_test(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.startswith("test_") or name.endswith("_test.py")


class Staging:
    def __init__(self, repo: Path | str, tests_dir: str = "tests", interface_dir: str = "interface"):
        self.repo = Path(repo)
        self.tests_dir, self.interface_dir = tests_dir, interface_dir
        self.root = staging_path(self.repo)

    def paths(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
                      if not p.is_dir())

    def _validate(self, path: str, content: str) -> PurePosixPath:
        rel = PurePosixPath(path)
        parts = rel.parts
        suffix = {self.tests_dir: ".py", self.interface_dir: ".pyi"}.get(parts[0] if parts else "")
        if (not path or rel.is_absolute() or len(parts) < 2 or suffix is None
                or rel.suffix != suffix or any(p in ("..", ".", "__pycache__") for p in parts)
                or rel.as_posix() != path):
            raise StagingError(f"{path!r}: write only {self.tests_dir}/**/*.py "
                               f"or {self.interface_dir}/**/*.pyi")
        for i in range(1, len(parts) + 1):
            if (self.repo / Path(*parts[:i])).is_symlink():
                raise StagingError(f"{path}: goes through a symlink in the target")
        if len(content) > CONTENT_BUDGET:
            raise StagingError(f"{path}: over {CONTENT_BUDGET} characters")
        return rel

    def propose(self, path: str, content: str) -> RedResult:
        """Stage one file and return the check of everything staged."""
        rel = self._validate(path, content)
        with run_lock(self.repo):
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return self.check()

    def discard(self) -> None:
        with run_lock(self.repo):
            shutil.rmtree(self.root, ignore_errors=True)

    def check(self) -> RedResult:
        staged = self.paths()
        new_tests = [p for p in staged if p.startswith(self.tests_dir + "/") and _is_test(p)]
        interface_staged = any(p.startswith(self.interface_dir + "/") for p in staged)
        old_tests = []
        if interface_staged:
            root = self.repo / self.tests_dir
            old_tests = sorted(p.relative_to(self.repo).as_posix() for p in root.rglob("*.py")
                               if _is_test(p.name)) if root.is_dir() else []
            old_tests = [p for p in old_tests if p not in new_tests]
        if not new_tests and not old_tests:
            return self._interface_only()
        result = red_check(self.repo, new_tests + old_tests, self.interface_dir, self.tests_dir,
                           overlay=self.root)
        if not result.tests:
            return result
        new = [t for t in result.tests if t.nodeid.split("::")[0] in new_tests]
        old = [t for t in result.tests if t.nodeid.split("::")[0] not in new_tests]
        if any(t.verdict == "broken" for t in result.tests):
            return RedResult("broken", result.tests, _summarize("broken", result.tests))
        if any(t.verdict == "passes" for t in new):
            return RedResult("passes_on_stubs", new, _summarize("passes_on_stubs", new))
        if not new:
            n = len(old)
            return RedResult("ok", old, f"interface ok: {n} committed test{'s' if n != 1 else ''} "
                                        "not broken")
        return RedResult("red", new, _summarize("red", new))

    def _interface_only(self) -> RedResult:
        with tempfile.TemporaryDirectory(prefix="big_brother_stage_") as tmp_name:
            merged = Path(tmp_name) / self.interface_dir
            merged.mkdir()
            for layer in (self.repo / self.interface_dir, self.root / self.interface_dir):
                if layer.is_dir():
                    shutil.copytree(layer, merged, dirs_exist_ok=True,
                                    copy_function=shutil.copyfile)
            try:
                n = len(make_stubs(merged, Path(tmp_name) / "stubs"))
            except InterfaceError as err:
                return RedResult("broken", [], _summarize("broken", [], f"interface: {err}"))
        return RedResult("ok", [], f"interface ok: {n} module{'s' if n != 1 else ''}")

    def commit(self, message: str) -> str:
        """Check the staged files, commit them through the lock, empty staging; return HEAD."""
        staged = self.paths()
        if not staged:
            raise StagingError("nothing is staged")
        result = self.check()
        if result.status not in ("red", "ok"):
            raise StagingError(result.summary)
        lock = SuiteLock(self.repo, self.tests_dir, self.interface_dir)
        with lock.accept(message):
            for rel in staged:
                target = self.repo / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.root / rel, target)
        shutil.rmtree(self.root, ignore_errors=True)
        return _git(self.repo, "rev-parse", "HEAD").strip()
