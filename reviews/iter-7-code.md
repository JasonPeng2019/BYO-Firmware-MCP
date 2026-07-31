# Iteration 7 — Code Adversary

Scope: the codebase as it stands at HEAD (`088505a`), attacked fresh as a hostile reviewer
seeing it for the first time, per `reviews/REVIEW_POLICY.md`. Priority weight on the two
newest artifacts: `docs/architecture.md`'s new provenance paragraph and the D20 regression
test, both added in `088505a` and unreviewed until now. `reviews/ledger.md` (C1–C16,
D1–D22, M1–M6) and all prior iteration reports read first; nothing re-raised without new
grounds.

**Verification performed this round:** `uv run --locked ruff check src tests` → all
checks passed. `uv run --locked pyright src` → 0 errors. `PYTHONPATH=src python -m
unittest discover -s tests` → 665 passed, 7 skipped, OK (single run at the end; scoped
single-test runs used while iterating).

## Summary

| ID | Severity | One-line |
| --- | --- | --- |
| C18 | LOW (cross-reference to D23) | Reading `tests/test_hook_gating_and_budget.py`'s finalizer fixtures next to `kernel/finalizers.py::UARTWriteFinalizer` with no diff context, the field name mismatch (`"data"` vs the schema's `"text"`) is visible on inspection — same fact as D23 in `reviews/iter-7-diff.md`, independently reachable by a diff-blind reader. |

No other findings from a fresh read. `docs/architecture.md`'s new paragraph and the D20
test both hold up under cold, adversarial reading — see below for what was checked.

---

## Cold read: `docs/architecture.md`'s new provenance paragraph, as a diff-blind reader would encounter it

Read `docs/architecture.md:193-225` as a whole section, without reference to
`hardware_inventory.py`'s diff history, then cross-checked every factual claim against the
current source — the same claims are re-verified in `reviews/iter-7-diff.md`'s Target 1
table in more detail; not duplicated here. From the cold-read angle specifically (does the
prose stand on its own, is it internally consistent, does its terminology match the code a
reader would actually find):

- Every code identifier the paragraph names (`native`, `hook:<id>`, `vendor:<provider_id>`,
  `provenance`, `identity_scope`, `stable`, `session`, `SERIAL_FALLBACKS`,
  `PYOCD_SERIAL_FALLBACK_REGISTRY`) exists verbatim in `hardware_inventory.py`/
  `serial_resolver.py` — a reader grepping the source after reading the doc would find
  exactly what's described, not an approximation.
- The paragraph is self-contained: it doesn't lean on the reader already knowing the
  gating rule from the preceding paragraph (`:195-202`, hook-only), restating the "native
  wins completely" rule in its own terms rather than assuming context. This produces the
  redundancy noted in the diff report (not a numbered finding, pure style) but means the
  new paragraph is independently correct even read in isolation.
- Checked `docs/client-contract.md`, `SERVER_GUIDE.md`, `README.md` for any
  vendor-provenance claim that could now contradict the new `architecture.md` text:
  `grep -rln "vendor:" docs/ SERVER_GUIDE.md README.md` → only `docs/architecture.md`
  matches. No other document makes a claim about this mechanism that could now be
  inconsistent with it.

**No defect found.** Independently confirms the diff-report's Target 1 conclusion from a
different angle.

---

## Cold read: the D20 test and its neighbors in `BudgetTests`

Read `tests/test_hook_gating_and_budget.py:372-663` (the whole `BudgetTests` vendor/hook
section) top to bottom as a fresh reviewer, not just the one new test, checking for the
specific failure mode this task has produced three times before: a test whose assertions
can pass regardless of whether the code they name is correct.

- `test_read_serial_with_a_uart_finalizer_reserves_vendor_budget_twice`
  (`:614-644`) computes two independent deltas (plain action, action+finalizer) across
  the same before/after `SERIAL_FALLBACKS` toggle and asserts a strict 2:1 ratio between
  them — a shape that requires the finalizer's contribution to be *additively distinct*
  from the main action's, not just present. Confirmed load-bearing by direct break (see
  `reviews/iter-7-diff.md`).
- Found the field-name issue in its fixture (C18/D23) by simple inspection — it stood out
  once `finalizers.py`'s model was open side by side with the test, precisely the kind of
  thing a diff-only read (checking "does this test assert the right numbers") would not
  surface, since the numbers *are* right regardless.
- Checked whether the test's placement (immediately after
  `test_every_uart_action_gains_budget_from_configured_vendor_specs`, before
  `test_refresh_discovery_hooks_budget_unaffected_by_vendor_specs`) causes any ordering or
  shared-state issue: `BudgetTests.setUp`/`addCleanup` reset the hook-count provider per
  test (`:372-375`), and each test constructs its own `patch.object` context managers
  rather than relying on class-level state — no cross-test contamination possible
  regardless of unittest's (alphabetical, but not guaranteed-stable across versions)
  execution order.

**No defect found beyond C18/D23**, which does not change the test's validity for what it
asserts (confirmed by direct computation in the diff report).

---

## Independent sweep, diff-blind

Given the explicit instruction not to manufacture depth in territory already swept
multiple times, kept this brief and targeted at anything genuinely unexamined rather than
re-deriving prior conclusions:

- **`action_batch`'s recursive budget composition** (`kernel/operations.py:582-606`) —
  the one control-flow shape in `operation_timeout_seconds` not part of the six-site
  enumeration from the last two rounds. Traced by hand (see diff report); additive and
  correct, no double-count or gap.
- **`write_serial`+finalizer test coverage** — genuinely absent (D24), but the shared-
  closure structure of `include_finalizer` makes an independent per-tool bug unlikely,
  unlike the original D17 site multiplicity.
- **Ledger/doc cross-references** — `docs/architecture.md`'s new text and
  `reviews/ledger.md`'s D21 row tell the same story (including candidly describing the
  first-draft error), and both match what the code actually does. No drift between what
  the ledger claims was fixed and what is actually in the tree.

No further findings.
