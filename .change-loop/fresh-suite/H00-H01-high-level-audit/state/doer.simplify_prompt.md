# Main-model final simplicity correction

Resume the same BYO-Firmware-MCP doer role. Reread `../.codex/design_charter.md` and the CL-001
plan item before acting. Edit only `src/pyocd_debug_mcp/kernel/processes.py`.

The current behavior is correct, but the two booleans `identity_failure` and
`native_identity_failure` carry the same cleanup decision and are unnecessary complexity.
Simplify them to one clearly named primary/body-failure flag (or an equivalently flatter minimal
form) while preserving every now-tested behavior:

- close is always attempted after a handle is opened;
- any primary `ProcessIdentityUnavailable`, `AttributeError`, or `OSError` remains primary if
  close also fails;
- a close-only access failure remains observable and normalized by the outer boundary;
- missing/exited and live-token returns are unchanged.

Do not edit tests, README, metadata, plan, or runtime evidence. Use
`UV_PROJECT_ENVIRONMENT=.h01-venv` to run the three focused identity tests, Ruff, Pyright, and
`git diff --check`. Do not run duplicate processes. Reread the charter at finish and report both
checkpoints.
