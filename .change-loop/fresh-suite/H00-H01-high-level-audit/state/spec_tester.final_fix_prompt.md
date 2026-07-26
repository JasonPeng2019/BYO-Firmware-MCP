# Persistent spec-tester correction after the doer follow-up

Stay in BYO-Firmware-MCP and resume the same spec-tester role. Edit only your owned
`tests/test_h00_repository_contract.py` and required spec command/manifest. Do not edit
production source, README, metadata, plan, review, or neutral-report files.

Reread `../.codex/design_charter.md`, the reviewed plan, the main final review, and the latest
neutral report. The doer has repaired the CL-001 primary-error failure. Correct the CL-003 helper
implementation in your owned test file so the two new absence tests pass without weakening them:

- an already-absent tree is successful;
- a `PermissionError` followed by confirmed absence is successful.

Preserve immediate real removal, the five-second monotonic retry bound, short sleeps, the final
concrete persistent `PermissionError`, readonly recovery, and no suppression. Keep the corrected
honest callback annotation. Use `UV_PROJECT_ENVIRONMENT=.h01-venv` and run only the new CL-001
primary-error test plus the three CL-003 cleanup controls in this turn. Do not run the full
clean-candidate transaction or start duplicate processes; the neutral gate will run the full
recorded suite.

Record the complete self-preparing full spec command and the one-file manifest. Reread the charter
at finish and report both checkpoints and focused evidence.
