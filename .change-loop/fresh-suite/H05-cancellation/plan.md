# Change implementation plan

## Source change list

- Source: `/mnt/c/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP/.change-loop/fresh-suite/H05-cancellation/changes.md`
- Goal summary: Make the host-only `wait` tool cooperate with the server's existing
  managed-operation cancellation signal so a cancelled wait stops promptly, never records a false
  success, and releases same-board serialization for the next request, while preserving ordinary
  wait validation, timing, event, text, timeout, and direct-unit behavior.

## Repository context and assumptions

- Verified architecture and relevant entry points:
  - `src/pyocd_debug_mcp/tools/misc.py::build_misc_handlers` owns the synchronous `wait`
    implementation. It validates `ms`, blocks through the injected `MiscToolServices.sleep`, writes
    the success/refusal event, and returns the existing Layer-2 text.
  - `src/pyocd_debug_mcp/server.py` constructs those handlers, registers `wait` as a Layer-2
    action, and therefore routes every public call through
    `kernel.registry.RegistryFastMCP.call_tool`.
  - `src/pyocd_debug_mcp/kernel/registry.py::RegistryFastMCP.call_tool` passes the MCP request ID,
    board ID, and synchronous handler to `kernel.operations.dispatch`.
  - `src/pyocd_debug_mcp/kernel/operations.py::dispatch` runs synchronous actions in the
    board-serialized worker, exposes that `ManagedOperation` through `current_operation()`, sets
    its `cancellation_requested` `threading.Event` when the MCP task is cancelled, and already
    supplies `run_if_not_cancelled` for an atomic short commit.
  - `operation_timeout_seconds("wait", ...)` already derives the finite operation timeout from the
    requested duration; this repair does not change timeout policy.
- Existing test/build commands relevant to the change:
  - Baseline verified green:
    `./.h01-venv-batchstrict/Scripts/python.exe -m unittest -q
    tests.test_server_trust_model_round_1 tests.test_h01_strict_mcp_boundary`
    (`18` tests, exit `0`).
  - `tests/test_server_trust_model_round_1.py` contains the existing direct wait validation and
    injectable-sleep regression.
  - `tests/test_h01_strict_mcp_boundary.py` contains host-only registered FastMCP boundary helpers
    and adjacent dispatch behavior.
- <!-- Assumption: The MCP SDK's `{code: 0, message: "Request cancelled"}` response is
  dependency-owned and accepted for this repair. MCP 2025-03-26 prefers no response and does not
  define `-32800`; production scope is prompt work/resource cancellation, not response rewriting. -->

## Plan items

### CL-001 — Make `wait` cancellation-aware without polling or false success

- **What to change:** In the synchronous `wait` handler, retain the existing direct-call sleep
  seam when no managed operation exists. During managed dispatch, block on the current
  operation's existing `cancellation_requested` event for at most the requested duration instead
  of calling an uninterruptible `time.sleep`. After either timeout or wake-up, use the existing
  managed-operation checkpoint and short atomic commit mechanism so cancellation before the
  commit raises the existing `OperationCancelledError`, no success event/text is produced, and a
  cancellation racing after the completed duration cannot retroactively turn a committed success
  into failure. Do not add a polling interval or new timeout/limit.
- **Where:** Production change only in `src/pyocd_debug_mcp/tools/misc.py`, reusing
  `current_operation` and `run_if_not_cancelled`/the equivalent existing operation methods from
  `src/pyocd_debug_mcp/kernel/operations.py`. Tester-owned focused coverage may be added under
  `tests/`; the implementation doer must not edit tests.
- **Exact intended behavior:**
  1. `wait(board_id, 5000)` under managed dispatch wakes when its exact MCP request is cancelled,
     raises through the existing cancellation path, records no `ToolOutcome.SUCCESS`, emits no
     tool-success text, and leaves no active operation.
  2. The cancelled worker releases same-board serialization within
     `CANCELLATION_CLEANUP_GRACE_SECONDS + 0.25` seconds. A same-transport, same-board `50 ms`
     follow-up wait completes within that cleanup allowance plus `50 ms` and `0.25` seconds,
     rather than after the original five seconds.
  3. An uncancelled managed wait blocks for its requested positive duration, records exactly one
     success event, commits once, and returns the byte-for-byte existing
     `Waited {ms} ms for board '{board_id}'.` Layer-2 response.
  4. A cancellation that wins before the short success commit produces cancellation and no
     success event. A success commit that wins after the requested duration remains success; this
     is the protocol's ordinary completion/cancellation race.
  5. Direct handler invocation outside managed dispatch calls the injected `sleep` exactly once
     with `ms / 1000.0`, preserving deterministic unit use. Invalid boolean, non-integer, zero,
     and negative values keep the existing refusal event/code/text and do not sleep.
- **Must remain intact:** Public tool name/schema/description; positive-duration support including
  values above former limits; argument-derived finite timeout; board isolation and lock ordering;
  Layer-2 safe-exit wrapping; event fields and runtime/session lookup; all hardware, connection,
  plan, permission, UART, provider, cleanup, and MCP initialization behavior; cross-platform
  operation with no OS or board constants. Do not alter the SDK cancellation response, dependency
  versions, or server-wide dispatch semantics.
- **Objective verification:**
  - A focused unit test runs the real `dispatch` plus real `wait` handler, waits until the operation
    is active, cancels by request ID, and asserts bounded completion, cancellation type, zero
    success events, empty `OperationManager.snapshots()`, and immediate same-board follow-up.
  - Direct-handler regressions assert exactly one injected sleep and one success event for a valid
    wait, and the complete invalid-input refusal matrix with no sleep.
  - A public stdio subprocess test performs initialize/initialized/handshake/list, sends a
    five-second `wait`, sends `notifications/cancelled`, immediately sends the same-board `50 ms`
    wait, and asserts the first has no tool success, the second succeeds inside the corrected
    bound, stdout remains JSON-RPC-only, the server stays usable, and EOF exits cleanly with no
    helper/operation leak.
  - Focused and adjacent tester commands must use the repository's existing
    `.h01-venv-batchstrict` runtime and exit zero under the neutral gate.

## Out of scope / must not change

- MCP SDK response-code behavior, dependency pins/upgrades, and protocol vendoring or monkeypatches.
- Global managed-operation redesign, polling loops, arbitrary timing constants, and new frameworks.
- Hardware, firmware, fixtures, experiment evidence, board profiles, routing, gates, plans,
  permissions, connection cleanup, UART, provider processes, generated distributions, or docs.
- Existing contracts not named for change remain unchanged.
- No unrelated refactors, dependency upgrades, formatting sweeps, commits, or generated artifacts.

## Acceptance gate

- Every CL-NNN item has at least one automated spec assertion.
- Regression coverage exercises callers, shared modules, and adjacent behavior touched by the diff.
- Both tester-recorded commands exit 0 in the same neutral harness iteration.
- The doer does not modify tester-owned files, manifests, or gate commands.
