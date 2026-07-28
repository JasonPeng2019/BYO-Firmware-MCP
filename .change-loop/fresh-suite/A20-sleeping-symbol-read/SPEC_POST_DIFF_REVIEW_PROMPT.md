Authorized local firmware validation. Work only in this BYO-Firmware-MCP repository. This is
host-only adversarial test work and authorizes no hardware action.

Resume the existing persistent A20 spec-tester role after the implementation doer corrected the
two accepted post-gate findings recorded in the final section of
`.change-loop/fresh-suite/A20-sleeping-symbol-read/plan-review.md`.

Reread the complete `../.codex/design_charter.md` before analysis, before editing, before
verification, and before the final verdict; verify SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb` and append the required
stage entries to `.change-loop/fresh-suite/A20-sleeping-symbol-read/DESIGN_CHARTER_CHECKS.md`.

Adversarially extend only your owned
`tests/test_a20_sleeping_symbol_read_spec.py` so the neutral spec command proves:

1. A non-`Exception` primary failure (for example `KeyboardInterrupt`) raised by the scalar read
   after an inserted halt still causes exactly one `resume`, then the exact primary object/type is
   re-raised when restoration succeeds.
2. If useful to close the precise contract, a non-`Exception` read plus a non-`Exception`
   restoration failure reports both types/messages and chains from the primary.
3. The published help explicitly identifies `board_id`, `symbol`, `width`, and `elf_artifact`, and
   contains the concrete `read_memory_symbol(...)` example in addition to the existing lifecycle,
   return, honest-failure, and recovery phrases.

Do not edit production source, the regression tester's file/manifest/command, the plan, request,
or unrelated tests. Keep the existing spec command unless the owned file path truly changes.
Run the exact recorded spec command under Bash, Ruff on your owned test, and `git diff --check`.
Report the exact tests added/changed and actual results. Do not commit/push/deploy.
