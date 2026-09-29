"""The MCP server Claude Code, the test writer, drives one target project through.

Every result is a few lines: a verdict, counts, test ids, names the interface
declares. Nothing returned ever carries implementation text, because the test
writer must not see `src/`. Refusals come back as tool errors (`is_error`),
clipped to the same budget, so the writer can read them and try again.

Tools:
- `next_requirement()`: the next ledger item, stuck or tested work first.
- `get_interface(module)`: a module's `.pyi`, staged version first; with an
  empty module name, the list of modules.
- `propose_test(path, content)` / `propose_interface(path, content)`: stage a
  file under `tests/` or `interface/` and return the red check of everything
  staged.
- `discard_staged()`: drop every staged file.
- `commit_tests(message, requirement_id)`: commit the staged files through the
  suite lock and mark the requirement tested.
- `build(max_tries)`: run the builder loop with the local model. Green marks
  every tested requirement done; stuck counts against each of them.
- `feedback()`: coverage and surviving mutants, by interface name.

Ollama starts on the first `build` of a session and stays up for the rest of
it; the session's end stops it, and only if the server started it. A session
that never builds never starts it. `main()` turns SIGTERM into a normal exit so
that stop still runs when Claude Code closes the server.

A stdio MCP server must never write to stdout, which carries the protocol.
Build and feedback progress goes to `.git/big_brother/progress.log`, which the
user can follow with `tail -f`; it holds counts only.

Usage: python -m big_brother.server TARGET
"""
from __future__ import annotations

import argparse
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, ExitStack, asynccontextmanager
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from big_brother.builder import DirtySrc, Model, build as run_build
from big_brother.feedback import feedback as run_feedback
from big_brother.ledger import Ledger, LedgerError
from big_brother.ollama import OllamaClient, OllamaError, ollama_on_demand
from big_brother.red_check import SUMMARY_BUDGET, _clip
from big_brother.runlock import BuildBusy
from big_brother.staging import Staging, StagingError
from big_brother.suite_lock import NotLocked, TestsTampered

INTERFACE_BUDGET = 8000    # characters of one .pyi returned by get_interface
ERROR_BUDGET = 400         # the SDK prefixes "Error executing tool <name>: "
MODULE = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*")
REFUSALS = (StagingError, LedgerError, BuildBusy, TestsTampered, DirtySrc, NotLocked,
            OllamaError, ValueError)

INSTRUCTIONS = """You write pytest tests for a project whose implementation you never see.
Cycle: next_requirement, get_interface, propose_interface if the interface needs a change,
propose_test until it reports red, commit_tests, build, feedback, then the next test."""


def progress_path(repo: Path | str) -> Path:
    git_dir = subprocess.run(["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
                             check=True, capture_output=True, text=True).stdout.strip()
    return Path(git_dir) / "big_brother" / "progress.log"


def _progress_writer(repo: Path, job: str) -> Callable[[str], None]:
    path = progress_path(repo)
    path.parent.mkdir(exist_ok=True)

    def write(line: str) -> None:
        with open(path, "a") as log:
            log.write(f"{time.strftime('%H:%M:%S')} {job}: {line}\n")
    return write


def _refuse(err: Exception) -> ToolError:
    return ToolError(_clip(str(err), ERROR_BUDGET))


def make_server(repo: Path | str, model: Model | None = None,
                on_demand: Callable[[], AbstractContextManager] | None = None,
                tests_dir: str = "tests", interface_dir: str = "interface") -> MCPServer:
    repo = Path(repo).resolve()
    if model is None:
        client = OllamaClient()
        model, on_demand = client, on_demand or (lambda: ollama_on_demand(is_up=client.is_up))
    start_model = on_demand or ollama_on_demand
    staging = Staging(repo, tests_dir, interface_dir)
    model_up = ExitStack()
    model_lock = threading.Lock()
    started = False

    def ensure_model() -> None:
        nonlocal started
        with model_lock:
            if not started:
                model_up.enter_context(start_model())
                started = True

    @asynccontextmanager
    async def session(_server: MCPServer) -> AsyncIterator[dict]:
        nonlocal started
        try:
            yield {}
        finally:
            with model_lock:
                model_up.close()
                started = False

    server = MCPServer("big_brother", instructions=INSTRUCTIONS, lifespan=session)

    def ledger() -> Ledger:
        return Ledger(repo)

    def propose(path: str, content: str, folder: str) -> str:
        if not path.startswith(folder + "/"):
            raise ToolError(_clip(f"{path!r}: this tool writes only under {folder}/", ERROR_BUDGET))
        try:
            return staging.propose(path, content).summary
        except REFUSALS as err:
            raise _refuse(err) from None

    @server.tool()
    def next_requirement() -> str:
        """The next requirement to work on: tested (possibly stuck) ones before new ones."""
        req = ledger().next()
        if req is None:
            return "no open requirements"
        stuck = f" (stuck builds: {req.stuck_builds})" if req.stuck_builds else ""
        return _clip(f"{req.id} [{req.status}] {req.text}{stuck}", SUMMARY_BUDGET)

    @server.tool()
    def get_interface(module: str) -> str:
        """A module's interface (.pyi), including staged changes. Empty name: list modules."""
        roots = [staging.root / interface_dir, repo / interface_dir]
        if not module:
            names = sorted({".".join(p.relative_to(r).with_suffix("").parts)
                            .removesuffix(".__init__")
                            for r in roots if r.is_dir() for p in r.rglob("*.pyi")})
            return _clip("modules: " + (", ".join(names) or "none"), SUMMARY_BUDGET)
        if not MODULE.fullmatch(module):
            raise ToolError("give a module name such as calc or pkg.shapes")
        rel = Path(*module.split("."))
        for root in roots:
            for candidate in (root / rel.with_suffix(".pyi"), root / rel / "__init__.pyi"):
                if candidate.is_file() and not candidate.is_symlink():
                    return _clip(candidate.read_text(), INTERFACE_BUDGET)
        raise ToolError(f"no interface for module {module}")

    @server.tool()
    def propose_test(path: str, content: str) -> str:
        """Stage a test file under tests/ and red-check everything staged against stubs."""
        return propose(path, content, tests_dir)

    @server.tool()
    def propose_interface(path: str, content: str) -> str:
        """Stage an interface file (.pyi) under interface/ and check everything staged."""
        return propose(path, content, interface_dir)

    @server.tool()
    def discard_staged() -> str:
        """Drop every staged file."""
        n = len(staging.paths())
        try:
            staging.discard()
        except REFUSALS as err:
            raise _refuse(err) from None
        return f"discarded {n} staged file{'s' if n != 1 else ''}"

    @server.tool()
    def commit_tests(message: str, requirement_id: int) -> str:
        """Commit the staged files as the locked suite and mark the requirement tested."""
        try:
            if ledger().get(requirement_id).status == "done":
                raise LedgerError(f"requirement {requirement_id} is done")
            commit = staging.commit(message)
            ledger().tests_committed(requirement_id, commit)
        except REFUSALS as err:
            raise _refuse(err) from None
        return f"committed {commit[:12]} for requirement {requirement_id}"

    @server.tool()
    def build(max_tries: int = 5) -> str:
        """Let the local model implement src/ until the locked suite is green or tries run out."""
        try:
            ensure_model()
            result = run_build(repo, model, max_tries=max_tries, tests_dir=tests_dir,
                               interface_dir=interface_dir,
                               progress=_progress_writer(repo, "build"))
        except REFUSALS as err:
            raise _refuse(err) from None
        book = ledger()
        tested = [r for r in book.all() if r.status == "tested"]
        if result.status == "green":
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
            for req in tested:
                book.done(req.id, head)
        else:
            for req in tested:
                book.build_stuck(req.id)
        return result.summary

    @server.tool()
    def feedback() -> str:
        """Coverage gaps and surviving mutants, named by the interface."""
        try:
            return run_feedback(repo, tests_dir=tests_dir, interface_dir=interface_dir,
                                progress=_progress_writer(repo, "feedback")).summary
        except REFUSALS as err:
            raise _refuse(err) from None

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve one target project to Claude Code.")
    parser.add_argument("target")
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    make_server(args.target).run("stdio")


if __name__ == "__main__":
    main()
