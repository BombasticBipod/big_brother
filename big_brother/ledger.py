"""The requirements ledger: what to build next, and how far each requirement got.

The ledger is a SQLite file in the target's git directory,
`.git/big_brother/ledger.sqlite`, next to the suite lock state. It is
bookkeeping, not contract, so it is not committed.

Each requirement moves through three statuses:

- **open**: added, no tests committed yet.
- **tested**: tests for it are committed; `suite_commit` is the latest such
  commit. Committing more tests keeps it here and updates the commit. A stuck
  build keeps it here too and counts in `stuck_builds`.
- **done**: a build went green; `src_commit` is the commit it made. Final.

`next()` returns the oldest tested requirement before the oldest open one, so
a requirement whose build got stuck is finished before new work starts.

Usage: python -m big_brother.ledger [--repo REPO] add TEXT | list
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS requirements (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL CHECK (length(trim(text)) > 0),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'tested', 'done')),
    suite_commit TEXT,
    src_commit TEXT,
    stuck_builds INTEGER NOT NULL DEFAULT 0,
    updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
COLUMNS = "id, text, status, suite_commit, src_commit, stuck_builds"


class LedgerError(Exception):
    """An unknown requirement, or a status change the ledger does not allow."""


@dataclass(frozen=True)
class Requirement:
    id: int
    text: str
    status: str
    suite_commit: str | None
    src_commit: str | None
    stuck_builds: int


def ledger_path(repo: Path | str) -> Path:
    git_dir = subprocess.run(["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
                             check=True, capture_output=True, text=True).stdout.strip()
    return Path(git_dir) / "big_brother" / "ledger.sqlite"


class Ledger:
    def __init__(self, repo: Path | str):
        self.path = ledger_path(repo)
        self.path.parent.mkdir(exist_ok=True)
        with self._db() as db:
            db.execute(SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """One transaction on a connection that is always closed afterwards."""
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _one(self, sql: str, args: tuple = ()) -> Requirement | None:
        with self._db() as db:
            row = db.execute(sql, args).fetchone()
        return Requirement(*row) if row else None

    def add(self, text: str) -> int:
        if not text.strip():
            raise LedgerError("a requirement needs text")
        with self._db() as db:
            cursor = db.execute("INSERT INTO requirements (text) VALUES (?)", (text.strip(),))
        return int(cursor.lastrowid or 0)

    def get(self, req_id: int) -> Requirement:
        req = self._one(f"SELECT {COLUMNS} FROM requirements WHERE id = ?", (req_id,))
        if req is None:
            raise LedgerError(f"no requirement {req_id}")
        return req

    def all(self) -> list[Requirement]:
        with self._db() as db:
            return [Requirement(*row) for row in
                    db.execute(f"SELECT {COLUMNS} FROM requirements ORDER BY id")]

    def next(self) -> Requirement | None:
        return self._one(f"SELECT {COLUMNS} FROM requirements WHERE status != 'done' "
                         "ORDER BY status = 'open', id LIMIT 1")

    def _move(self, req_id: int, allowed: set[str], sets: str, args: tuple) -> None:
        status = self.get(req_id).status
        if status not in allowed:
            raise LedgerError(f"requirement {req_id} is {status}")
        with self._db() as db:
            db.execute(f"UPDATE requirements SET {sets}, updated = CURRENT_TIMESTAMP "
                       "WHERE id = ?", (*args, req_id))

    def tests_committed(self, req_id: int, suite_commit: str) -> None:
        self._move(req_id, {"open", "tested"}, "status = 'tested', suite_commit = ?",
                   (suite_commit,))

    def build_stuck(self, req_id: int) -> None:
        self._move(req_id, {"tested"}, "stuck_builds = stuck_builds + 1", ())

    def done(self, req_id: int, src_commit: str) -> None:
        self._move(req_id, {"tested"}, "status = 'done', src_commit = ?", (src_commit,))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Add and list a target's requirements.")
    parser.add_argument("--repo", default=".")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("add").add_argument("text")
    commands.add_parser("list")
    args = parser.parse_args(argv)
    ledger = Ledger(args.repo)
    if args.command == "add":
        print(f"added {ledger.add(args.text)}")
    else:
        rows = ledger.all()
        for req in rows:
            print(f"{req.id} {req.status} {req.text}")
        if not rows:
            print("no requirements")
    return 0


if __name__ == "__main__":
    sys.exit(main())
