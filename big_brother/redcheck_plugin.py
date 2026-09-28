"""pytest plugin used by red_check: record each test phase's outcome as JSON lines.

Only the exception type and its one-line message are recorded, never a
traceback, so no source code reaches the test writer.
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
        })
