# Setup plan relocking bug

Fix only the setup-authorization bookkeeping defect where closing stale setup
allowance P1 can invalidate replacement PlanEngine plan P2 for the same board.

Required behavior:

1. SetupWorkflow allowance-close callbacks carry `board_id`, `allowance_id`,
   and `reason`.
2. `PlanEngine.complete_paired_plan()` accepts optional `expected_plan_id` and
   only consumes permission / invalidates when the active plan matches it.
3. Recheck the matching active plan after permission cleanup so a concurrently
   installed replacement is not invalidated.
4. Server setup allowance closure passes the closing allowance ID.
5. Setup tool wrappers pass the allowance/plan ID wherever already known.
6. Replacing incomplete P1 with P2 must leave P2 active and its paired
   `board_fix_setup` callable; closing matching P1 must still relock normally.
7. Cover the full external-UART continuation route and replacement-during-
   completion race with automated tests.
8. Scope the adjacent loader allowance and continuation cleanup by the same
   expected allowance identity. Reorder binding if necessary so synchronous P1
   retirement clears P1 state before P2 becomes the loader's current allowance.
   A late P1 callback/wrapper must not clear P2's loader allowance, accepted
   selections, or continuation facts.

Follow `../.codex/design_charter.md`: use identity-based correctness rather
than broad guards, preserve one-time permission boundaries, keep the change
general and simple, and do not introduce board-, port-, or OS-specific logic.

The broader limitation around multiple sequential repair passes is explicitly
out of scope.

Preserve all unrelated pre-existing working-tree changes, including the earlier
UTF-8 probe-inventory fix. Do not edit or restore deleted `testing_folder`
content; a fresh hardware-only `testing_folder` will be created after the
software change loop is green.
