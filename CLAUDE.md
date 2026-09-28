# big_brother

Claude Code writes tests; a local Ollama model writes the implementation. Read `docs/design.md` first.

- Test-driven with pytest: write the test, watch it fail, then implement. Commit every change.
- Run tests with `uv run pytest -q`.
- big_brother's own code lives in `big_brother/`. The "never read src/" rule applies to target projects big_brother drives, not to this package.
- Commands that change machine state (model pulls, installs) go in a tracked script under `scripts/`.
- Ollama runs on demand for a whole run, then stops. Never enable it as a service.
- Before any push, `uv run python -m big_brother.pii . --history` must report 0 findings. The pre-push hook (`scripts/pre-push`, linked into `.git/hooks`) enforces it. A finding checked by hand and judged not PII goes in `.pii-allow` with a comment saying why; never add a real name, address or denylist term there. Commits use the GitHub no-reply identity set in this repo's git config.
