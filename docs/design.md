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
- Code under build is untrusted. Every run of it (the builder's pytest runs, feedback's coverage and mutmut runs, and red checks of proposed tests) happens in a temporary copy inside a bubblewrap sandbox (`big_brother/sandbox.py`). The sandbox sees `/usr`, big_brother's virtualenv and package (read-only), fresh `/proc`, `/dev` and `/tmp`, and one writable directory: the temporary copy. It has no home directory, no target, no `.git`, no network and no view of other processes; all capabilities are dropped and the root is read-only. A missing `bwrap` is an error before anything is written, never a silent fallback. The kernel is shared, so escaping needs a kernel exploit.
- Behind the sandbox, as defense in depth, the build also pins the locked commit in memory (a rewritten `suite_lock.json` is restored and reported as tampering), refuses to start while suite files are staged, and treats any staged file that appears during the build as tampering: it is wiped and the build aborts.

Note: these rules apply to the *target* project that big_brother drives, not to big_brother's own source in `big_brother/`.

## Interface and red check

A target project's `interface/` holds `.pyi` files (signatures, docstrings, classes, constants) written by the test writer. The interface is contract, like `tests/`: the builder must not write it. The suite lock covers `interface/` and `tests/` together, and `accept()` commits both, because the test writer changes them together. The lock state still keys the commit by the tests directory, so a lock made before it covered the interface reads as locked and now guards both.

`make_stubs` turns each `.pyi` into a module whose function bodies raise `NotImplementedError("<qualified name>")`. `red_check` copies `tests/` and the stubs into a temporary directory and runs only the named test files there, with a clean environment (`PYTHONPATH` set to the stubs only, no inherited pytest options, no root conftest or pytest config from the target). The real `src/` is never importable, so a test cannot pass by reaching real code and no implementation text reaches the result. Only exception types and one-line messages are recorded, never tracebacks.

Verdicts: **red** (failed with `NotImplementedError` or `AssertionError`, in setup or call), **passes_on_stubs** (the test asks for no behavior), **broken** (collection error, wrong exception, skip, timeout, no tests). The summary is capped at 500 characters.

`red_check(overlay=DIR)` lays staged `interface/` and `tests/` files over the committed ones in the throwaway copy. A symlink anywhere in the interface or tests, committed or staged, makes the check broken before anything is copied.

Limits: red against stubs is automatic for any test that calls the interface, even when the real implementation already satisfies it. Only a build shows whether a new test asks for new behavior. Target tests run on big_brother's own Python interpreter, so a target's own dependencies are not installed there; that is fine for the step 7 toy project and must be revisited for real targets.

## Builder loop

`build(repo, model, max_tries)` refuses to start unless the suite is locked and `src/` is fully committed, and holds a run lock (`flock` on `.git/big_brother/build.lock`) for its whole length. `accept()` takes the same lock, so tests never change mid-build. Each try sends the model the interface, the current source, the failing test files and the tail of the pytest output. It then writes back only the files the reply names that the interface declares: `src/<module>.py` for each `interface/<module>.pyi`. Any other path is refused and reported back to the model. Tests run with `PYTHONPATH` set to `src/`, which comes before site-packages, so without this rule a reply could plant `src/pytest.py` or `src/sitecustomize.py` and fake a green run. The suite lock is enforced after the writes and again after the test run, so green always comes from the locked tests.

Green commits `src/` only. Stuck, or any exception (model error, interrupt, tampering), restores `src/` to HEAD.

The summary and progress lines go to the test writer, so they carry only counts, test ids and exception types. The one exception is the message of an AssertionError raised in a test file. Any exception raised inside `src/`, including a failed `assert` there or a syntax error, can quote implementation text, so those messages are dropped. Prompts, replies and full pytest output go to `.git/big_brother/build.log`. That log contains implementation text, so step 8 must deny reads of `.git/big_brother/` as well as `src/`.

The model call always sets `num_ctx`, because a server default that is too small silently cuts the front of the prompt, where the format rules are. Ollama is started for the build and stopped at the end if the build started it. Tests use a fake model. The real-model check is opt-in (`uv run pytest -m ollama`) so the default suite passes with Ollama stopped.

## Feedback

Red check, coverage and mutation testing (mutmut) all run locally at no token cost. Claude Code only sees short summaries: gaps and surviving mutants.

`feedback(repo)` takes the run lock, enforces the suite lock and refuses uncommitted `src/`. It extracts `src/` and `tests/` from HEAD with `git archive` into a temporary directory, so the target is never touched and the result describes committed code. There it runs the suite under branch coverage, then `mutmut run`. A suite that is not green stops before mutation with "suite is not green; build first". A timeout or a missing coverage report returns status `failed` with a one-line reason.

Results are named only by what the interface declares: `calc.sign`, `calc.Counter.bump`. Helpers, nested functions and undeclared classes in a declared module fold into "<module> (other code)". Modules with no `.pyi` fold into one "other modules" bucket. Line numbers are left out. The summary lists coverage gaps and surviving mutants, lowest coverage and most survivors first, in the 500-character budget. Progress lines carry counts only. Full coverage and mutmut output, which quotes mutated source lines, goes to `.git/big_brother/feedback.log`.

mutmut is pinned to 3.8 because the parser reads mutmut's internal mutant names (`<module>.x_<function>__mutmut_<n>`, `<module>.xǁ<Class>ǁ<method>__mutmut_<n>`).

The interface names are read from the working tree's `interface/`. That matches the locked commit, because `feedback` enforces the suite lock, which covers `interface/`, before it reads anything.

## Staging

The suite lock unlocks only inside one `accept()` block, but the test writer proposes files over several tool calls. Proposed files wait in `.git/big_brother/staged/`, mirroring their paths, and touch the target only on commit.

- `Staging.propose(path, content)` accepts only `tests/**/*.py` and `interface/**/*.pyi`, refuses paths through a symlink in the target and content over 100,000 characters, then checks everything staged. The writer proposes interface files through the same staging area, so no separate interface tool path is needed.
- The check runs the red check with the overlay. Staged tests must be red. When an interface file is staged, the committed tests run too and must not be broken, so a changed signature is caught here instead of showing up as a stuck build. With no tests anywhere, the merged interface must still turn into stubs.
- `Staging.commit(message)` re-checks, then copies the staged files in inside `SuiteLock.accept()`, which commits both directories and relocks, and empties staging.
- `propose` and `discard` take the run lock and `commit` takes it through `accept()`, so staging never changes during a build.

## Requirements ledger

The ledger is a SQLite file at `.git/big_brother/ledger.sqlite` in the target (stdlib `sqlite3`, no dependency). It is bookkeeping, not contract, so it is not committed. The user adds requirements with `python -m big_brother.ledger --repo TARGET add "TEXT"` and reads them with `list`.

Each requirement is **open** (no tests yet), **tested** (tests committed; `suite_commit` is the latest such commit) or **done** (a build went green; `src_commit` is its commit). Committing more tests keeps a requirement tested and updates the commit. A stuck build keeps it tested and counts in `stuck_builds`. Done is final. `next()` returns the oldest tested requirement before the oldest open one, so a stuck build is finished before new work starts.

## MCP server

Claude Code is the only MCP client. The builder is a backend job the server runs. `big_brother/server.py` uses the `mcp` Python SDK 2.x (`MCPServer`) over stdio: `python -m big_brother.server TARGET`. `scripts/register_mcp.sh TARGET` adds it to the target's `.mcp.json` with `claude mcp add --scope project`. Tools:

- `next_requirement()` returns the next ledger item: tested (possibly stuck) before open.
- `get_interface(module)` returns a module's `.pyi`, staged version first, capped at 8,000 characters. An empty name lists the modules. Only dotted module names are accepted.
- `propose_test(path, content)` stages a file under `tests/` only and returns the check of everything staged ("red: N tests fail correctly", "passes_on_stubs: ..." or "broken: ...").
- `propose_interface(path, content)` does the same for `interface/**/*.pyi`.
- `discard_staged()` drops every staged file.
- `commit_tests(message, requirement_id)` commits the staged files through the suite lock and marks the requirement tested. An unknown or done requirement is refused before anything is committed.
- `build(max_tries)` starts Ollama on demand, runs the builder loop and returns its summary. Green marks every tested requirement done at the new commit; stuck counts against each of them.
- `feedback()` returns the coverage and mutation summary.

Refusals are tool errors (`is_error`), clipped to 400 characters, so the writer reads them and retries. Results stay within the 500-character summary budget, except `get_interface`. Stdout carries the protocol, so build and feedback progress (counts only) goes to `.git/big_brother/progress.log` for the user to `tail -f`. Stdio servers have no per-call timeout in Claude Code (the default wall-clock limit is about 28 hours), so `build` and `feedback` run synchronously.

Claude Code's cycle: pick a requirement, write a test, confirm red, commit, build, read a few lines of feedback, write the next test. Every tool result is a few lines.

## Reference answers

The test writer can also hand in its own implementation, kept as training data for small builder models. The server's `--reference` option sets when it asks:

- `none` (default): never. The `submit_reference` tool is not offered.
- `stuck`: after a stuck build. The stuck summary ends with a request naming the stuck requirements. A requirement with no stuck build is refused.
- `all`: after every `commit_tests`. Any requirement with tests is accepted.

`submit_reference(requirement_id, files)` takes complete `src/<module>.py` files. Only files the interface declares are accepted, as in the builder, because a stray `src/sitecustomize.py` could fake a green run and poison the data. Content is capped at 100,000 characters per file. It takes the run lock, enforces the suite lock, and runs the locked suite in the sandbox against the submitted files alone: none of the builder's `src/` is copied in, so a wrong reference cannot pass on the builder's code. Each submission, green or red, is appended to `.git/big_brother/reference/references.jsonl` with the requirement id, trigger, locked suite commit, `src/` HEAD, the files, the green flag and the counts. The two commits are enough to rebuild the prompt the builder got. The writer sees "reference green: N tests pass" or the failing test ids.

The builder never sees a reference: the working tree is not touched, the builder's prompt is built only from `interface/`, `src/` and `tests/`, and its sandbox has no `.git`. The writer's deny rules on `.git/big_brother/**` keep it from reading earlier references too.

Cost: a reference is paid in writer tokens, which the design otherwise saves. `stuck` spends them only where the small model fails, which is also the most useful data. Check the model provider's terms before training on its output.

## Target settings

`python -m big_brother.permissions TARGET` merges into the target's `.claude/settings.json` and adds a rules section to its `CLAUDE.md` once:

- `permissions.deny`: `Read(/src/**)`, `Read(/.git/big_brother/**)`, and `Edit` of `src/`, `tests/` and `interface/`, so suite changes go through the MCP tools. A `/path` rule in project settings anchors at the target's root. Claude Code applies Read deny rules to its file tools, best-effort to Grep and Glob, and to Bash file commands it recognizes (`cat`, `head`, `tail`, `sed`, redirections). It does not apply them to `grep -r x .` or to a script that opens files itself.
- `sandbox`: enabled, `allowUnsandboxedCommands: false`, and `filesystem.denyRead` of `./src` and `./.git/big_brother`, which closes that gap at the OS level for every Bash command and its children. On Linux the sandbox needs `bubblewrap` and `socat`.

The MCP server runs outside Claude Code's sandbox, so builds and feedback still read `src/`. The tests check the files written, not that Claude Code enforces them.

## End to end

`python -m big_brother.e2e TARGET` creates a toy target (an interface for `calc.add`, a locked suite, one requirement) and plays the test writer against the real server subprocess over stdio: next_requirement, get_interface, propose_test, commit_tests, build, feedback, next_requirement. Each step is printed and logged to `TARGET/.git/big_brother/e2e.log`. The real-model run is also an opt-in test (`pytest -m ollama`).

## Build order

1. Suite lock: `tests/` locked at all times, unlocked only to accept committed changes; tampering detected and reverted. (Done.)
2. Red check: stub generation and confirming new tests fail correctly. (Done.)
3. Builder loop: pytest plus Ollama, max tries, green or stuck result. (Done.)
4. Feedback: coverage and mutmut summaries under a size budget. (Done.)
5. Requirements ledger in SQLite. (Done.)
6. MCP server wrapping all of it, with permission tests (writes outside the allowed directory are rejected, results stay under budget). (Done.)
7. End to end on one toy requirement. (Done.)
8. Claude Code permission settings denying reads of the target's `src/` and of everything under `.git/big_brother/` (`build.log`, `feedback.log` and any later log), since those logs quote implementation text. (Done.)
9. Sandbox: bubblewrap around every run of model-written code and proposed tests, so absolute paths cannot reach the target, the home directory or the network. (Done.)

## Operating rules

- Ollama is started once per run and stopped at its end, never left always-on and never restarted per step (see the user's ollama-on-demand preference). For the command-line builder a run is one build; for the MCP server it is the whole session: the first `build` starts Ollama and the session's end stops it.
- Everything is test-driven with pytest and tracked in git.
