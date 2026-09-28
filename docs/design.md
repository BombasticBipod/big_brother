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
- The builder must never write `tests/`. The **suite lock** keeps `tests/` locked at all times, not only during builds: the files and directories are read-only on disk, and the locked commit is recorded in `.git/big_brother/suite_lock.json`. The only way to change tests is `accept(message)`, which unlocks, lets the test writer edit, commits only `tests/` and locks again at the new commit. If the writer fails partway, its edits are discarded and the old suite stays locked.
- Read-only permissions stop accidental writes, not a process that chmods its way in, so after every builder iteration the lock also compares `tests/` with the locked commit (modified, added, deleted, staged or committed). Any change aborts the run and reverts `tests/`.
- Runs take turns, never overlap: `build()` holds a lock so tests cannot change mid-run.

Note: these rules apply to the *target* project that big_brother drives, not to big_brother's own source in `big_brother/`.

## Interface and red check

A target project's `interface/` holds `.pyi` files (signatures, docstrings, classes, constants) written by the test writer. The interface is contract, like `tests/`: the builder must not write it. The suite lock guards one directory today and `accept()` commits only that directory, so before step 6 the lock must cover `interface/` and `tests/` together, because the test writer changes them together.

`make_stubs` turns each `.pyi` into a module whose function bodies raise `NotImplementedError("<qualified name>")`. `red_check` copies `tests/` and the stubs into a temporary directory and runs only the named test files there, with a clean environment (`PYTHONPATH` set to the stubs only, no inherited pytest options, no root conftest or pytest config from the target). The real `src/` is never importable, so a test cannot pass by reaching real code and no implementation text reaches the result. Only exception types and one-line messages are recorded, never tracebacks.

Verdicts: **red** (failed with `NotImplementedError` or `AssertionError`, in setup or call), **passes_on_stubs** (the test asks for no behavior), **broken** (collection error, wrong exception, skip, timeout, no tests). The summary is capped at 500 characters.

Limits: red against stubs is automatic for any test that calls the interface, even when the real implementation already satisfies it. Only a build shows whether a new test asks for new behavior. Target tests run on big_brother's own Python interpreter, so a target's own dependencies are not installed there; that is fine for the step 7 toy project and must be revisited for real targets. `shutil.copytree` follows symlinks, so a symlink in `tests/` pointing into `src/` would copy real source into the check; step 6's `propose_test` must refuse symlinks.

## Builder loop

`build(repo, model, max_tries)` refuses to start unless the suite is locked and `src/` is fully committed, and holds a run lock (`flock` on `.git/big_brother/build.lock`) for its whole length. `accept()` takes the same lock, so tests never change mid-build. Each try sends the model the interface, the current source, the failing test files and the tail of the pytest output. It then writes back only the files the reply names that the interface declares: `src/<module>.py` for each `interface/<module>.pyi`. Any other path is refused and reported back to the model. Tests run with `PYTHONPATH` set to `src/`, which comes before site-packages, so without this rule a reply could plant `src/pytest.py` or `src/sitecustomize.py` and fake a green run. The suite lock is enforced after the writes and again after the test run, so green always comes from the locked tests.

Green commits `src/` only. Stuck, or any exception (model error, interrupt, tampering), restores `src/` to HEAD.

The summary and progress lines go to the test writer, so they carry only counts, test ids and exception types. The one exception is the message of an AssertionError, which comes from the test. A syntax error or other exception raised inside `src/` can quote implementation text, so those messages are dropped. Prompts, replies and full pytest output go to `.git/big_brother/build.log`. That log contains implementation text, so step 8 must deny reads of it as well as `src/`.

The model call always sets `num_ctx`, because a server default that is too small silently cuts the front of the prompt, where the format rules are. Ollama is started for the build and stopped at the end if the build started it. Tests use a fake model. The real-model check is opt-in (`uv run pytest -m ollama`) so the default suite passes with Ollama stopped.

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

1. Suite lock: `tests/` locked at all times, unlocked only to accept committed changes; tampering detected and reverted. (Done.)
2. Red check: stub generation and confirming new tests fail correctly. (Done.)
3. Builder loop: pytest plus Ollama, max tries, green or stuck result. (Done.)
4. Feedback: coverage and mutmut summaries under a size budget.
5. Requirements ledger in SQLite.
6. MCP server wrapping all of it, with permission tests (writes outside the allowed directory are rejected, results stay under budget).
7. End to end on one toy requirement.
8. Claude Code permission settings denying reads of the target's `src/` and of `.git/big_brother/build.log`.

## Operating rules

- Ollama is started once per build run and stopped at the end, never left always-on (see the user's ollama-on-demand preference).
- Everything is test-driven with pytest and tracked in git.
