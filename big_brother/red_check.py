"""Confirm that new tests fail for the right reason before they are committed.

`red_check` builds a throwaway directory holding stubs generated from the
target's `interface/` and a copy of its `tests/`, then runs the named test
files there with a clean environment. The target's real `src/`, root
conftest and pytest configuration are never on the path, so a test cannot
pass by reaching real code, and no implementation text reaches the result.
The run happens inside the bubblewrap sandbox (big_brother.sandbox), so a
test cannot open `src/` by absolute path either.

Each test gets a verdict:
- red: it failed with NotImplementedError or AssertionError, in setup or call.
- passes: it passed against stubs, so it does not ask for any behavior.
- broken: anything else (collection error, wrong exception, skip, timeout).

The overall status is `broken` if any test is broken or none ran, else
`passes_on_stubs` if any test passed, else `red`. `summary` is a short text
for the test writer, capped at SUMMARY_BUDGET characters.

`overlay`, when given, is a directory of staged files laid over the target's:
its `interface/` and `tests/` files are added to, or replace, the committed
ones in the throwaway copy. The target itself is never written.

A symlink anywhere in the interface or tests (committed or staged) makes the
check broken before anything is copied, because copying follows symlinks and
one pointing into `src/` would carry real source into the check.

Red against stubs is expected for any test that calls the interface, even when
the real implementation already satisfies it. Only a build shows whether a
new test asks for new behavior.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from big_brother.sandbox import require_bwrap, sandboxed
from big_brother.stubs import InterfaceError, make_stubs

RED_EXCEPTIONS = {"NotImplementedError", "AssertionError"}
SUMMARY_BUDGET = 500
MESSAGE_BUDGET = 120


@dataclass(frozen=True)
class TestOutcome:
    __test__ = False  # keep pytest from collecting this as a test class

    nodeid: str
    when: str
    outcome: str
    exc_type: str | None
    message: str
    verdict: str
    raised_in: str = ""   # file the exception was raised in, "" if none or unknown


@dataclass(frozen=True)
class RedResult:
    status: str
    tests: list[TestOutcome]
    summary: str


def _check_paths(tests_dir: str, paths: list[str]) -> None:
    for p in paths:
        parts = PurePosixPath(p).parts
        if not parts or parts[0] != tests_dir or ".." in parts or PurePosixPath(p).is_absolute():
            raise ValueError(f"{p} is not inside {tests_dir}/")


def _symlink(roots: list[Path]) -> Path | None:
    """The first symlink at or under any root, or None."""
    for root in roots:
        if root.is_symlink():
            return root
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                if Path(dirpath, name).is_symlink():
                    return Path(dirpath, name)
    return None


def _layer(sources: list[Path], dest: Path) -> None:
    """Copy each existing source tree into dest in order, later files replacing earlier ones."""
    dest.mkdir(parents=True, exist_ok=True)
    for src in sources:
        if src.is_dir():
            shutil.copytree(src, dest, copy_function=shutil.copyfile, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
        for d, _, _ in os.walk(dest):  # a locked suite copies read-only dirs
            os.chmod(d, 0o755)


def _verdict(entry: dict) -> str:
    if entry["outcome"] == "passed":
        return "passes"
    if (entry["outcome"] == "failed" and entry["when"] in ("setup", "call")
            and entry["exc_type"] in RED_EXCEPTIONS):
        return "red"
    return "broken"


def _outcomes(records: list[dict]) -> list[TestOutcome]:
    """One outcome per test: its first non-passing phase, else its call phase."""
    chosen: dict[str, dict] = {}
    for entry in records:
        current = chosen.get(entry["nodeid"])
        if current is None or (current["outcome"] == "passed" and entry["outcome"] != "passed"):
            chosen[entry["nodeid"]] = entry
    return [TestOutcome(**e, verdict=_verdict(e)) for e in chosen.values()]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 3] + "..."


def fit(prefix: str, lines: list[str], budget: int = SUMMARY_BUDGET, sep: str = "; ") -> str:
    """Join lines after prefix with sep, ending with "(+N more)" where the budget runs out."""
    out = prefix
    for i, line in enumerate(lines):
        more = f" (+{len(lines) - i} more)"
        piece = (sep if i else "") + line
        if len(out) + len(piece) + len(more) > budget:
            return out + more
        out += piece
    return out


def _summarize(status: str, tests: list[TestOutcome], note: str = "") -> str:
    if status == "red":
        n = len(tests)
        return f"red: {n} test{'s' if n != 1 else ''} fail{'s' if n == 1 else ''} correctly"
    if note:
        return _clip(f"broken: {note}", SUMMARY_BUDGET)
    wanted = "broken" if status == "broken" else "passes"
    lines = []
    for t in tests:
        if t.verdict != wanted:
            continue
        if wanted == "passes":
            lines.append(f"{t.nodeid} passed against stubs")
        else:
            lines.append(_clip(f"{t.nodeid} [{t.when}] {t.exc_type}: {t.message}", MESSAGE_BUDGET))
    return fit(f"{status}: ", lines)


def red_check(repo: Path | str, test_paths: list[str], interface_dir: str = "interface",
              tests_dir: str = "tests", timeout: float = 120,
              overlay: Path | str | None = None) -> RedResult:
    repo = Path(repo)
    require_bwrap()
    _check_paths(tests_dir, test_paths)
    layers = [repo] + ([Path(overlay)] if overlay is not None else [])
    link = _symlink([layer / d for layer in layers for d in (interface_dir, tests_dir)])
    if link is not None:
        return RedResult("broken", [], _summarize("broken", [], f"symlink in suite: {link.name}"))
    with tempfile.TemporaryDirectory(prefix="big_brother_red_") as tmp_name:
        tmp = Path(tmp_name)
        _layer([layer / interface_dir for layer in layers], tmp / interface_dir)
        try:
            make_stubs(tmp / interface_dir, tmp / "stubs")
        except InterfaceError as err:
            return RedResult("broken", [], _summarize("broken", [], f"interface: {err}"))
        shutil.rmtree(tmp / interface_dir)
        _layer([layer / tests_dir for layer in layers], tmp / tests_dir)
        (tmp / "pytest.ini").write_text("[pytest]\n")
        out = tmp / "records.jsonl"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp),
            "PYTHONPATH": str(tmp / "stubs"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "BIG_BROTHER_REDCHECK_OUT": str(out),
        }
        cmd = [sys.executable, "-m", "pytest", "-q", "-c", str(tmp / "pytest.ini"),
               "--rootdir", str(tmp), "-p", "no:cacheprovider",
               "-p", "big_brother.redcheck_plugin", *test_paths]
        cmd = sandboxed(cmd, writable=tmp, cwd=tmp)
        try:
            proc = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return RedResult("broken", [], _summarize("broken", [], f"timed out after {timeout}s"))
        records = [json.loads(l) for l in out.read_text().splitlines()] if out.exists() else []
    tests = _outcomes(records)
    if not tests:
        if proc.returncode == 5:
            note = "no tests collected"
        else:
            output = (proc.stderr or proc.stdout).strip().splitlines()
            note = _clip(output[-1] if output else "pytest produced no results", MESSAGE_BUDGET)
        return RedResult("broken", [], _summarize("broken", [], note))
    verdicts = {t.verdict for t in tests}
    status = "broken" if "broken" in verdicts else "passes_on_stubs" if "passes" in verdicts else "red"
    return RedResult(status, tests, _summarize(status, tests))
