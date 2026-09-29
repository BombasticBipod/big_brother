"""scripts/register_mcp.sh registers the server in a target's .mcp.json through `claude mcp add`."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "register_mcp.sh"


def test_register_calls_claude_mcp_add_in_the_target(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "args"
    fake = bin_dir / "claude"
    fake.write_text(f'#!/bin/sh\npwd > {record}\nprintf "%s\\n" "$@" >> {record}\n')
    fake.chmod(0o755)
    target = tmp_path / "target"
    target.mkdir()
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    subprocess.run(["sh", str(SCRIPT), str(target)], check=True, env=env, capture_output=True)
    lines = record.read_text().splitlines()
    assert lines[0] == str(target)
    assert lines[1:] == ["mcp", "add", "--scope", "project", "--transport", "stdio", "big_brother",
                         "--", "uv", "--directory", str(ROOT), "run", "python", "-m",
                         "big_brother.server", str(target)]


def test_register_needs_an_existing_target(tmp_path):
    result = subprocess.run(["sh", str(SCRIPT), str(tmp_path / "missing")], capture_output=True)
    assert result.returncode != 0
