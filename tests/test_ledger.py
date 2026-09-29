"""The requirements ledger: what to build next, and how far each requirement got."""
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from big_brother.ledger import Ledger, LedgerError, ledger_path, main


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    return tmp_path


def test_ledger_lives_in_the_git_directory(repo):
    Ledger(repo).add("add two numbers")
    assert ledger_path(repo) == repo / ".git" / "big_brother" / "ledger.sqlite"
    assert ledger_path(repo).exists()


def test_empty_ledger_has_nothing_next(repo):
    assert Ledger(repo).next() is None


def test_requirements_come_out_in_the_order_they_went_in(repo):
    ledger = Ledger(repo)
    first = ledger.add("add two numbers")
    ledger.add("subtract two numbers")
    item = ledger.next()
    assert (item.id, item.text, item.status) == (first, "add two numbers", "open")


def test_a_requirement_with_committed_tests_comes_before_any_open_one(repo):
    ledger = Ledger(repo)
    ledger.add("first")
    second = ledger.add("second")
    ledger.tests_committed(second, "abc123")
    item = ledger.next()
    assert (item.id, item.status, item.suite_commit) == (second, "tested", "abc123")


def test_a_stuck_build_keeps_its_requirement_next(repo):
    ledger = Ledger(repo)
    first = ledger.add("first")
    ledger.add("second")
    ledger.tests_committed(first, "abc123")
    ledger.build_stuck(first)
    ledger.build_stuck(first)
    item = ledger.next()
    assert (item.id, item.status, item.stuck_builds) == (first, "tested", 2)


def test_done_requirements_are_skipped(repo):
    ledger = Ledger(repo)
    first = ledger.add("first")
    second = ledger.add("second")
    ledger.tests_committed(first, "abc123")
    ledger.done(first, "def456")
    assert ledger.next().id == second
    got = ledger.get(first)
    assert (got.status, got.suite_commit, got.src_commit) == ("done", "abc123", "def456")


def test_more_tests_can_be_committed_for_the_same_requirement(repo):
    ledger = Ledger(repo)
    first = ledger.add("first")
    ledger.tests_committed(first, "abc123")
    ledger.tests_committed(first, "bcd234")
    assert ledger.get(first).suite_commit == "bcd234"


def test_done_needs_committed_tests(repo):
    ledger = Ledger(repo)
    first = ledger.add("first")
    with pytest.raises(LedgerError):
        ledger.done(first, "def456")
    with pytest.raises(LedgerError):
        ledger.build_stuck(first)


def test_done_is_final(repo):
    ledger = Ledger(repo)
    first = ledger.add("first")
    ledger.tests_committed(first, "abc123")
    ledger.done(first, "def456")
    with pytest.raises(LedgerError):
        ledger.tests_committed(first, "bcd234")


def test_unknown_requirement_is_an_error(repo):
    with pytest.raises(LedgerError):
        Ledger(repo).tests_committed(99, "abc123")
    with pytest.raises(LedgerError):
        Ledger(repo).get(99)


def test_empty_requirement_text_is_refused(repo):
    with pytest.raises(LedgerError):
        Ledger(repo).add("   ")


def test_state_survives_a_new_ledger_object(repo):
    first = Ledger(repo).add("first")
    Ledger(repo).tests_committed(first, "abc123")
    assert Ledger(repo).next().status == "tested"


def test_the_database_rejects_an_unknown_status(repo):
    Ledger(repo).add("first")
    with closing(sqlite3.connect(ledger_path(repo))) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE requirements SET status = 'bogus'")


def test_all_lists_every_requirement(repo):
    ledger = Ledger(repo)
    ledger.add("first")
    ledger.add("second")
    assert [r.text for r in ledger.all()] == ["first", "second"]


# command line

def test_cli_adds_and_lists(repo, capsys):
    assert main(["--repo", str(repo), "add", "add two numbers"]) == 0
    assert capsys.readouterr().out.strip() == "added 1"
    assert main(["--repo", str(repo), "list"]) == 0
    assert capsys.readouterr().out.strip() == "1 open add two numbers"


def test_cli_list_on_an_empty_ledger(repo, capsys):
    assert main(["--repo", str(repo), "list"]) == 0
    assert capsys.readouterr().out.strip() == "no requirements"
