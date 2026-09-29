"""Target settings keep Claude Code, the test writer, away from src/ and big_brother's logs."""
import json
import subprocess
from pathlib import Path

import pytest

from big_brother.permissions import CLAUDE_MD_MARKER, DENY, main, write_target_settings


@pytest.fixture
def target(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    return tmp_path


def settings(target: Path) -> dict:
    return json.loads((target / ".claude" / "settings.json").read_text())


def test_reads_of_src_and_big_brother_state_are_denied(target):
    write_target_settings(target)
    deny = settings(target)["permissions"]["deny"]
    assert "Read(/src/**)" in deny
    assert "Read(/.git/big_brother/**)" in deny


def test_direct_edits_of_the_suite_and_src_are_denied(target):
    write_target_settings(target)
    deny = settings(target)["permissions"]["deny"]
    assert {"Edit(/src/**)", "Edit(/tests/**)", "Edit(/interface/**)"} <= set(deny)


def test_sandboxed_commands_cannot_read_src_or_the_logs(target):
    write_target_settings(target)
    sandbox = settings(target)["sandbox"]
    assert sandbox["enabled"] is True
    assert sandbox["allowUnsandboxedCommands"] is False
    assert {"./src", "./.git/big_brother"} <= set(sandbox["filesystem"]["denyRead"])


def test_existing_settings_are_kept_and_rules_are_not_duplicated(target):
    (target / ".claude").mkdir()
    (target / ".claude" / "settings.json").write_text(json.dumps({
        "model": "x", "permissions": {"allow": ["Bash(ls *)"], "deny": ["Read(/src/**)"]}}))
    write_target_settings(target)
    write_target_settings(target)
    got = settings(target)
    assert got["model"] == "x" and got["permissions"]["allow"] == ["Bash(ls *)"]
    assert sorted(got["permissions"]["deny"]) == sorted(DENY)


def test_a_custom_src_dir_is_covered(target):
    write_target_settings(target, src_dir="lib")
    deny = settings(target)["permissions"]["deny"]
    assert "Read(/lib/**)" in deny and "Read(/src/**)" not in deny
    assert "./lib" in settings(target)["sandbox"]["filesystem"]["denyRead"]


def test_claude_md_states_the_rule_once(target):
    (target / "CLAUDE.md").write_text("# Toy\n\nExisting notes.\n")
    write_target_settings(target)
    write_target_settings(target)
    text = (target / "CLAUDE.md").read_text()
    assert text.startswith("# Toy\n\nExisting notes.\n")
    assert text.count(CLAUDE_MD_MARKER) == 1
    assert "never read src/" in text


def test_cli(target, capsys):
    assert main([str(target)]) == 0
    assert ".claude/settings.json" in capsys.readouterr().out
