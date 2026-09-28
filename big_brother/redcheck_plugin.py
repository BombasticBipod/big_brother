"""pytest plugin used by red_check: record each test phase's outcome as JSON lines.

Only the exception type, its one-line message and the file it was raised in
are recorded, never a traceback. Callers decide which messages are safe to
show: one raised in a target's src/ may quote implementation text.
"""
from __future__ import annotations

import json
import os

import pytest


def _record(entry: dict) -> None:
    with open(os.environ["BIG_BROTHER_REDCHECK_OUT"], "a") as out:
        out.write(json.dumps(entry) + "\n")


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" or report.outcome != "passed":
        exc = call.excinfo
        _record({
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "exc_type": exc.typename if exc else None,
            "message": _first_line(exc.exconly()) if exc else "",
            "raised_in": str(exc.traceback[-1].path) if exc and exc.traceback else "",
        })


def pytest_collectreport(report):
    if report.failed:
        lines = [l[1:].strip() for l in str(report.longrepr).splitlines() if l.startswith("E ")]
        _record({
            "nodeid": report.nodeid,
            "when": "collect",
            "outcome": "error",
            "exc_type": "CollectionError",
            "message": lines[-1] if lines else _first_line(str(report.longrepr)),
            "raised_in": "",
        })
