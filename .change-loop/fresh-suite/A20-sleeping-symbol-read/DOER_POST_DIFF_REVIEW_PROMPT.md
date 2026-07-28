Authorized local firmware validation. Work only in this BYO-Firmware-MCP repository. This is
host-only server repair work and authorizes no hardware action.

Resume the existing persistent A20 implementation-doer role. The main/orchestrating model has
accepted two post-gate adversarial findings as required execution corrections under the unchanged
CL-001/CL-002 plan. They are recorded in the final section of
`.change-loop/fresh-suite/A20-sleeping-symbol-read/plan-review.md` and are no longer optional.

Before analysis and immediately before each distinct production edit, reread the complete
`../.codex/design_charter.md`, verify SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`, and append a concise stage
entry to `.change-loop/fresh-suite/A20-sleeping-symbol-read/DESIGN_CHARTER_CHECKS.md`. Reread it
again before verification and final verdict.

Make only these smallest production corrections:

1. In `_read_coherent_scalar`, guarantee exactly one restoration attempt after every failure raised
   by `read_target_memory` once the inserted halt succeeded, including a `BaseException` such as
   synchronous cancellation/interrupt. Preserve and re-raise the original primary failure when
   restoration succeeds. If restoration also raises any `BaseException`, raise one honest error
   containing both failure types/messages and chain it from the primary failure. Do not swallow
   cancellation/interrupt, retry, change the already-HALTED path, or broaden lifecycle behavior to
   raw reads/writes.
2. Extend only the public `read_memory_symbol` docstring so it explicitly explains `board_id`,
   `symbol`, `width`, and optional `elf_artifact`, plus one concise invocation example. Preserve
   the schema, return format, lifecycle/error/recovery explanation, and MCP-facing concision.

You own production source only. Do not edit any test, tester manifest/snapshot, test command,
plan, request, or unrelated dirty file. Run focused diagnostics, Ruff, Pyright, and
`git diff --check` as useful, but do not run hardware or commit/push/deploy. Finish with the exact
production files changed and checks actually run.
