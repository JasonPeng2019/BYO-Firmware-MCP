# Change implementation plan

## Source change list

- Source: `.change-loop/changes.md`
- Goal summary: Prevent a stale setup allowance from consuming or clearing the active replacement setup plan and its loader/continuation state, while retaining the existing one-time paired setup authorization lifecycle for the matching allowance.

## Repository context and assumptions

- Verified architecture and relevant entry points: `SetupWorkflow` owns per-board allowance identities and invokes its allowance-close callback from `_close_allowance_locked` in `src/pyocd_debug_mcp/setup_flow/setup.py`; `PlanEngine` owns active `board_setup` plans and permission consumption in `src/pyocd_debug_mcp/guardrails/plan_engine.py`; server-global continuation facts and the workflow callback are in `src/pyocd_debug_mcp/server.py`; `SetupToolLoadState` binds the currently callable setup wrapper to an allowance in `src/pyocd_debug_mcp/tools/setup.py`. `tests/test_server_trust_model_round_1.py` currently exercises workflow replacement, and `tests/test_validation_honesty.py` supplies setup-handler test fixtures.
- Existing test/build commands relevant to the change: `python -m unittest discover -s tests` (test suite convention verified from the repository's `unittest` test modules); `ruff check src tests`; `pyright src` (both static tools are declared in `pyproject.toml`).

## Plan items

### CL-001 — Carry the closed allowance identity through the workflow callback

- **What to change:** Change the `AllowanceClosed` callback contract and all `SetupWorkflow` invocations/default callback so a close notification receives `(board_id, allowance_id, reason)`. Preserve close-once behavior and emit the allowance's own normalized ID when a plan is replaced, cancelled, disconnected, revoked, completes, or terminates.
- **Where:** `src/pyocd_debug_mcp/setup_flow/setup.py` (`AllowanceClosed`, `SetupWorkflow.__init__`, and `_close_allowance_locked`), plus direct callback test construction in `tests/test_server_trust_model_round_1.py`.
- **Exact intended behavior:** When P1 is closed, its observer receives P1's board ID, P1's allowance ID, and P1's reason even if a newer P2 exists or is about to be installed for that board. A callback is not emitted for unknown or already-closed allowances.
- **Must remain intact:** Allowance IDs remain non-empty and unique; only one current allowance is tracked per board; replacement retires P1 before P2 is registered as current; all existing closure reasons and workflow state transitions remain available. No callback exposes an allowance as a new authorization source.
- **Objective verification:** Automated workflow tests record callback arguments and assert the exact three-tuple for replacement and for a terminal close, assert P1 is closed after replacement, and assert a repeated close produces no additional callback.

### CL-002 — Make paired-plan completion compare-and-close by plan identity

- **What to change:** Add optional `expected_plan_id` to `PlanEngine.complete_paired_plan()`. Under the plan-engine lock, select an active state only when it matches that expected ID (when supplied); otherwise return without consuming permission or invalidating anything. After releasing the lock for permission cleanup, reacquire it and invalidate only if the same expected/matched plan is still active; do not invalidate a concurrently installed replacement.
- **Where:** `src/pyocd_debug_mcp/guardrails/plan_engine.py` (`complete_paired_plan`) and focused plan-engine regression coverage in the setup-adjacent `unittest` tests.
- **Exact intended behavior:** `complete_paired_plan("board_setup", board, reason, expected_plan_id="P1")` is a no-op against active P2: P2 remains active, its paired `board_fix_setup` allowance remains callable, and P2's authorization is not consumed. For matching P1, completion retains current behavior: consume one-time permission where applicable, then relock/invalidate P1. If permission cleanup allows P2 to be installed in between, the final invalidation must not remove P2.
- **Must remain intact:** Existing three-argument callers continue to close the currently active paired plan; full-session permission consumption remains its provider-defined no-op; `PolicyRefusal` during cleanup remains tolerated while closure relocks the matching plan; unrelated plan actions and invalidation APIs retain their present behavior.
- **Objective verification:** Tests submit P1 then P2 for one board and prove expected-P1 completion leaves P2 active; prove expected-P1 completion closes a matching P1 and consumes its one-time permission exactly once; use a controllable permission provider/thread synchronization to install P2 during P1 permission cleanup and assert P2 is still active after `complete_paired_plan` returns.

### CL-003 — Scope server and loader cleanup to the allowance being retired

- **What to change:** Make the server's allowance-close handler accept the allowance ID and pass it to identity-aware paired-plan completion, loader cleanup, and continuation cleanup. Extend `SetupToolLoadState.clear_allowance` and the server continuation-cleanup boundary with an optional expected allowance identity so they remove per-board state only when the recorded/current allowance equals that identity. Wire the server's workflow callback and revocation/disconnect paths so broad lifecycle revocation can still deliberately clear current state, while a late P1 close cannot clear P2 state.
- **Where:** `src/pyocd_debug_mcp/server.py` (`_close_setup_allowance`, `_clear_setup_continuation`, `_revoke_with_setup_closure`, and existing disconnect/assignment cleanup callers) and `src/pyocd_debug_mcp/tools/setup.py` (`SetupToolLoadState.clear_allowance` and service callback typing).
- **Exact intended behavior:** Closing P1 for a board with current P2 attempts cleanup only for P1 and leaves P2's loader allowance, accepted selections, target/attachment overrides, candidate/pipeline facts, and research/continuation facts intact. Closing the matching current allowance removes those run-scoped facts and relocks its matching plan exactly as today. Explicit board-wide revocation/disconnect still removes all current setup state for that board.
- **Must remain intact:** Continuation data stays board-scoped and is cleared at matching terminal closure; loader state remains run-scoped and thread-safe; no stale continuation can grant authority; ordinary disconnect, assignment replacement, and permission revocation continue to close setup state. No board, UART-port, or OS special case is introduced.
- **Objective verification:** Automated server/loader tests seed P2 loader and continuation facts, invoke the P1 close callback, and assert every P2 fact remains; invoke matching P2 closure and assert loader allowance plus all continuation facts are removed; retain assertions that board-wide revoke/disconnect clears setup state.

### CL-004 — Pass known allowance identities through setup wrappers and preserve the external-UART continuation route

<!-- Assumption: Place the new handler-level regressions in the existing setup-adjacent `unittest` modules when their fixtures fit; otherwise use one focused new `unittest` module. Test placement does not change production behavior. -->

- **What to change:** In `build_setup_handlers`, use the active plan ID/loaded allowance ID as `expected_plan_id` and expected cleanup identity whenever `board_setup` completes synchronously or `board_fix_setup` finishes. Order the initial wrapper binding so synchronous P1 retirement/close cleanup runs before P2 becomes the loader's current allowance; then bind P2 only after `begin_plan` succeeds. Thread the same expected identity into continuation clearing.
- **Where:** `src/pyocd_debug_mcp/tools/setup.py` (`board_setup` and `board_fix_setup` handlers and `SetupToolServices` callback surface), with integration coverage using the existing handler fixtures in `tests/test_validation_honesty.py` and/or a focused new `unittest` module.
- **Exact intended behavior:** A replacement P2 started after incomplete P1 remains the loader's allowance and can call `board_fix_setup`; a late P1 wrapper/close callback cannot clear P2 or consume P2's plan. The complete external-UART route—initial `board_setup` returns the external-adapter confirmation continuation, `continue_setup` accepts the confirmation and records selections, then `board_fix_setup` runs under the same active allowance—continues to work, including when P1 is replaced by P2 before the repair call.
- **Must remain intact:** `board_setup` still requires an active setup plan and preserves its existing blocked response; `board_fix_setup` still returns its existing blocked response when no allowance is loaded, runs only once per allowance, and consumes/relocks the matching paired plan after completion; continuation acceptance still grants no permission and uses the established friendly external-adapter confirmation contract.
- **Objective verification:** A handler-level test drives the full external-adapter confirmation response through `board_setup` → `continue_setup` → `board_fix_setup` and asserts the repair call is accepted under the active paired allowance. A replacement regression creates incomplete P1, installs P2, triggers P1's stale callback/wrapper completion, then asserts loader `allowance_for(board)` is P2, P2 continuation selections/facts remain, `active_plan("board_setup", board)` is P2, and `board_fix_setup` is callable; a matching P1 close control case asserts normal relock/cleanup.

## Out of scope / must not change

- Multiple sequential repair passes beyond the existing one paired `board_fix_setup` allowance per plan.
- Any board-, serial-port-, external-adapter-vendor-, or OS-specific behavior.
- The earlier UTF-8 probe-inventory working-tree change and all unrelated pre-existing changes.
- Deleted `testing_folder` content; do not restore, edit, or regenerate it during this software change loop.
- Existing contracts not named for change remain unchanged.
- No unrelated refactors, dependency upgrades, formatting sweeps, commits, or generated artifacts.

## Acceptance gate

- Every CL-NNN item has at least one automated spec assertion.
- Regression coverage exercises callers, shared modules, and adjacent behavior touched by the diff.
- Both tester-recorded commands exit 0 in the same neutral harness iteration.
- The doer does not modify tester-owned files, manifests, or gate commands.
