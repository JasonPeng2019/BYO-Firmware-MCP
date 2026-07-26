# Persistent regression-tester final review

Stay in BYO-Firmware-MCP and resume the same regression-tester role. Reread
`../.codex/design_charter.md`, the reviewed plan, `main-final-review.md`, the latest neutral
report, and the final diff. Try to prove the final CL-001 primary-error preservation or CL-003
test-helper correction broke adjacent behavior.

Edit only your owned `tests/test_h00_repository_regressions.py` and required regression
command/manifest, and only if a credible regression is not already covered. Do not edit
production, the spec tester's file, README, metadata, plan, or review/evidence files. Avoid
duplicating the spec suite's Windows identity controls.

Use `UV_PROJECT_ENVIRONMENT=.h01-venv` for any local Windows run. Run your focused regression
suite once, without duplicate processes. Keep the recorded command Bash-neutral and
self-preparing for the neutral harness:

`uv sync --locked && uv run --locked --no-sync pytest -q tests/test_h00_repository_regressions.py`

Reread the charter at finish and report both checkpoints, blast-radius assessment, owned-file
status, and focused evidence.
