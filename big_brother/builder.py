"""The builder loop: run the locked suite against the target's src/, ask the model to fix it.

`build` refuses to start unless the suite is locked and src/ is fully
committed, and holds the run lock for its whole length. Each try sends the
model the interface, the current source, the failing test files and the
pytest output, then writes back only the files the reply names that the
interface declares (`src/<module>.py` for each `interface/<module>.pyi`). Any
other path is refused and reported back, so a reply cannot plant
`src/pytest.py` or `src/sitecustomize.py` to fake a green run. The suite lock
is enforced after writing and again after the test run, so a green result
always comes from the locked tests.

Green commits src/ only. Stuck, or any exception, restores src/ to HEAD.

Code under build is untrusted, so the tests run in a temporary copy of `src/`
and `tests/` inside a bubblewrap sandbox (big_brother.sandbox): no target, no
`.git`, no home directory, no network. Behind the sandbox, as defense in
depth, the build pins the locked commit in memory
(rewriting the state file is caught), refuses to start while suite files are
staged, and treats any staged file that appears during the build as tampering.

Two audiences, two levels of detail. The summary and the progress lines are
for the test writer, who must never see implementation text, so they carry
counts, test ids and exception types, plus the message of an AssertionError
raised in a test file. Everything else (prompts, replies, full pytest
output) goes to the build log in `.git/big_brother/build.log`, which the test
writer must not read.

Usage: python -m big_brother.builder [REPO] [--max-tries N] [--model NAME]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from big_brother.ollama import DEFAULT_HOST, DEFAULT_MODEL, OllamaClient, ollama_on_demand
from big_brother.red_check import MESSAGE_BUDGET, TestOutcome, _clip, _outcomes, fit
from big_brother.runlock import run_lock
from big_brother.sandbox import require_bwrap, sandboxed
from big_brother.staging import StagingError, staging_path
from big_brother.suite_lock import SuiteLock, TestsTampered

FILE_BUDGET = 6000      # characters of any one file shown to the model
OUTPUT_BUDGET = 4000    # characters of pytest output shown to the model (the tail)

FILE_BLOCK = re.compile(r"^[ \t]*(?:\*\*)?FILE:[ \t]*`?([^`\s*]+)`?(?:\*\*)?[ \t]*\n+"
                        r"```[^\n]*\n(.*?)^```", re.M | re.S)
FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)

SYSTEM = """You write Python implementation files so that a pytest suite passes.

Answer with one block per file you change, in exactly this form:

FILE: src/<module>.py
```python
<the complete file>
```

Rules:
- You may write only these files: {allowed}. Any other path is refused.
- Always write the complete file, never a diff or a fragment.
- The tests and the interface are fixed. Change only the implementation.
- Implement what the interface declares, with the same names and signatures."""


class DirtySrc(Exception):
    """src/ has uncommitted changes, which a stuck build would throw away."""


class Model(Protocol):
    def chat(self, messages: list[dict]) -> str: ...


@dataclass(frozen=True)
class BuildResult:
    status: str     # "green" or "stuck"
    tries: int      # model calls made
    summary: str


@dataclass(frozen=True)
class TestRun:
    __test__ = False  # keep pytest from collecting this as a test class

    outcomes: list[TestOutcome]
    output: str
    returncode: int | None

    @property
    def passed(self) -> int:
        return sum(t.outcome == "passed" for t in self.outcomes)

    @property
    def failed(self) -> int:
        return len(self.outcomes) - self.passed

    @property
    def green(self) -> bool:
        return self.returncode == 0 and self.passed > 0 and self.failed == 0


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def build_log_path(repo: Path | str) -> Path:
    return Path(_git(Path(repo), "rev-parse", "--absolute-git-dir").strip()) / "big_brother" / "build.log"


def allowed_paths(repo: Path | str, interface_dir: str = "interface", src_dir: str = "src") -> set[str]:
    """The src/ files the interface declares; the only files a reply may write."""
    root = Path(repo) / interface_dir
    return {f"{src_dir}/{p.relative_to(root).with_suffix('.py').as_posix()}"
            for p in root.rglob("*.pyi")}


def parse_reply(text: str, allowed: set[str]) -> tuple[dict[str, str], list[str]]:
    """Files named in a reply, split into those allowed and the refused paths."""
    files: dict[str, str] = {}
    refused: list[str] = []
    for m in FILE_BLOCK.finditer(text):
        path, body = m.group(1), m.group(2)
        if path in allowed:
            files[path] = body
        else:
            refused.append(path)
    if not files and not refused and len(allowed) == 1:
        blocks = FENCE.findall(text)
        if len(blocks) == 1:
            files[next(iter(allowed))] = blocks[0]
    return files, refused


def _run_tests(repo: Path, tests_dir: str, src_dir: str, timeout: float,
               sandbox: bool = True) -> TestRun:
    """Run the suite in a sandboxed copy of src/ and tests/, reporting paths as the target's."""
    with tempfile.TemporaryDirectory(prefix="big_brother_build_") as tmp:
        work = Path(tmp) / "work"
        work.mkdir()
        for d in (src_dir, tests_dir):
            if (repo / d).is_dir():
                shutil.copytree(repo / d, work / d, symlinks=True, copy_function=shutil.copyfile,
                                ignore=shutil.ignore_patterns("__pycache__"))
        out = Path(tmp) / "records.jsonl"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tmp,
            "PYTHONPATH": str(work / src_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
            "BIG_BROTHER_REDCHECK_OUT": str(out),
        }
        cmd = [sys.executable, "-m", "pytest", "-q", "--tb=short", "--rootdir", str(work),
               "-p", "no:cacheprovider", "-p", "big_brother.redcheck_plugin", tests_dir]
        if sandbox:
            cmd = sandboxed(cmd, writable=tmp, cwd=work)
        try:
            proc = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return TestRun([], f"pytest timed out after {timeout}s", None)
        records = [json.loads(l) for l in out.read_text().splitlines()] if out.exists() else []
    for r in records:
        if r.get("raised_in") and Path(r["raised_in"]).is_relative_to(work):
            r["raised_in"] = str(repo / Path(r["raised_in"]).relative_to(work))
    output = (proc.stdout + proc.stderr).replace(str(work), str(repo))
    return TestRun(_outcomes(records), output, proc.returncode)


def _staged(repo: Path) -> list[str]:
    root = staging_path(repo)
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if not p.is_dir()) if root.is_dir() else []


def _guard(repo: Path, lock: SuiteLock) -> None:
    """Enforce the pinned lock, and wipe and report any staged file planted during the build."""
    planted = _staged(repo)
    if planted:
        shutil.rmtree(staging_path(repo))
    try:
        lock.enforce()
    except TestsTampered as err:
        raise TestsTampered(err.paths + [f"staged/{p}" for p in planted]) from None
    if planted:
        raise TestsTampered([f"staged/{p}" for p in planted])


def _from_tests(t: TestOutcome, tests_root: Path) -> bool:
    return bool(t.raised_in) and Path(t.raised_in).resolve().is_relative_to(tests_root.resolve())


def _line(t: TestOutcome, tests_root: Path) -> str:
    # Only an AssertionError raised in a test file is safe to quote; anything raised
    # in src/, including an assert there, may carry implementation text.
    safe = t.exc_type == "AssertionError" and t.message and _from_tests(t, tests_root)
    detail = t.message if safe else t.exc_type or t.outcome
    return _clip(f"{t.nodeid} [{t.when}] {detail}", MESSAGE_BUDGET)


def _plural(n: int) -> str:
    return f"{n} try" if n == 1 else f"{n} tries"


def _stuck_summary(tries: int, run: TestRun, tests_root: Path) -> str:
    prefix = f"stuck after {_plural(tries)}: "
    if run.returncode is None:
        return prefix + "tests timed out"
    lines = [_line(t, tests_root) for t in run.outcomes if t.outcome != "passed"]
    return fit(prefix, lines) if lines else prefix + f"pytest exited {run.returncode}"


def _read_capped(path: Path) -> str:
    return _clip(path.read_text(), FILE_BUDGET) if path.is_file() else "(missing: create it)"


def _prompt(repo: Path, allowed: set[str], run: TestRun, notes: str,
            interface_dir: str, tests_dir: str) -> list[dict]:
    parts = ["## Interface"]
    for p in sorted((repo / interface_dir).rglob("*.pyi")):
        parts += [f"### {p.relative_to(repo).as_posix()}", _read_capped(p)]
    parts.append("## Current source")
    for rel in sorted(allowed):
        parts += [f"### {rel}", _read_capped(repo / rel)]
    failing = sorted({t.nodeid.split("::")[0] for t in run.outcomes if t.outcome != "passed"})
    failing = [f for f in failing if (repo / f).is_file()] or sorted(
        p.relative_to(repo).as_posix() for p in (repo / tests_dir).rglob("test_*.py"))
    parts.append("## Failing tests")
    for rel in failing:
        parts += [f"### {rel}", _read_capped(repo / rel)]
    output = run.output if len(run.output) <= OUTPUT_BUDGET else "..." + run.output[-OUTPUT_BUDGET:]
    parts += ["## pytest output", output]
    if notes:
        parts += ["## Problems with your last reply", notes]
    return [{"role": "system", "content": SYSTEM.format(allowed=", ".join(sorted(allowed)))},
            {"role": "user", "content": "\n\n".join(parts)}]


def _restore_src(repo: Path, src_dir: str) -> None:
    if _git(repo, "ls-tree", "--name-only", "HEAD", "--", src_dir).strip():
        _git(repo, "restore", "--source=HEAD", "--staged", "--worktree", "--", src_dir)
    _git(repo, "clean", "-fdq", "--", src_dir)


def _commit_src(repo: Path, src_dir: str, message: str) -> None:
    _git(repo, "add", "-A", "--", src_dir)
    if subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet", "--",
                       src_dir]).returncode:
        _git(repo, "commit", "-qm", message, "--", src_dir)


class _Log:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(exist_ok=True)

    def write(self, title: str, body: str = "") -> None:
        with open(self.path, "a") as log:
            log.write(f"==== {time.strftime('%Y-%m-%d %H:%M:%S')} {title}\n{body}\n")


def build(repo: Path | str, model: Model, max_tries: int = 5, tests_dir: str = "tests",
          interface_dir: str = "interface", src_dir: str = "src", timeout: float = 120,
          progress: Callable[[str], None] = print, sandbox: bool = True) -> BuildResult:
    """Build src/ until the locked suite is green. sandbox=False exists only to test the guards."""
    repo = Path(repo)
    if sandbox:
        require_bwrap()
    lock = SuiteLock(repo, tests_dir, interface_dir)
    with run_lock(repo):
        lock.enforce()
        lock.pin()
        if _staged(repo):
            raise StagingError("commit or discard the staged suite files before building")
        if _git(repo, "status", "--porcelain", "--untracked-files=all", "--", src_dir).strip():
            raise DirtySrc(f"commit or remove the changes in {src_dir}/ before building")
        log = _Log(build_log_path(repo))
        log.write("build started", f"max_tries={max_tries}")
        try:
            return _loop(repo, model, lock, log, max_tries, tests_dir, interface_dir, src_dir,
                         timeout, progress, sandbox)
        except BaseException as err:
            log.write("build aborted", repr(err))
            _restore_src(repo, src_dir)
            raise


def _loop(repo: Path, model: Model, lock: SuiteLock, log: _Log, max_tries: int, tests_dir: str,
          interface_dir: str, src_dir: str, timeout: float,
          progress: Callable[[str], None], sandbox: bool) -> BuildResult:
    allowed = allowed_paths(repo, interface_dir, src_dir)
    run = _run_tests(repo, tests_dir, src_dir, timeout, sandbox)
    _guard(repo, lock)
    log.write("initial test run", run.output)
    progress(f"start: {run.passed} passed, {run.failed} failed")
    if run.returncode == 5:
        return BuildResult("stuck", 0, "stuck: no tests collected")
    if run.green:
        return BuildResult("green", 0, f"green after 0 tries: {run.passed} tests pass")
    notes = ""
    for n in range(1, max_tries + 1):
        step = f"try {n}/{max_tries}"
        progress(f"{step}: asking the model")
        messages = _prompt(repo, allowed, run, notes, interface_dir, tests_dir)
        log.write(f"{step} prompt", messages[-1]["content"])
        text = model.chat(messages)
        log.write(f"{step} reply", text)
        files, refused = parse_reply(text, allowed)
        for rel, body in files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(body)
        _guard(repo, lock)
        notes = ""
        if refused:
            notes += (f"These paths were refused and not written: {', '.join(refused)}. "
                      f"Write only: {', '.join(sorted(allowed))}.\n")
        if not files and not refused:
            notes += "Your reply had no FILE blocks, so nothing was written. Use the format.\n"
        progress(f"{step}: wrote {len(files)} file(s), refused {len(refused)}")
        run = _run_tests(repo, tests_dir, src_dir, timeout, sandbox)
        _guard(repo, lock)
        log.write(f"{step} test run", run.output)
        progress(f"{step}: {run.passed} passed, {run.failed} failed")
        if run.green:
            _commit_src(repo, src_dir, f"build: green after {_plural(n)}")
            log.write("green")
            return BuildResult("green", n, f"green after {_plural(n)}: {run.passed} tests pass")
    _restore_src(repo, src_dir)
    log.write("stuck")
    return BuildResult("stuck", max_tries, _stuck_summary(max_tries, run, repo / tests_dir))


def main(argv: list[str] | None = None, client: Model | None = None,
         on_demand: Callable | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a target's src/ with a local model.")
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--max-tries", type=int, default=5)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--timeout", type=float, default=120, help="seconds per test run")
    args = parser.parse_args(argv)
    if client is None:
        real = OllamaClient(host=args.host, model=args.model)
        client, on_demand = real, on_demand or (lambda: ollama_on_demand(is_up=real.is_up))
    on_demand = on_demand or ollama_on_demand
    print(f"log: {build_log_path(args.repo)}", flush=True)
    with on_demand():
        result = build(args.repo, client, max_tries=args.max_tries, timeout=args.timeout,
                       progress=lambda line: print(line, flush=True))
    print(result.summary)
    return 0 if result.status == "green" else 1


if __name__ == "__main__":
    sys.exit(main())
