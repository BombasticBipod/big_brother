"""Tell the test writer where the suite is weak, without showing any implementation.

`feedback` copies the committed `src/` and `tests/` from HEAD into a temporary
directory, runs the suite under branch coverage, then runs mutmut. Results are
reported only by names the interface declares (`calc.sign`,
`calc.Counter.bump`). Everything else in a module (helpers, nested functions,
undeclared classes) is folded into one "<module> (other code)" bucket, and
modules with no `.pyi` fold into one "other modules" bucket, so no private
name reaches the test writer. Line numbers are left out too.

The summary is capped at SUMMARY_BUDGET characters and never carries tool
output, which can quote `src/`. Full output goes to
`.git/big_brother/feedback.log`, which the test writer must not read.

mutmut 3.8 names each mutant `<module>.x_<function>__mutmut_<n>`, or
`<module>.xǁ<Class>ǁ<method>__mutmut_<n>` for a method, and
`mutmut results --all true` prints one `<name>: <status>` line per mutant.
That format is internal to mutmut, so the version is pinned in pyproject.toml.

Usage: python -m big_brother.feedback [REPO]
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from big_brother.builder import DirtySrc
from big_brother.red_check import SUMMARY_BUDGET, fit
from big_brother.runlock import run_lock
from big_brother.suite_lock import SuiteLock

OTHER_MODULES = "other modules"
CAUGHT = {"killed", "timeout"}
SURVIVED = {"survived", "no tests"}
PROGRESS = re.compile(r"(\d+)/(\d+)")
MUTMUT_CONFIG = """[tool.pytest.ini_options]

[tool.mutmut]
source_paths = ["src/"]
pytest_add_cli_args = ["-p", "no:cacheprovider"]
pytest_add_cli_args_test_selection = ["tests/"]
"""


@dataclass(frozen=True)
class FeedbackResult:
    status: str                                                  # "ok", "not_green" or "failed"
    summary: str
    total_coverage: float = 0.0
    coverage: dict[str, float] = field(default_factory=dict)     # name -> percent covered
    survivors: dict[str, int] = field(default_factory=dict)      # name -> surviving mutants
    killed_in: dict[str, int] = field(default_factory=dict)      # name -> killed mutants
    killed: int = 0
    total: int = 0


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def feedback_log_path(repo: Path | str) -> Path:
    git_dir = Path(_git(Path(repo), "rev-parse", "--absolute-git-dir").strip())
    return git_dir / "big_brother" / "feedback.log"


def _module(rel: Path) -> str:
    parts = rel.with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def interface_names(interface_dir: Path | str) -> dict[str, set[str]]:
    """Declared functions and methods per module: {"calc": {"add", "Counter.bump"}}."""
    root = Path(interface_dir)
    names: dict[str, set[str]] = {}
    for pyi in sorted(root.rglob("*.pyi")):
        declared: set[str] = set()
        for node in ast.parse(pyi.read_text()).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                declared.add(node.name)
            elif isinstance(node, ast.ClassDef):
                declared |= {f"{node.name}.{item.name}" for item in node.body
                             if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
        names[_module(pyi.relative_to(root))] = declared
    return names


def _other(module: str, declared: dict[str, set[str]]) -> str:
    return f"{module} (other code)" if module in declared else OTHER_MODULES


def mutant_name(key: str, declared: dict[str, set[str]]) -> str:
    """Map a mutmut mutant key to a declared name, or to its module's other-code bucket."""
    base = key.rsplit("__mutmut_", 1)[0]
    module, _, mangled = base.rpartition(".")
    if mangled.startswith("xǁ"):
        qualname = ".".join(mangled[2:].split("ǁ"))
    elif mangled.startswith("x_"):
        qualname = mangled[2:]
    else:
        return _other(module, declared)
    if qualname in declared.get(module, ()):
        return f"{module}.{qualname}"
    return _other(module, declared)


def run_killable(cmd: list[str], cwd: Path, env: dict, timeout: float,
                 on_output: Callable[[str], None] | None = None) -> tuple[int | None, str, bool]:
    """Run cmd in its own process group, streaming output; on timeout kill the whole group."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    timed_out = threading.Event()

    def kill() -> None:
        timed_out.set()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    timer = threading.Timer(timeout, kill)
    timer.start()
    chunks: list[str] = []
    try:
        assert proc.stdout is not None
        while data := os.read(proc.stdout.fileno(), 65536):
            text = data.decode(errors="replace")
            chunks.append(text)
            if on_output:
                on_output(text)
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            kill()
            proc.wait()
        proc.stdout.close()
    return (None if timed_out.is_set() else proc.returncode), "".join(chunks), timed_out.is_set()


def _percent(summary: dict) -> float:
    total = summary["num_statements"] + summary.get("num_branches", 0)
    done = summary["covered_lines"] + summary.get("covered_branches", 0)
    return round(100.0 * done / total, 1) if total else 100.0


def _coverage(report: dict, declared: dict[str, set[str]], src_dir: str) -> dict[str, float]:
    result: dict[str, float] = {}
    other: dict[str, Counter] = {}  # bucket name -> summed counts
    for path, data in report["files"].items():
        module = _module(Path(path).relative_to(src_dir))
        for qualname, fn in data.get("functions", {}).items():
            if not qualname:
                continue  # module-level code
            if qualname in declared.get(module, ()):
                result[f"{module}.{qualname}"] = _percent(fn["summary"])
            else:
                other.setdefault(_other(module, declared), Counter()).update(
                    {k: fn["summary"].get(k, 0) for k in
                     ("num_statements", "num_branches", "covered_lines", "covered_branches")})
    for bucket, sums in other.items():
        result[bucket] = _percent(sums)
    return result


def _summary(total_coverage: float, coverage: dict[str, float], survivors: dict[str, int],
             killed: int, total: int) -> str:
    half = (SUMMARY_BUDGET - len("feedback: ; ")) // 2
    gaps = [f"{name} {pct:.0f}%" for name, pct in sorted(coverage.items(), key=lambda kv: kv[1])
            if pct < 100]
    cov = fit(f"branch coverage {total_coverage:.0f}%" + (", gaps: " if gaps else ""), gaps,
              half, sep=", ")
    if survivors:
        ranked = sorted(survivors.items(), key=lambda kv: (-kv[1], kv[0]))
        mut = fit(f"{sum(survivors.values())} of {total} mutants survived in: ",
                  [f"{name} {n}" for name, n in ranked], half, sep=", ")
    else:
        mut = f"no surviving mutants ({killed} of {total} killed)"
    return f"feedback: {cov}; {mut}"


class _Log:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(exist_ok=True)

    def write(self, title: str, body: str = "") -> None:
        with open(self.path, "a") as log:
            log.write(f"==== {time.strftime('%Y-%m-%d %H:%M:%S')} {title}\n{body}\n")


class _MutationProgress:
    """Turn mutmut's spinner stream into occasional count-only progress lines."""

    def __init__(self, progress: Callable[[str], None], every: float = 10):
        self.progress, self.every = progress, every
        self.last_done, self.last_time, self.tail = -1, 0.0, ""

    def __call__(self, text: str) -> None:
        self.tail = (self.tail + text)[-200:]
        matches = PROGRESS.findall(self.tail)
        if not matches:
            return
        done, total = map(int, matches[-1])
        step = max(1, total // 10)
        now = time.monotonic()
        if done != self.last_done and (done == total or done - self.last_done >= step
                                       or now - self.last_time >= self.every):
            self.progress(f"mutation: {done}/{total}")
            self.last_done, self.last_time = done, now


def feedback(repo: Path | str, tests_dir: str = "tests", interface_dir: str = "interface",
             src_dir: str = "src", coverage_timeout: float = 300, mutation_timeout: float = 1800,
             progress: Callable[[str], None] = print) -> FeedbackResult:
    repo = Path(repo)
    with run_lock(repo):
        SuiteLock(repo, tests_dir).enforce()
        if _git(repo, "status", "--porcelain", "--untracked-files=all", "--", src_dir).strip():
            raise DirtySrc(f"commit or remove the changes in {src_dir}/ before feedback")
        if not _git(repo, "ls-tree", "--name-only", "HEAD", "--", src_dir).strip():
            return FeedbackResult("not_green", "feedback: suite is not green; build first")
        declared = interface_names(repo / interface_dir)
        log = _Log(feedback_log_path(repo))
        archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", "HEAD", "--",
                                  src_dir, tests_dir], check=True, capture_output=True).stdout
        with tempfile.TemporaryDirectory(prefix="big_brother_feedback_") as tmp_name:
            tmp = Path(tmp_name)
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(tmp, filter="data")
            (tmp / "pyproject.toml").write_text(
                MUTMUT_CONFIG.replace('"src/"', f'"{src_dir}/"').replace('"tests/"', f'"{tests_dir}/"'))
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "HOME": os.environ.get("HOME", tmp_name),
                   "PYTHONPATH": src_dir, "PYTHONDONTWRITEBYTECODE": "1"}
            return _measure(tmp, env, declared, log, tests_dir, src_dir, coverage_timeout,
                            mutation_timeout, progress)


def _measure(tmp: Path, env: dict, declared: dict[str, set[str]], log: _Log, tests_dir: str,
             src_dir: str, coverage_timeout: float, mutation_timeout: float,
             progress: Callable[[str], None]) -> FeedbackResult:
    py = sys.executable
    progress("coverage: running the suite")
    code, output, timed_out = run_killable([py, "-m", "coverage", "run", "--branch",
                                            f"--source={src_dir}", "-m", "pytest", "-q",
                                            "-p", "no:cacheprovider", tests_dir],
                                           tmp, env, coverage_timeout)
    log.write("coverage run", output)
    if timed_out:
        why = f"coverage run timed out after {coverage_timeout:.0f}s"
        progress(why)
        return FeedbackResult("failed", f"feedback: {why}")
    if code != 0:
        progress("coverage: suite is not green")
        return FeedbackResult("not_green", "feedback: suite is not green; build first")
    code, output, _ = run_killable([py, "-m", "coverage", "json", "-q", "-o", "coverage.json"],
                                   tmp, env, 60)
    if code != 0 or not (tmp / "coverage.json").exists():
        log.write("coverage json", output)
        why = f"coverage report failed (exit {code})"
        progress(why)
        return FeedbackResult("failed", f"feedback: {why}")
    report = json.loads((tmp / "coverage.json").read_text())
    coverage = _coverage(report, declared, src_dir)
    total_coverage = report["totals"]["percent_covered"]
    progress(f"coverage: {total_coverage:.0f}%")

    progress("mutation: generating mutants")
    code, output, timed_out = run_killable([py, "-m", "mutmut", "run"], tmp, env,
                                           mutation_timeout, _MutationProgress(progress))
    log.write("mutmut run", output[-20000:])
    if code != 0:
        why = f"timed out after {mutation_timeout:.0f}s" if timed_out else f"failed (exit {code})"
        progress(f"mutation: {why}")
        return FeedbackResult("failed", f"feedback: mutation run {why}", total_coverage, coverage)
    code, output, _ = run_killable([py, "-m", "mutmut", "results", "--all", "true"], tmp, env, 120)
    log.write("mutmut results", output)
    survivors: Counter = Counter()
    killed_in: Counter = Counter()
    total = 0
    for line in output.splitlines():
        key, sep, status = line.strip().rpartition(": ")
        if not sep or "__mutmut_" not in key:
            continue
        total += 1
        name = mutant_name(key, declared)
        if status in SURVIVED:
            survivors[name] += 1
        elif status in CAUGHT:
            killed_in[name] += 1
    killed = sum(killed_in.values())
    summary = _summary(total_coverage, coverage, dict(survivors), killed, total)
    log.write("summary", summary)
    return FeedbackResult("ok", summary, total_coverage, coverage, dict(survivors),
                          dict(killed_in), killed, total)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Coverage and mutation gaps, by interface name.")
    parser.add_argument("repo", nargs="?", default=".")
    args = parser.parse_args(argv)
    print(f"log: {feedback_log_path(args.repo)}", flush=True)
    result = feedback(args.repo, progress=lambda line: print(line, flush=True))
    print(result.summary)
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
