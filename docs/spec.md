# big_brother: what exists today

Snapshot of 2026-09-29, branch `run` (8 commits ahead of `main`). `docs/design.md` holds the full design and the reasons behind it; this file is the short version.

## Description

big_brother splits test-driven development between two roles. A **test writer** (Claude Code by default) reads requirements and interface stubs and writes pytest tests. It never sees implementation code. A **builder** (a local Ollama model by default) writes the implementation until the tests pass. The expensive model spends tokens only on tests and short summaries. The cheap model does the long work of writing code.

big_brother drives a separate *target* project. The target holds `interface/*.pyi` (the contract), `tests/` (the locked suite) and `src/` (builder output). big_brother is the referee between the two roles. It locks the contract, checks proposed tests, runs every piece of untrusted code in a sandbox and passes only short summaries back to the writer.

## Spec

### Contract and enforcement (on `main`)

- **Suite lock.** `tests/` and `interface/` stay read-only at all times. The locked commit is recorded in `.git/big_brother/suite_lock.json`. The only way to change them is `accept()`. After every builder step, big_brother compares them with the locked commit. Any change aborts the run and reverts the files.
- **Red check.** `.pyi` files become stubs that raise `NotImplementedError`. Proposed tests run against the stubs only, never against `src/`. The verdict is `red`, `passes_on_stubs` or `broken`, with a summary of at most 500 characters.
- **Staging.** Proposed tests and interface files wait in `.git/big_brother/staged/`. Staged tests must be red. Committed tests must stay unbroken when the interface changes. `commit` goes through `accept()`.
- **Builder loop.** `build(repo, model, max_tries)` runs pytest, sends the failures to the model and writes only `src/<module>.py` files that the interface declares. A green result commits `src/`. A stuck result, or any error, restores `src/` to HEAD. Summaries carry counts, test ids and exception types. Full text goes to `.git/big_brother/build.log`.
- **Feedback.** Branch coverage and mutmut 3.8 run on the committed code in a temporary copy. The summary names only declared interface members and fits in 500 characters.
- **Ledger.** SQLite at `.git/big_brother/ledger.sqlite`. Each requirement moves from open to tested to done. `next()` returns tested (stuck) work before open work.
- **Sandbox.** Every run of model-written code or proposed tests happens in a bubblewrap sandbox. The sandbox has no network, no home directory, no `.git` and no view of the target. A missing `bwrap` is an error, never a silent fallback.
- **Target settings.** `python -m big_brother.permissions TARGET` writes Claude Code rules into the target. The rules deny reads of `src/` and `.git/big_brother/`, deny edits of the contract and enable the OS-level sandbox.
- **MCP server.** `python -m big_brother.server TARGET` offers these tools over stdio: `next_requirement`, `get_interface`, `propose_test`, `propose_interface`, `discard_staged`, `commit_tests`, `build` and `feedback`. Refusals are tool errors of at most 400 characters. Results are at most 500 characters.
- **End to end.** `python -m big_brother.e2e TARGET` plays the writer against a toy target through the real server.

### Pluggable roles and live view (on `run`, not merged)

- **Role specs** (`roles.py`) take the form `kind[:model]`:
  - builder: `ollama` (default `qwen2.5-coder:3b`) or `anthropic` (default `claude-opus-5-5`)
  - writer: `claude-code`, `ollama` or `anthropic`

  Specs are checked before anything starts.
- **Cloud backends** (`cloud.py`): `AnthropicModel` is a streaming builder. `AnthropicConversation` is a tool-calling writer model that uses the server-side refusal fallback. Credentials come from the environment and are never logged.
- **Writer backends** (`writers.py`):
  - `ClaudeCodeWriter` runs `claude -p` with no built-in tools, with only the big_brother MCP server loaded and with only its tools allowed.
  - `AgentWriter` is a plain tool-calling loop for any other model.

  Both reach the target only through the server's tools.
- **Streaming.** Ollama and cloud builder replies stream token by token. The server takes `--builder SPEC` and `--stream FILE`, and it gains an `add_requirement` tool.
- **Windows** (`watch.py`, `windows.py`): terminal windows `tail -f` each stream file. They close when the run ends, even after `kill -9` of the main process. Supported terminals are konsole, gnome-terminal, kitty and xterm. `Windows(None)` opens none.

### Not built yet

- A top-level run command that picks both roles, opens the windows, starts the server and drives the writer. The pieces exist but nothing connects them yet.
- `docs/design.md` does not yet describe the `run` work.
- Targets run on big_brother's own interpreter, so a target's own dependencies are not installed.

## Operating rules

- Ollama starts once per run and stops at the end. It is never a service.
- Tests: `uv run pytest -q` (308 pass). Real-model tests are opt-in: `uv run pytest -m ollama`.
- Before a push, `uv run python -m big_brother.pii . --history` must report 0 findings.
