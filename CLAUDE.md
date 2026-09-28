# big_brother

Claude Code writes tests; a local Ollama model writes the implementation. Read `docs/design.md` first.

- Test-driven with pytest: write the test, watch it fail, then implement. Commit every change.
- Run tests with `uv run pytest -q`.
- big_brother's own code lives in `big_brother/`. The "never read src/" rule applies to target projects big_brother drives, not to this package.
- Commands that change machine state (model pulls, installs) go in a tracked script under `scripts/`.
- Ollama runs on demand for a whole run, then stops. Never enable it as a service.
