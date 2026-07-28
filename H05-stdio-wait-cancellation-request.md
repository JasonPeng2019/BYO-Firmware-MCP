# H05 repair request — cancelled public wait retains the same-board worker

## Authorized scope

Authorized local firmware-server validation. This is a host-only repair in the named local
`BYO-Firmware-MCP` repository. No board, remote target, or third-party system is in scope.

## Verified production failure

- Server commit: `4e1393775167166146c6ee1a0ce310c9747ca3bf`
- Runtime: `.h01-venv-batchstrict/Scripts/pyocd-debug-mcp.exe`
- Negotiated MCP protocol: `2025-03-26`
- Pinned MCP SDK: `1.28.1`
- Main independent evidence:
  `../fresh-experiments/H05_20260725-210246/.agent-workspace/main_verification/result.json`
- Main classification:
  `../fresh-experiments/H05_20260725-210246/.agent-workspace/MAIN_SERVER_FAILURE_REVIEW_002.md`

The public stdio client starts request `410`,
`wait(board_id="h05-host-only", ms=5000)`, then sends a valid
`notifications/cancelled` after 150 ms and immediately submits request `420`,
`wait(board_id="h05-host-only", ms=50)`, on the same connection and board ID.

The SDK promptly emits its dependency-owned cancellation response for request `410`, but the
server-owned synchronous wait action continues sleeping. Request `420` remains blocked by the
same-board worker lock and arrives approximately 4.94 seconds later, only after the cancelled
five-second wait reaches its original end.

## Expected behavior

- The managed wait action notices its existing `ManagedOperation.cancellation_requested` signal
  and ends without recording or returning success.
- It releases the same-board worker/reservation within
  `CANCELLATION_CLEANUP_GRACE_SECONDS + 0.25` seconds of cancellation.
- A subsequent 50 ms wait on the same board and still-open stdio transport completes within that
  cleanup allowance plus its own duration and 0.25 seconds of scheduling tolerance.
- An ordinary uncancelled wait still waits for the requested positive duration, records exactly
  one success event, and returns the existing user-facing success text.
- Direct unit use outside managed dispatch remains deterministic and keeps the existing injectable
  sleep seam.

## Required implementation constraints

Follow `.codex/design_charter.md` throughout planning, implementation, and each distinct test
feature. Prefer the simplest existing managed-operation mechanism. Do not add a polling interval,
board-specific behavior, OS-specific behavior, a new framework, or an arbitrary limit. Do not
touch hardware, permission, plan, routing, or connection semantics.

Do not patch, fork, or vendor the MCP SDK merely to change its response code. MCP `2025-03-26`
prefers no cancellation response and does not define `-32800`; the pinned SDK's
`{code: 0, message: "Request cancelled"}` is accepted as dependency behavior for this repair.

## Verification

1. Unit test: managed wait cancellation wakes on the operation's cancellation event, does not
   record success, and leaves no active operation.
2. Unit regression: ordinary direct and managed waits retain positive-duration validation,
   injected sleep behavior where applicable, exact success event count, and existing output.
3. Public stdio regression: cancel a five-second wait, immediately issue a same-board 50 ms wait,
   prove the second response arrives within the corrected bound rather than after five seconds,
   prove the first never returns tool success, and prove the server remains usable.
4. Run focused tests plus the repository's neutral regression selection.

## Exclusions

- No MCP response-code change.
- No server-wide dependency upgrade.
- No firmware, fixture, SDK, experiment evidence, documentation-only, metadata-only, hardware, or
  unrelated cleanup change.
- No commit, push, deploy, flash, or physical-board action.
