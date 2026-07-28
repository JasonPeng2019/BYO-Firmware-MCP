# H05 repair request — confirmed marker-removal failure is reported as successful close

## Authorized scope

Authorized local firmware-server validation. This is a host-only repair in the named local
`BYO-Firmware-MCP` repository. No physical board, serial device, remote target, or third-party
system is in scope.

## Verified production failure

- Server HEAD: `4e1393775167166146c6ee1a0ce310c9747ca3bf`
- Runtime: `.h01-venv-batchstrict/Scripts/python.exe`
- Current accepted dirty baseline: the already neutral-gated H05 cancellation repair in
  `src/pyocd_debug_mcp/tools/misc.py`, SHA-256
  `f29e666dfa9a12f6248b58362b4fd8badcaff435e99ecabc035eafc60c36d0c7`
- Test-agent evidence:
  `../fresh-experiments/H05_20260725-210246/.agent-workspace/evidence/req007_postrepair_retry3_summary.json`
- Main independent evidence:
  `../fresh-experiments/H05_20260725-210246/.agent-workspace/main_verification_req007/result.json`
- Main classification:
  `../fresh-experiments/H05_20260725-210246/.agent-workspace/MAIN_SERVER_FAILURE_REVIEW_003.md`

With `PYOCD_MCP_RUNS_ROOT` set before production import, a real `_WorkerClient` completes one
healthy fake-provider request. After the provider is independently confirmed dead,
`ProcessMarkerStore.remove` is made to raise one `OSError`. The first
`_WorkerClient.close()` calls that removal boundary exactly once but returns success while the
recovery marker remains. A second close removes the marker without another provider request.

Repository inspection shows the failure crosses two existing production paths:

1. `_WorkerClient.call("close", ...)` encounters the dead worker and invokes `_invalidate`;
2. `_invalidate` confirms termination, sets `_cleanup_confirmed=True`, then
   `_remove_confirmed_marker` raises a typed `TargetConnectionError` because marker removal failed;
3. `_WorkerClient.close` catches that exception in the broad diagnostic handler for graceful-close
   failures, skips its own final cleanup because `_closed=True`, and returns solely because
   `_cleanup_confirmed=True`, even though `_marker` remains.

## Expected behavior

1. When worker/process cleanup is confirmed but marker removal raises `OSError`, the first close
   raises the existing typed, actionable cleanup failure and retains the marker.
2. After marker removal becomes available, a second close retries only marker removal, succeeds,
   clears the stored marker, and sends no additional provider request.
3. A normal healthy close still confirms process cleanup, removes its marker, and succeeds.
4. A graceful provider-close/protocol error may remain diagnostic-only when final process cleanup
   and marker removal both succeed, preserving the current close contract.
5. Unconfirmed termination remains fail-closed and actionable with its marker retained.

## Required implementation constraints

Reread and apply `.codex/design_charter.md` before analysis, before editing, between any distinct
implementation features, before verification, and before acceptance. Make the smallest
state-based correction in the existing `_WorkerClient.close` owner. Do not add polling, retry
loops, timing constants, OS branches, board branches, marker deletion fallbacks, new abstractions,
or security/adversarial-input hardening.

Preserve the accepted uncommitted `misc.py` cancellation repair byte-for-byte. Do not change
`ProcessMarkerStore`, process-group termination, startup hygiene, provider protocol schemas,
connection/routing/permission behavior, or public MCP response shapes unless objective execution
proves the plan wrong and the main model records an amendment.

## Objective verification

1. Focused spec test reproduces the exact state transition: confirmed dead worker, one removal
   `OSError`, typed first-close failure, retained marker, successful retry removal, and no second
   provider request.
2. Healthy control proves ordinary close removes the marker and succeeds.
3. Diagnostic provider-close failure control proves the pre-existing policy remains: when process
   termination and marker removal are both confirmed, close does not convert a diagnostic-only
   graceful-close failure into an operator failure.
4. Unconfirmed termination control still fails closed and retains the marker.
5. Regression tests cover startup/protocol invalidation callers and the already accepted managed
   wait cancellation diff without altering its behavior.
6. Both tester-recorded neutral commands pass in the same iteration; targeted lint, formatting,
   typing, and focused/adjacent tests are clean before manager acceptance.

## Exclusions

- No firmware, physical hardware, serial, SDK, fixture, experiment-evidence, documentation-only,
  or metadata-only change.
- No dependency update, global process/operation redesign, unrelated cleanup, formatting sweep,
  commit, push, deploy, flash, erase, unlock, or recovery action.
