# H05 production repair request — marker-removal failure swallowed by worker close

## Authorized scope

Authorized local firmware-server validation. The target is only this local
`BYO-Firmware-MCP` repository and run-owned host processes. No physical board, remote system, or
third-party target is in scope. Follow the repository design charter and do not perform hardware
actions.

## Server identity and existing work

- Server HEAD: `4e1393775167166146c6ee1a0ce310c9747ca3bf`.
- Preserve the already accepted, uncommitted H05 `misc.wait` cancellation repair and its tests.
- New production scope is only worker-close cleanup-error propagation in
  `src/pyocd_debug_mcp/adapters/swd_process.py`.

## Independently verified defect

When a provider worker has already exited, `_WorkerClient.close()` calls its ordinary graceful
`close` request. That request detects EOF, invalidates the worker, confirms process cleanup, and
attempts to remove the ownership marker. If `ProcessMarkerStore.remove` raises `OSError`, the
existing marker-removal helper raises an actionable `TargetConnectionError` and retains the
marker. However, `_WorkerClient.close()` catches that exception in the same broad block used to
suppress harmless graceful-close diagnostics, then returns success because process termination
was confirmed. This silently misreports incomplete ownership cleanup.

The persistent H05 test agent reproduced the behavior twice with a case-local marker root:

- `fresh-experiments/H05_20260725-210246/.agent-workspace/evidence/req007_postrepair_retry3_marker_unlink_failure_case_1/result.json`
- `fresh-experiments/H05_20260725-210246/.agent-workspace/evidence/req007_postrepair_retry3_marker_unlink_failure_case_2/result.json`
- summary:
  `fresh-experiments/H05_20260725-210246/.agent-workspace/evidence/req007_postrepair_retry3_summary.json`

The main model independently reproduced the same result and a healthy control:

- `.change-loop/fresh-suite/H05-marker-unlink/main_verification/fault/result.json`
- `.change-loop/fresh-suite/H05-marker-unlink/main_verification/healthy/result.json`

In every failing reproduction:

- the injected `ProcessMarkerStore.remove` boundary ran exactly once and raised
  `OSError("H05_MARKER_UNLINK")`;
- first `close()` returned without exception while the marker remained;
- after restoring normal removal, a second `close()` removed the marker without another provider
  request;
- the same setup without the injected removal failure removed the marker and returned normally.

## Required behavior

1. A marker-removal failure after confirmed worker death must escape the initiating `close()` as
   the existing typed, actionable cleanup error, retain the marker, and preserve the original
   `OSError` as its cause.
2. A later `close()` after the removal problem is corrected must retry only marker removal,
   remove the marker, and return without sending another provider request or terminating again.
3. Continue suppressing ordinary graceful-close protocol/EOF diagnostics when process termination
   and marker cleanup are both confirmed. Do not turn a clean forced close into an operator-facing
   failure.
4. Unconfirmed termination must remain fail-closed and retain its actionable marker.
5. Normal live-worker close, already-closed clean close, request ordering, deadlines, process
   identity checks, marker schema/root selection, cross-platform behavior, and all unrelated
   provider/MCP/hardware behavior must remain unchanged.

## Design constraints

- Reread `../.codex/design_charter.md` before planning/implementation, between distinct features
  if any, before verification, after a risky diff, and before acceptance.
- Append each role's checkpoint to
  `.change-loop/fresh-suite/H05-marker-unlink/DESIGN_CHARTER_CHECKS.md`.
- Prefer the smallest local control-flow correction in `_WorkerClient.close`; do not add a new
  retry loop, timeout, board/OS/toolchain special case, public API, or cleanup framework.
- Do not edit firmware, fresh-experiment evidence, documentation, dependencies, generated
  artifacts, or unrelated production code. Do not commit, push, deploy, flash, or operate hardware.

## Required automated proof

- Spec coverage must reproduce the already-dead-worker path and show the first `close()` raises
  the actionable marker-removal error with `OSError("H05_MARKER_UNLINK")` as cause, leaves the
  marker, and the second `close()` removes it without another provider request.
- Regression coverage must prove a healthy externally exited worker still closes cleanly, an
  ordinary graceful-close failure remains suppressed only when termination plus marker cleanup are
  confirmed, and unconfirmed termination remains actionable with its marker retained.
- Both neutral change-loop suites must pass in the same iteration, followed by the exact persistent
  H05 test-agent retest.
