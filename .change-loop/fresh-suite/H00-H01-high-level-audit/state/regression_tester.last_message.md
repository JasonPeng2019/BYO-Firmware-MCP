Final review complete.

- Charter checkpoints: reread before review and again at finish.
- CL-001 primary native-error preservation and CL-003 absent-tree/permission-release behavior are already covered by the spec suite; no duplicate regression test added.
- Focused suite passed once using `UV_PROJECT_ENVIRONMENT=.h01-venv`: `1 passed`.
- Owned test: `tests/test_h00_repository_regressions.py`.
- Recorded Bash-neutral command:

`uv sync --locked && uv run --locked --no-sync pytest -q tests/test_h00_repository_regressions.py`

Residual risk: native Windows behavior still requires the spec suite and native-host gate; no additional credible regression gap found.
