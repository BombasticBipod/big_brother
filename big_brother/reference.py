"""Reference answers: the test writer's own implementation, kept as training data.

The test writer may submit its own version of `src/` for a requirement. The
server decides when to ask, by mode: `none` never, `stuck` after a stuck
build, `all` after every commit of tests. `submit` runs the locked suite
against the submitted files alone, in the sandbox, with none of the builder's
`src/` present, and appends one JSON line per submission to
`.git/big_brother/reference/references.jsonl`. Red submissions are kept too,
marked `green: false`, so a training set filters on that field.

The builder never sees a reference: it lives under `.git/big_brother/`, which
the builder's prompt never includes and its sandbox never mounts, and the
target's working tree is not touched. The record keeps the locked suite
commit and the `src/` commit, which is enough to rebuild the prompt the
builder got for that suite.

Only files the interface declares are accepted, for the same reason as in
the builder: tests run with `PYTHONPATH` set to `src/`, so a stray
`src/sitecustomize.py` could fake a green run and poison the data.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from big_brother.builder import _git, _line, _run_tests, allowed_paths
from big_brother.red_check import fit
from big_brother.runlock import run_lock
from big_brother.sandbox import require_bwrap
from big_brother.staging import CONTENT_BUDGET
from big_brother.suite_lock import SuiteLock

MODES = ("none", "stuck", "all")


class ReferenceError(ValueError):
    """A submitted reference was refused before anything ran."""


@dataclass(frozen=True)
class ReferenceResult:
    green: bool
    summary: str


def reference_log_path(repo: Path | str) -> Path:
    git_dir = Path(_git(Path(repo), "rev-parse", "--absolute-git-dir").strip())
    return git_dir / "big_brother" / "reference" / "references.jsonl"


def _check(repo: Path, files: dict[str, str], interface_dir: str, src_dir: str) -> None:
    if not files:
        raise ReferenceError("give at least one file, e.g. {\"src/calc.py\": \"...\"}")
    allowed = allowed_paths(repo, interface_dir, src_dir)
    refused = sorted(set(files) - allowed)
    if refused:
        raise ReferenceError(f"refused {', '.join(refused)}: submit only the files the "
                             f"interface declares: {', '.join(sorted(allowed))}")
    for rel, body in files.items():
        if len(body) > CONTENT_BUDGET:
            raise ReferenceError(f"{rel}: keep a file under {CONTENT_BUDGET} characters")


def submit(repo: Path | str, files: dict[str, str], requirement_id: int, trigger: str,
           tests_dir: str = "tests", interface_dir: str = "interface", src_dir: str = "src",
           timeout: float = 120, sandbox: bool = True) -> ReferenceResult:
    """Run the locked suite on `files` alone and record the submission."""
    repo = Path(repo)
    _check(repo, files, interface_dir, src_dir)
    if sandbox:
        require_bwrap()
    lock = SuiteLock(repo, tests_dir, interface_dir)
    with run_lock(repo):
        lock.enforce()
        run = _run_tests(repo, tests_dir, src_dir, timeout, sandbox, src_files=files)
        lock.enforce()
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "requirement_id": requirement_id,
            "trigger": trigger,
            "suite_commit": lock.base,
            "src_commit": _git(repo, "rev-parse", "HEAD").strip(),
            "files": files,
            "green": run.green,
            "passed": run.passed,
            "failed": run.failed,
        }
        path = reference_log_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as log:
            log.write(json.dumps(record) + "\n")
    if run.green:
        return ReferenceResult(True, f"reference green: {run.passed} tests pass")
    if run.returncode is None:
        return ReferenceResult(False, "reference red: tests timed out")
    lines = [_line(t, repo / tests_dir) for t in run.outcomes if t.outcome != "passed"]
    return ReferenceResult(False, fit("reference red: ", lines) if lines
                           else f"reference red: pytest exited {run.returncode}")
