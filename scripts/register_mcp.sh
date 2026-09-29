#!/bin/sh
# Register big_brother's MCP server in TARGET's .mcp.json (project scope), so a
# Claude Code session started in TARGET gets the test-writer tools.
# Extra arguments go to the server, e.g. --reference stuck.
# Usage: scripts/register_mcp.sh TARGET [SERVER ARGS...]
set -eu
[ $# -ge 1 ] && [ -d "$1" ] || { echo "usage: $0 TARGET [SERVER ARGS...] (TARGET an existing directory)" >&2; exit 2; }
target=$(realpath "$1")
shift
root=$(realpath "$(dirname "$0")/..")
cd "$target"
claude mcp add --scope project --transport stdio big_brother -- \
    uv --directory "$root" run python -m big_brother.server "$target" "$@"
