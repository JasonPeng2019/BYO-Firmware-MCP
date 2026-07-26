# Persistent doer follow-up after the main-model final review

Stay in the BYO-Firmware-MCP repository and resume the existing doer role. Edit only the
doer-owned production file `src/pyocd_debug_mcp/kernel/processes.py`. Do not edit either tester's
files, README, dependency metadata, the reviewed plan, or runtime review/evidence files.

Before acting, reread:

1. `../.codex/design_charter.md`
2. `.change-loop/fresh-suite/H00-H01-high-level-audit/plan.md`
3. `.change-loop/fresh-suite/H00-H01-high-level-audit/main-final-review.md`
4. `.change-loop/fresh-suite/H00-H01-high-level-audit/state/test_report.md`

Fix the remaining CL-001 failure under the existing plan. The primary
`AttributeError`/`OSError` raised while obtaining liveness or birth-time identity must remain the
cause of the final `ProcessIdentityUnavailable` even if `CloseHandle` also fails, while close must
still be attempted. Preserve:

- a close-only access failure as the cause;
- an already-specific `ProcessIdentityUnavailable` as primary if close also fails;
- `None` for an exited/missing process;
- the unchanged successful token;
- the existing Job-helper `OSError` contracts.

Prefer the simplest flat control flow. Do not add a generic cleanup framework, suppression,
dependency, or configuration change. Use the existing Windows-specific environment
`UV_PROJECT_ENVIRONMENT=.h01-venv` for focused Windows checks so you do not collide with the WSL
neutral `.venv`. Run the new primary-error test plus the existing close and specific-error tests,
then Ruff, Pyright, and `git diff --check`. Do not start duplicate commands.

At the end, reread `../.codex/design_charter.md` and report both charter checkpoints, the exact
production change, and focused evidence.
