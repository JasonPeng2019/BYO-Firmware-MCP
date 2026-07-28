# Change implementation plan

## Source change list

- Source: `.change-loop/fresh-suite/H05-uart-close/changes.md`
- Goal summary: Make all three backend-neutral UART helpers report close-only failures with
  actionable operation/device/baud context and preserve a primary I/O failure as the principal
  failure when close also fails, while retaining exactly-once close, existing bounds, explicit
  reopen behavior, healthy results, and every accepted H05 repair.

## Repository context and assumptions

- Verified architecture and relevant entry points:
  - `src/pyocd_debug_mcp/services/uart_capture.py` owns `capture_uart_output`,
    `write_uart_output`, and `exchange_uart_output`.
  - Each helper normalizes open/read/write/body exceptions inside `except Exception`, but calls
    `backend.close(port_handle)` unguarded in `finally`. Python therefore exposes a raw close
    exception and, when both fail, makes close top-level while the normalized primary error is
    only contextual.
  - The helpers are injected into serial/setup service boundaries from `server.py`; their
    result types and call signatures are public production behavior and do not need to change.
  - `tests/test_uart_capture_evidence.py` covers healthy exchange/capture evidence but not cleanup
    exception composition. No existing focused close-failure suite was found.
- Existing test/build commands relevant to the change:
  - `.venv/Scripts/ruff.exe check src tests`
  - `.venv/Scripts/ruff.exe format --check src tests`
  - `.venv/Scripts/pyright.exe src/pyocd_debug_mcp/services/uart_capture.py`
  - `.h01-venv-batchstrict/Scripts/python.exe -m unittest -q
    tests.test_uart_capture_evidence`
- <!-- Assumption: Python 3.10 exception composition uses the explicit, cycle-free object graph
  specified in CL-001 rather than ExceptionGroup. The traversal contract is depth-first
  top-level -> __cause__ before __context__, with an identity-based visited set. -->

## Plan items

### CL-001 — Compose UART body and close failures without losing either

- **What to change:** Introduce the smallest private cleanup/error-composition boundary in
  `uart_capture.py` and route the three helpers' opened-handle cleanup through it. Track the
  normalized primary operation exception, its traceback, and cancellation identity until close has
  been attempted exactly once. If close alone fails, raise the exact normalized UART close error
  defined below. If both fail, re-raise the same primary normalized exception object with its
  original traceback and the exact cycle-free causal graph below. Treat
  `OperationCancelledError` separately from ordinary UART failures so the exact cancellation
  object remains principal. If neither fails, return the existing result unchanged.
- **Where:** `src/pyocd_debug_mcp/services/uart_capture.py` only for production. Tester roles may
  add separate focused files under `tests/`; they must not modify existing H05 repair tests or
  production callers.
- **Exact intended behavior:**
  1. Open success always leads to exactly one close attempt, including early capture returns,
     cancellation, primary exceptions, and normal completion. Open failure performs no close.
  2. Use operation labels `read`, `write`, and `exchange` for capture, write, and exchange
     respectively. The normalized close exception is a `RuntimeError` whose exact text is:
     `Unable to close UART after {operation} on {device} at {baudrate} baud; handle cleanup is
     uncertain: {CloseType}: {close_message}`. In a close-only case it is top-level,
     `close_normalized.__cause__ is raw_close`, `close_normalized.__suppress_context__ is True`,
     and no retry/reopen occurs.
  3. Primary-only behavior is byte-for-byte compatible: the existing normalized `RuntimeError`
     remains top-level with its existing exact text and
     `primary_normalized.__cause__ is raw_primary`.
  4. For an ordinary primary plus close failure, the top-level object is the same
     `primary_normalized` object captured from the body and is re-raised with its original
     traceback. Its exact text is its existing primary-only text followed by
     `; additionally, UART close failed and handle cleanup is uncertain: {CloseType}:
     {close_message}`. The required graph is:
     `primary_normalized.__cause__ is raw_primary`;
     `raw_primary.__context__ is close_normalized`;
     `close_normalized.__cause__ is raw_close`.
     `close_normalized.__context__` and `raw_close.__context__` are explicitly `None` so the graph
     is cycle-free; both normalized wrappers suppress their implicit context. No additional
     wrapper is created.
  5. If `OperationCancelledError` occurs and close succeeds, re-raise the exact cancellation
     object unchanged, with its original text, traceback, cause, and context. If close also fails,
     that exact cancellation object remains top-level/principal with unchanged text, traceback,
     and cause; set `cancellation.__context__ is close_normalized`, with
     `close_normalized.__cause__ is raw_close`, and clear the close/raw-close contexts as in item
     4. Do not UART-normalize cancellation.
  6. Tests traverse exception objects depth-first in the order top-level, `__cause__`, then
     `__context__`, skipping identities already visited. Expected sequences are:
     primary-only `[primary_normalized, raw_primary]`; close-only
     `[close_normalized, raw_close]`; primary-plus-close
     `[primary_normalized, raw_primary, close_normalized, raw_close]`; cancellation-plus-close
     `[cancellation, close_normalized, raw_close]`. Assert object identity, exact text, and absence
     of cycles.
  7. Healthy capture/write/exchange return byte-for-byte equivalent result fields; capture's
     caller-requested reopen count/timing and early-return semantics are unchanged.
- **Must remain intact:** All function signatures and dataclasses; UARTInterface/provider
  neutrality; no real-port probing; one-open exchange semantics; explicit-only capture reopen;
  cancellation checkpoints and finite timing; ready/probe/follow-up behavior; byte accumulation;
  serial tool/server delegates; accepted `misc.wait` and `_WorkerClient.close` diffs. Add no retry,
  implicit reopen, new timeout, public exception class/API, dependency, or OS/board/device/provider
  branch.
- **Objective verification:**
  - Spec tests inject unique sentinel exception objects through fake adapters for close-only and
    primary-plus-close across capture, write, and exchange. They assert the exact strings, object
    identities, cause/context edges, suppression flags, cycle-free traversal sequences, original
    primary traceback, operation/device/baud context, open/close counts, and no retry/reopen.
  - Spec tests include expected-text and `max_bytes` early-return capture cleanup plus
    cancellation-only and cancellation-plus-close identity/ordering.
  - Regression tests cover healthy capture/write/exchange results, open/primary-only failures,
    explicit capture reopen, large capture/evidence behavior, and a serial service/delegate caller.
  - Neutral commands run only tester-owned files and both pass in one iteration; manager then runs
    focused/adjacent UART tests plus the two accepted H05 repair suites.

## Out of scope / must not change

- UART adapter implementations, serial tools, server callers, schemas, public result dataclasses,
  tool descriptions, setup/routing, physical-port selection, and dependencies.
- Retry/reopen/timeout policy beyond preserving current explicit behavior.
- Accepted H05 wait-cancellation and SWD marker-cleanup production/test slices.
- Firmware, experiment evidence, hardware, real serial, plans/permissions, docs, metadata, and
  unrelated dirty work.
- Existing contracts not named for change remain unchanged.
- No unrelated refactors, dependency upgrades, formatting sweeps, commits, or generated artifacts.

## Acceptance gate

- Every CL-NNN item has automated spec assertions.
- Regression coverage exercises the shared helper and one production delegate boundary.
- Both tester-recorded commands exit zero in the same neutral harness iteration.
- The doer does not modify tester-owned files, manifests, commands, or existing H05 tests.
- Every active role rereads `../.codex/design_charter.md` and records concrete checkpoints in
  `.change-loop/fresh-suite/H05-uart-close/DESIGN_CHARTER_CHECKS.md`.
