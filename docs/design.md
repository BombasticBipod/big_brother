# big_brother design

Decided on 2026-09-28. Claude Code and the local Ollama server both run on the same laptop.

## Roles

- **Test writer: Claude Code.** Reads requirements and interface stubs, writes pytest tests, confirms each new test fails for the right reason, commits the suite. Never reads implementation code. That is where the token savings come from.
- **Builder: local model through Ollama** (first model: `qwen2.5-coder:3b`). A plain Python loop runs pytest, sends failures to Ollama's local HTTP API, writes the answer into the target's `src/`, and repeats until green or out of tries. Ollama tokens cost local compute only, so the builder can be as verbose as it needs.

## Why the design is simple

With one machine there is no second copy of anything. The filesystem is the shared state and git is the version bookkeeping: a suite version is a commit, and "did anything touch the tests?" is a git diff against that commit. The cache store, envelope format, diff transport, hash-based suite sync, drift detection and folder inboxes from the earlier two-machine design are dropped. They come back only if the work is ever split across machines.

## Enforcement

On one filesystem nothing physically stops either side touching the other's files, so enforcement is deliberate:

- Claude Code must not read the target project's `src/`. The rule goes in the target's `CLAUDE.md` and in Claude Code permission deny rules (`Read(src/**)`), so it is enforced rather than requested.
- The builder must never write `tests/`. The builder only writes inside `src/`, and after every iteration the **test lock** checks `tests/` against the commit recorded at the start of the build. Any change aborts the run and reverts `tests/`.
- Runs take turns, never overlap: `build()` holds a lock so tests cannot change mid-run.

Note: these rules apply to the *target* project that big_brother drives, not to big_brother's own source in `big_brother/`.

## Feedback

Red check, coverage and mutation testing (mutmut) all run locally at no token cost. Claude Code only sees short summaries: gaps and surviving mutants.

## MCP server

Claude Code is the only MCP client. The builder is a backend job the server runs. Tools:

- `next_requirement()` returns the next open ledger item.
- `get_interface(module)` returns the stubs.
- `propose_test(path, content)` writes to `tests/` only, runs the red check against stubs, returns "fails correctly" or the error.
- `commit_tests(message)` commits the suite and marks it ready.
- `build(max_tries)` runs the builder loop and returns a compact "green" or "stuck" with the failing test and a short error.
- `feedback()` runs coverage and mutmut and returns only gaps and surviving mutants.

Claude Code's cycle: pick a requirement, write a test, confirm red, commit, build, read a few lines of feedback, write the next test. Every tool result is a few lines.

## Build order

1. Test lock: detect and revert any change to `tests/` during a build.
2. Red check: stub generation and confirming new tests fail correctly.
3. Builder loop: pytest plus Ollama, max tries, green or stuck result.
4. Feedback: coverage and mutmut summaries under a size budget.
5. Requirements ledger in SQLite.
6. MCP server wrapping all of it, with permission tests (writes outside the allowed directory are rejected, results stay under budget).
7. End to end on one toy requirement.
8. Claude Code permission settings denying reads of the target's `src/`.

## Operating rules

- Ollama is started once per build run and stopped at the end, never left always-on (see the user's ollama-on-demand preference).
- Everything is test-driven with pytest and tracked in git.
