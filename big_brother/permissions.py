"""Claude Code settings that keep the test writer away from a target's implementation.

`write_target_settings(target)` merges into the target's `.claude/settings.json`
(project settings, so `/path` rules anchor at the target's root) and states the
rule once in its `CLAUDE.md`:

- `permissions.deny` blocks the built-in file tools from reading `src/` and
  `.git/big_brother/` (the build, feedback and progress logs, which quote
  implementation text), and from editing `src/`, `tests/` and `interface/`
  directly: suite changes go through the MCP tools, which stage and check them.
  Claude Code also applies Read deny rules to Bash file commands it recognises
  (`cat`, `head`, `tail`, `sed`, redirections), but not to a command that reads
  files without naming them (`grep -r x .`) or to a script that opens files
  itself.
- The sandbox closes that gap at the OS level: sandboxed Bash commands and their
  children cannot read `./src` or `./.git/big_brother`, and with
  `allowUnsandboxedCommands` off no command may run outside the sandbox. On
  Linux the sandbox needs `bubblewrap` and `socat`.

These are settings for Claude Code to enforce. The tests here check the files
this module writes, not that Claude Code enforces them.

Usage: python -m big_brother.permissions TARGET [--src-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CLAUDE_MD_MARKER = "<!-- big_brother: test writer rules -->"
CLAUDE_MD_RULES = f"""
{CLAUDE_MD_MARKER}
## big_brother

You are the test writer. You never read src/ and never read .git/big_brother/.
Work only through the big_brother MCP tools: next_requirement, get_interface,
propose_interface, propose_test, commit_tests, build, feedback. Settings deny
direct reads of src/ and direct edits of src/, tests/ and interface/.
"""


def deny_rules(src_dir: str = "src") -> list[str]:
    return [f"Read(/{src_dir}/**)", "Read(/.git/big_brother/**)",
            f"Edit(/{src_dir}/**)", "Edit(/tests/**)", "Edit(/interface/**)"]


DENY = deny_rules()


def _union(existing: list, added: list) -> list:
    return existing + [item for item in added if item not in existing]


def write_target_settings(target: Path | str, src_dir: str = "src") -> Path:
    target = Path(target)
    path = target / ".claude" / "settings.json"
    path.parent.mkdir(exist_ok=True)
    settings = json.loads(path.read_text()) if path.exists() else {}
    permissions = settings.setdefault("permissions", {})
    permissions["deny"] = _union(permissions.get("deny", []), deny_rules(src_dir))
    sandbox = settings.setdefault("sandbox", {})
    sandbox["enabled"] = True
    sandbox["allowUnsandboxedCommands"] = False
    filesystem = sandbox.setdefault("filesystem", {})
    filesystem["denyRead"] = _union(filesystem.get("denyRead", []),
                                    [f"./{src_dir}", "./.git/big_brother"])
    path.write_text(json.dumps(settings, indent=2) + "\n")

    claude_md = target / "CLAUDE.md"
    text = claude_md.read_text() if claude_md.exists() else ""
    if CLAUDE_MD_MARKER not in text:
        claude_md.write_text(text + CLAUDE_MD_RULES)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deny the test writer reads of a target's src/.")
    parser.add_argument("target")
    parser.add_argument("--src-dir", default="src")
    args = parser.parse_args(argv)
    path = write_target_settings(args.target, args.src_dir)
    print(f"wrote {path} and the rules section of {Path(args.target) / 'CLAUDE.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
