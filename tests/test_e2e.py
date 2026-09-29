"""The end-to-end driver plays the test writer against the real server subprocess."""
import os
import subprocess
from pathlib import Path

import pytest

from big_brother.e2e import REQUIREMENT, main, make_toy_target, run
from big_brother.ledger import Ledger
from big_brother.suite_lock import SuiteLock


@pytest.fixture
def toy(tmp_path: Path):
    target = make_toy_target(tmp_path / "toy")
    yield target
    for d, _, _ in os.walk(target):  # let tmp cleanup delete read-only dirs
        os.chmod(d, 0o755)


def test_toy_target_is_a_locked_repo_with_one_open_requirement(toy):
    assert (toy / "interface" / "calc.pyi").is_file()
    assert not (toy / "src").exists() and not (toy / "tests").exists()
    assert SuiteLock(toy).is_locked()
    assert [(r.status, r.text) for r in Ledger(toy).all()] == [("open", REQUIREMENT)]
    assert subprocess.run(["git", "-C", str(toy), "status", "--porcelain"], capture_output=True,
                          text=True, check=True).stdout == ""


def test_an_existing_target_is_refused(toy):
    with pytest.raises(FileExistsError):
        make_toy_target(toy)


def test_main_refuses_an_existing_target_without_touching_it(toy, capsys):
    assert main([str(toy)]) == 2
    assert "exists" in capsys.readouterr().err


@pytest.mark.ollama
def test_real_model_end_to_end(toy):
    lines: list[str] = []
    outcome = run(toy, lines.append)
    assert outcome["build"].startswith("green"), outcome
    assert outcome["feedback"].startswith("feedback: ")
    assert Ledger(toy).get(1).status == "done"
    assert any(line.startswith("build:") for line in lines)
