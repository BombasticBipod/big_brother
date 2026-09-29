"""End to end on one toy requirement: play the test writer against the real server.

`make_toy_target` creates a new git repository holding only an interface for
`calc.add`, locks its suite and adds one requirement to its ledger. `run`
starts `python -m big_brother.server TARGET` as a stdio subprocess, exactly as
Claude Code would, and walks the writer's cycle through its tools:
next_requirement, get_interface, propose_test, commit_tests, build, feedback,
next_requirement. The server uses the real local model and starts Ollama on
the first build; closing the session stops it.

Each step is printed as it happens and appended to
`TARGET/.git/big_brother/e2e.log`. Build progress is in
`TARGET/.git/big_brother/progress.log` (`tail -f` it).

Usage: python -m big_brother.e2e TARGET   (TARGET must not exist yet)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

from big_brother.ledger import Ledger
from big_brother.suite_lock import SuiteLock

REQUIREMENT = "add(a, b) returns the sum of two integers"
CALC_PYI = 'def add(a: int, b: int) -> int:\n    """Return a plus b."""\n'
TEST_ADD = '''from calc import add


def test_add_positive():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-4, 1) == -3


def test_add_zero():
    assert add(0, 0) == 0
'''


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_toy_target(target: Path | str) -> Path:
    target = Path(target)
    target.mkdir(parents=True)   # raises FileExistsError: never reuse a directory
    (target / "interface").mkdir()
    (target / "interface" / "calc.pyi").write_text(CALC_PYI)
    _git(target, "init", "-q", "-b", "main")
    _git(target, "config", "user.email", "big-brother@example.com")
    _git(target, "config", "user.name", "big_brother e2e")
    _git(target, "add", "-A")
    _git(target, "commit", "-qm", "toy target: interface for calc.add")
    SuiteLock(target).lock()
    Ledger(target).add(REQUIREMENT)
    return target


def run(target: Path | str, say: Callable[[str], None] = print) -> dict[str, str]:
    """Drive the writer's cycle over stdio; return each tool's result text by step name."""
    target = Path(target).resolve()
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "big_brother.server", str(target)])
    outcome: dict[str, str] = {}

    async def step(client: Client, name: str, tool: str, **args) -> str:
        started = time.monotonic()
        say(f"{name}: calling {tool}")
        result = await client.call_tool(tool, args)
        text = "".join(getattr(c, "text", "") for c in result.content)
        flag = " (error)" if result.is_error else ""
        say(f"{name}: {text}{flag} [{time.monotonic() - started:.1f}s]")
        outcome[name] = text
        return text

    async def cycle() -> None:
        async with Client(params) as client:
            await step(client, "requirement", "next_requirement")
            await step(client, "interface", "get_interface", module="calc")
            await step(client, "propose", "propose_test", path="tests/test_add.py",
                       content=TEST_ADD)
            await step(client, "commit", "commit_tests", message="tests for add",
                       requirement_id=1)
            await step(client, "build", "build", max_tries=5)
            await step(client, "feedback", "feedback")
            await step(client, "next", "next_requirement")
        say("session closed")

    anyio.run(cycle)
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run big_brother end to end on a toy target.")
    parser.add_argument("target", help="a directory that does not exist yet")
    args = parser.parse_args(argv)
    try:
        target = make_toy_target(args.target)
    except FileExistsError:
        print(f"{args.target} exists; give a new directory", file=sys.stderr)
        return 2
    log = target / ".git" / "big_brother" / "e2e.log"

    def say(line: str) -> None:
        stamped = f"{time.strftime('%H:%M:%S')} {line}"
        print(stamped, flush=True)
        with open(log, "a") as handle:
            handle.write(stamped + "\n")

    say(f"target: {target}")
    say(f"log: {log}")
    say(f"build progress: {target / '.git' / 'big_brother' / 'progress.log'}")
    outcome = run(target, say)
    return 0 if outcome.get("build", "").startswith("green") else 1


if __name__ == "__main__":
    sys.exit(main())
