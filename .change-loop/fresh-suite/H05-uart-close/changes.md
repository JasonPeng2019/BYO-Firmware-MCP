# H05 production repair request — UART close failures overwrite primary I/O errors

## Authorized scope

Authorized local firmware-server validation. The target is only this local
`BYO-Firmware-MCP` repository and host-only fake UART adapters. No physical board, serial device,
remote system, or third-party target is in scope. Follow `../.codex/design_charter.md`.

## Independently verified defect

The persistent H05 test agent reproduced four times, and the main model independently reproduced,
that the `finally` close calls in `capture_uart_output`, `write_uart_output`, and
`exchange_uart_output` escape as raw backend exceptions.

- A close-only failure returns raw `RuntimeError("H05_UART_CLOSE")`, omitting operation, device,
  baud, and recovery context.
- When the primary read/write operation and close both fail, the raw close exception becomes the
  top-level failure and demotes the normalized primary operation error to `__context__`.

Evidence:

- `../fresh-experiments/H05_20260725-210246/.agent-workspace/evidence/req008_postrepair_summary.json`
- `../fresh-experiments/H05_20260725-210246/.agent-workspace/evidence/req008_postrepair_read_plus_close_capture/result.json`
- `../fresh-experiments/H05_20260725-210246/.agent-workspace/evidence/req008_postrepair_write_plus_close_write/result.json`
- `.change-loop/fresh-suite/H05-uart-close/main_verification/result.json`

## Required behavior

1. Every opened handle is still closed exactly once; do not reopen or retry implicitly.
2. A close-only failure is a normalized, operator-facing UART failure naming the operation,
   device, baud, and underlying close exception.
3. If primary open/read/write/exchange work and close both fail, the primary operation remains the
   principal/top-level failure. The close uncertainty remains visible and causally attached after
   the primary failure; neither fact may be lost.
4. Healthy capture/write/exchange, primary-only failures, cancellation, explicit reopen behavior,
   results, bounds, and all public server callers remain unchanged.

## Constraints and proof

- Prefer one small shared internal cleanup/error-composition boundary rather than three divergent
  fixes, but add no retry, timeout, public API, dependency, platform/board/provider case, or broad
  refactor.
- Preserve the accepted uncommitted H05 wait-cancellation and marker-cleanup repairs byte-for-byte.
- Spec tests must cover close-only plus primary-and-close cases for capture, write, and exchange,
  exact causal ordering, context text, and exactly-one close.
- Regression tests must cover healthy controls, primary-only controls, explicit capture reopen,
  cancellation/return behavior, and at least one production caller/delegate boundary.
- Do not edit firmware, experiment evidence, docs, dependencies, unrelated production code, or
  existing tester-owned H05 files. Do not commit, push, deploy, flash, or operate hardware.
- Every repair role rereads the complete charter before work, between distinct features, before
  verification, and before final reporting, appending checkpoints to
  `.change-loop/fresh-suite/H05-uart-close/DESIGN_CHARTER_CHECKS.md`.
