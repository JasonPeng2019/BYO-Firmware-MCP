# Change implementation plan

## Source change list

- Source: `/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP/.change-loop/fresh-suite/H05-marker-unlink/changes.md`
- Goal summary: Make `_WorkerClient.close` truthfully propagate its existing typed recovery-marker
  removal failure when process cleanup is confirmed but the marker remains, while preserving the
  current diagnostic-only treatment of graceful provider-close failures after complete cleanup,
  fail-closed unconfirmed termination, and marker-only retry semantics.

## Repository context and assumptions

- Verified architecture and relevant entry points:
  - `src/pyocd_debug_mcp/adapters/swd_process.py::_WorkerClient.call` serializes provider
    requests. Write/read/protocol failures route through `_invalidate`.
  - `_WorkerClient._invalidate` marks the client closed, invokes `_terminate`, and, when
    termination is confirmed, calls `_remove_confirmed_marker`. That helper already converts a
    marker-removal `OSError` into an actionable `TargetConnectionError`, retains `_marker`, and
    explicitly says close may retry it.
  - `_WorkerClient.close` first retries marker removal for an already-closed, cleanup-confirmed
    client. For a live client it treats `call("close")` failures as diagnostic, then owns final
    process termination and marker removal.
  - The reproduced defect occurs when `call("close")` itself invalidates the dead worker:
    `_invalidate` sets `_closed=True` and `_cleanup_confirmed=True`, marker removal raises, and
    `close` catches that actionable exception in its graceful-close diagnostic handler. Its
    `finally` then skips cleanup because the client is already closed, and the final
    `_cleanup_confirmed` check returns success even though `_marker` remains.
  - `ProcessIsolatedSWDInterface.close` delegates directly to `_WorkerClient.close`.
    `ProcessIsolatedSWDInterface._open` invokes the same close path for rollback while preserving
    the original open exception as primary.
  - `tests/test_swd_process_isolation.py` already covers direct `_invalidate` marker-removal
    failure/retry, open rollback, unconfirmed cleanup, exhausted/racing close deadlines, and
    confirmed termination. It does not cover the nested `call("close") -> _invalidate ->
    marker-removal failure` path that is currently swallowed.
- Existing test/build commands relevant to the change:
  - Current dirty baseline:
    `./.h01-venv-batchstrict/Scripts/python.exe -m unittest -q
    tests.test_swd_process_isolation tests.test_process_cleanup
    tests.test_h05_wait_cancellation_spec tests.test_regression_h05_wait_cancellation`
    ran `41` tests successfully before implementation.
  - Main independent reproduction:
    `.change-loop/fresh-suite/H05-marker-unlink/main_verification/fault/result.json`
    records one removal call, no first-close exception, a retained marker, successful marker-only
    retry, and one provider request. The matching healthy control is
    `.change-loop/fresh-suite/H05-marker-unlink/main_verification/healthy/result.json`.
  - The accepted pre-existing `src/pyocd_debug_mcp/tools/misc.py` cancellation repair and its two
    tester-owned H05 wait suites are part of the baseline and must remain unchanged.
- <!-- Assumption: A graceful provider-close failure remains diagnostic-only exactly when close
  can subsequently prove both process cleanup and marker removal. If `call("close")` has already
  confirmed termination but retains `_marker` because the existing removal helper raised, that
  exception represents incomplete ownership cleanup and must escape rather than be suppressed. -->

## Plan items

### CL-001 — Propagate retained-marker cleanup failure from the nested close path

- **What to change:** Narrow the diagnostic suppression in `_WorkerClient.close`. When the
  caught `call("close")` exception has left process cleanup confirmed but `_marker` still present,
  re-raise that existing exception before the method can report success. Otherwise preserve the
  current flow: diagnostic graceful-close failures continue to final termination/removal;
  successful invalidation with marker removal remains a successful close; unconfirmed termination
  reaches the existing fail-closed error; and an already-closed cleanup-confirmed client retries
  only `_remove_confirmed_marker`.
- **Where:** Production change only in
  `src/pyocd_debug_mcp/adapters/swd_process.py::_WorkerClient.close`. Reuse the existing
  `_cleanup_confirmed`, `_marker`, `_remove_confirmed_marker`, and `TargetConnectionError`
  contracts. The implementation doer must not edit tests, manifests, commands, the accepted
  `tools/misc.py` diff, or any other production module. Tester-owned focused coverage may be added
  under separate files in `tests/`.
- **Exact intended behavior:**
  1. If `call("close")` encounters a dead/malformed worker, `_invalidate` confirms termination,
     and `ProcessMarkerStore.remove` raises `OSError`, the first `close` raises the existing
     `TargetConnectionError`. Its text includes recovery-marker removal failure, the original
     `OSError` type/message, retained-marker status, and retry guidance; its direct cause remains
     the `OSError`.
  2. After that failure, `_closed is True`, `_cleanup_confirmed is True`, and `_marker` remains the
     original marker. A second `close` after normal removal is restored calls marker removal once,
     clears `_marker`, succeeds, and performs no provider call or process termination again.
  3. A healthy worker close still sends at most one graceful close request, confirms final process
     cleanup, removes the marker exactly once, and returns success.
  4. If a graceful provider-close request returns a typed provider/protocol error while the client
     has not already completed invalidation, that error remains diagnostic-only when the final
     termination and marker removal both succeed. The repair must not make all graceful-close
     failures fatal.
  5. If invalidation already confirmed termination and removed the marker before raising its
     ordinary "worker was terminated" error, outer close remains successful because ownership
     cleanup is complete.
  6. If termination is unconfirmed, close raises the existing actionable
     `TargetConnectionError` and retains the marker. A marker-removal attempt is not made.
  7. Expired/default/racing deadlines continue to force bounded final termination and marker
     removal according to the existing contract.
- **Must remain intact:** `_WorkerClient` locking, request IDs, provider JSON framing and result
  validation, operation deadlines, process-group cleanup authority, marker schema/root/startup
  hygiene, direct `_invalidate` behavior, open-rollback exception chaining, backend public
  interfaces, board/provider isolation, and all connection/permission/routing behavior. Preserve
  the accepted `misc.wait` cancellation diff byte-for-byte. Add no retry loop, timeout, polling
  interval, path fallback, environment-specific constant, board/provider case, or new abstraction.
- **Objective verification:**
  - A focused spec test deterministically drives `close -> call -> _invalidate` with one confirmed
    termination and a one-time marker-removal `OSError`; asserts the exact typed/cause/message and
    retained state after the first close; restores removal; then asserts a successful marker-only
    second close with no second provider request or termination.
  - The same spec suite includes a healthy close control and an invalidation-complete control whose
    marker was removed, proving the repair does not turn ordinary complete cleanup into failure.
  - An independent regression suite drives a diagnostic provider-close error followed by
    successful termination/removal and asserts close still succeeds; separately verifies
    unconfirmed termination stays fail-closed with no removal; and exercises the backend delegate
    so the fixed error reaches its production caller.
  - The neutral tester commands run only their owned focused files and both exit zero in the same
    iteration.
  - Manager follow-up runs targeted Ruff check/format, Pyright on the changed source, both neutral
    commands, and the existing adjacent `test_swd_process_isolation`, `test_process_cleanup`, and
    two H05 cancellation suites.

## Out of scope / must not change

- `ProcessMarkerStore`, `kernel/processes.py`, startup hygiene, process-group termination, provider
  worker protocol, marker schema, retry policy, timeout policy, SDK/dependencies, and public MCP
  response shapes.
- The accepted uncommitted `src/pyocd_debug_mcp/tools/misc.py` cancellation repair and its
  tester-owned tests.
- Firmware, fixtures, experiment evidence, physical hardware/serial, boards, routing, gates,
  plans, permissions, connections, documentation, metadata, and generated artifacts.
- Existing contracts not named for change remain unchanged.
- No unrelated refactors, dependency upgrades, formatting sweeps, commits, or generated artifacts.

## Acceptance gate

- Every CL-NNN item has at least one automated spec assertion.
- Regression coverage exercises callers, shared modules, and adjacent behavior touched by the diff.
- Both tester-recorded commands exit 0 in the same neutral harness iteration.
- The doer does not modify tester-owned files, manifests, or gate commands.
- Before implementation/testing and before verification/acceptance, every active repair role
  rereads `../.codex/design_charter.md` and appends a concrete checkpoint to
  `.change-loop/fresh-suite/H05-marker-unlink/DESIGN_CHARTER_CHECKS.md`.
