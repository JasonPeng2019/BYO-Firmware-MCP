Authorized local firmware validation. Work only in this BYO-Firmware-MCP repository. This is
host-only adversarial regression work and authorizes no hardware action.

Resume the existing persistent A20 regression-tester role after the doer changed
`_read_coherent_scalar` from `Exception` cleanup to `BaseException` cleanup and expanded only the
public handler docstring, and after the spec tester added direct cancellation/help coverage.

Reread the complete `../.codex/design_charter.md` before analysis, before any edit, before
verification, and before the final verdict; verify SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb` and append the required
stage entries to `.change-loop/fresh-suite/A20-sleeping-symbol-read/DESIGN_CHARTER_CHECKS.md`.

Trace the corrected production diff through callers, construction sites, Layer-2 translation,
adjacent raw memory operations, and cancellation/cleanup behavior. Add or modify only your owned
`tests/test_regression_a20_sleeping_symbol_read.py` if a distinct nonduplicated regression is
needed; do not duplicate the spec tester's direct `KeyboardInterrupt`, dual failure, or help
assertions merely for count. Preserve the exact regression command and manifest unless the owned
path truly changes.

Do not edit production source, the spec tester's file/manifest/command, plan, request, or unrelated
tests. Run the exact regression command under Bash, relevant adjacent focused tests if warranted,
Ruff on your owned file, and `git diff --check`. Report exact evidence and any remaining actionable
blast-radius defect. Do not commit/push/deploy.
