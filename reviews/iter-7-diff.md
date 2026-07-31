# Iteration 7 — Diff Adversary

Scope: `git diff 6f3da0a..HEAD` in full, fresh, per `reviews/REVIEW_POLICY.md`. Priority
weight on the two newest artifacts named by the coordinator — `docs/architecture.md`'s new
provenance paragraph (one false claim already caught there in the first draft) and the
D20 regression test (the newest test in the tree) — since prose has no test to catch it
and a test proving its own guarded behavior is this task's most reliable defect
signature. `reviews/ledger.md` (C1–C16, D1–D22, M1–M6), all six prior iteration reports,
and `reviews/narrow-d15-m6.md` read in full first. Nothing already ruled INVALID/
EXTRANEOUS is re-raised without new grounds.

**Verification performed:** `uv run --locked ruff check src tests` → all checks passed.
`uv run --locked pyright src` → 0 errors. `PYTHONPATH=src python -m unittest discover -s
tests` → **665 passed, 7 skipped, OK** (single run at the end; targeted single-test/
single-class runs used while iterating, per the policy). `git status --short` clean
before and after every experimental break (each reverted via `Edit`, confirmed via
`git diff`/`git status`, never `git checkout`/`restore`/`stash`).

## Summary

| ID | Severity | One-line |
| --- | --- | --- |
| D23 | LOW (does not undermine test validity) | The `on_exit` finalizer fixture used by both the pre-existing hook-budget test and the new D20 vendor-budget test (`{"action": "uart_write", "data": "q", "timeout_seconds": 2}`) has the wrong field name — `UARTWriteFinalizer` requires `text`, not `data` — so both tests exercise the malformed-input fallback branch of `operation_timeout_seconds`'s finalizer detection, never the well-formed `isinstance(finalizer, UARTWriteFinalizer)` branch. Proven not to change the outcome (identical budget numbers either way), so the tests remain valid for what they assert; flagged because neither branch has ever been exercised by a *well-formed* finalizer in this specific doubled-budget scenario. |
| D24 | LOW, likely EXTRANEOUS | Only `read_serial` has a dedicated finalizer-budget-doubling test (both the hook one and the new D20 vendor one); `write_serial` — the only other `ELIGIBLE_FINALIZER_TOOLS` member — has none. Lower-risk than it sounds because `include_finalizer` is one shared closure, not duplicated per tool (unlike the original D17 gap, which *was* duplicated across three separate call sites), so a read_serial-specific pass is a reasonable proxy. Reported per policy rather than pre-filtered. |

**Target 1** (`docs/architecture.md`'s new paragraph) checked line by line against
`hardware_inventory.py` and came back **clean** — no false claim found, unlike the first
draft. **Target 2** (the D20 test) is **confirmed load-bearing** by breaking the exact
guarded behavior and observing the failure, reproducing the coordinator's own `31.5 !=
63.0` result independently. Details below.

---

## Target 1 — `docs/architecture.md`'s new provenance paragraph, verified line by line

The paragraph (`docs/architecture.md:211-224`) was checked sentence by sentence against
the current `hardware_inventory.py`, specifically `_collect_uart_rows`
(`:394-428`) and `_uart_scope` (`:1041-1053`), rather than trusted because the first draft
was already shown to misdescribe this exact code.

| Claim | Checked against | Verdict |
| --- | --- | --- |
| "UART discovery combines three provenance sources: `native`, `hook:<id>`, `vendor:<provider_id>`" | `NATIVE_PROVENANCE = "native"` (`:64`), `f"hook:{execution.hook_id}"` (`:542,567`), `f"vendor:{self.provider_id}"` (`:182`) | True — exact string formats match. |
| "identity_scope of either `stable` (has usb_serial and valid vid/pid) or `session`" | `_uart_scope:1048-1053` — `session` if `usb_serial` falsy, else `session` if either of `vid`/`pid` is not an `int` in `[0, 0xFFFF]`, else `stable` | True. |
| "native enumeration wins completely... hooks and vendor rows are each consulted only when native UART discovery returns empty" | `run_uart_hooks = bool(uart_rows) is False and ...` (`:413`) and `if not uart_rows:` (`:415`), both keyed on the *same* native-only `uart_rows` computed at `:408`, before either source runs | True — any native row at all (not merely "some minimum count") suppresses both. |
| "They are gated independently of one another, not in sequence -- vendor rows... are merged in *before* hooks run, and the hook-run decision is evaluated against the native rows alone" | `run_uart_hooks` (`:413`) is computed and captured into a local **before** the vendor merge block (`:415-419`) runs, and is not recomputed afterward; the vendor block executes before the `if run_uart_hooks:` block (`:421-426`) in source order | True on both counts — this is exactly what the coordinator's correction demanded, and it is now stated correctly: hook execution is decided from native-only state captured *prior to* the vendor merge, and vendor rows are merged first in execution order. |
| "when native discovery is empty and both a vendor helper and a UART hook are configured, rows from both sources can appear in the same snapshot, including two rows describing one physical port" | Confirmed by tracing the two independent gates above — both can fire in the same call when native is empty, and `_merge_uart_rows`'s session-local dedup (`_matching_uart_index:1067-1083`) requires matching `provenance`, which a vendor row and a hook row never share | True — this is the same fact recorded as D22 (ruled EXTRANEOUS last round); the doc now discloses it accurately rather than implying it can't happen. |
| "A hook or vendor row can never mask or override a natively discovered port" | Direct consequence of the gate: neither source's collection code runs at all while native rows exist | True. |
| "Vendor rows are always `session`-scoped (they carry no usb_serial or vid/pid identity)" | `_nrfjprog_vendor_rows`/`_stlink_vendor_rows` (`:191-210`) never pass `usb_serial`/`vid`/`pid` to `VendorUartRow`, which defaults all three to `None`; `_uart_scope(None, None, None)` returns `"session"` unconditionally | True. |
| "retained for one release for compatibility; the feature is slated for deprecation once `PYOCD_SERIAL_FALLBACK_REGISTRY` has been removed from production deployments" | Forward statement of intent, not a code-behavior claim — matches the guide's own "Keep `PYOCD_SERIAL_FALLBACK_REGISTRY` working for one release... deprecate only after migration tests pass" | Not independently falsifiable against source; consistent with the guide's stated intent, not contradicted by anything in the code. |

**Every checkable claim in the paragraph is accurate.** This is a genuinely clean result
for the target the coordinator specifically flagged as highest priority — the correction
holds.

One structural (not factual) observation, not filed as a numbered finding because it is
pure organization/style, which the policy explicitly directs effort away from: the
paragraph immediately above the new one (`docs/architecture.md:195-202`, pre-existing,
unchanged by this diff) already covers the hook-only half of the same gating rule ("a
kind's hooks execute only when that kind's native result is empty... A hook therefore can
never mask or outrank a natively visible device"). The new paragraph restates this for
hooks while extending it to vendor rows, producing some redundancy between the two
paragraphs. Not wrong, just slightly repetitive; a documentation editor's call, not a
review finding.

---

## Target 2 — the D20 test, attacked for whether it can pass for the wrong reason

Re-derived the coordinator's `31.5 != 63.0` result independently before trusting it, then
went further: checked whether the test's own arithmetic could be fooled by a class of bug
*other* than "the exact line removed."

**Re-proved the primary claim.** Temporarily removed `_vendor_uart_budget()` from
`include_finalizer`'s `finalizer_reaches_uart` branch (`kernel/operations.py:579`, leaving
only `_hook_budget("uart")`) and ran the single test:
```
python -m unittest tests.test_hook_gating_and_budget.BudgetTests.test_read_serial_with_a_uart_finalizer_reserves_vendor_budget_twice
```
Failed: `AssertionError: 31.5 != 63.0 within 6 places`. Reverted the one line; `git diff`
confirmed clean.

**Traced the arithmetic to rule out "passes for another reason."** Worked through
`operation_timeout_seconds`'s `read_serial` branch by hand: the main action's own
`_vendor_uart_budget()` term (`:656`) and `include_finalizer`'s second, independent term
(`:579`) are additive and structurally separate — one is baked into the value passed *into*
`include_finalizer`, the other is added *inside* it only when `finalizer_reaches_uart`.
Confirmed the test's `plain_arguments` control (no `on_exit`) isolates the first term
alone (`delta_plain == ONE_VENDOR_SPEC`, verified) so the doubling assertion
(`delta_finalizer == 2 * ONE_VENDOR_SPEC`) cannot be satisfied by a bug that scales the
*single* term instead of genuinely adding a second one — tried this directly: if
`_vendor_uart_budget()` returned a wrong-but-doubled value unconditionally, `delta_plain`
would then equal `2 * ONE_VENDOR_SPEC` instead of `1x`, and the test's second assertion
(`delta_plain == ONE_VENDOR_SPEC`) would independently catch that, which was verified for
real in iteration 6's D19 investigation (a `+5.0` unconditional leak) and holds by the same
reasoning here.

**Confirmed `ONE_VENDOR_SPEC` is not an independently-wrong expectation.** It is imported
from the production module (`ops.DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS +
ops.MAX_OWNED_PROCESS_CLEANUP_SECONDS`), not a hardcoded literal — a constant change in
production would move the test's expectation in lockstep, not create a stale-expectation
gap.

**Confirmed `BudgetTests.setUp`/`addCleanup` correctly reset the hook-count provider**
(`ops.reset_eligible_hook_count_provider()`) before and after every test in the class, so
`_hook_budget("uart")` is 0.0 throughout — the measured deltas are attributable to the
vendor term alone, not contaminated by hook-budget state leaking from another test.

**The test genuinely guards what it names, with the one caveat below (D23).**

---

## D23 — LOW (does not undermine what the test proves) — the finalizer fixture's `"data"` field is not the schema's `"text"` field, so the doubling tests only ever exercise the fallback branch

**Files:** `tests/test_hook_gating_and_budget.py:625` (new, D20's test) and `:424` (the
pre-existing `test_read_serial_with_a_uart_finalizer_reserves_both_hook_budgets`, part of
step 9's original implementation, in scope as part of `6f3da0a..HEAD`).
`src/pyocd_debug_mcp/kernel/finalizers.py:12-17` (`UARTWriteFinalizer`).

Both tests build `on_exit` as `{"action": "uart_write", "data": "q", "timeout_seconds": 2}`.
`UARTWriteFinalizer` (`finalizers.py:12-17`) is a `strict=True, extra="forbid"` pydantic
model requiring a field named `text`, not `data`. Verified directly:

```python
parse_finalizer("read_serial", {"action": "uart_write", "data": "q", "timeout_seconds": 2})
# -> raises FinalizerValidationError: "Invalid on_exit finalizer; only structured
#    uart_write and reset_and_run are allowed."
parse_finalizer("read_serial", {"action": "uart_write", "text": "q", "timeout_seconds": 2})
# -> UARTWriteFinalizer(action='uart_write', text='q', timeout_seconds=2.0)
```

So in `operation_timeout_seconds` (`kernel/operations.py:548-566`), both tests' fixtures
take the `except FinalizerValidationError:` branch (`:550-562`) — the deliberate
defensive fallback for malformed-but-uart-shaped input — never the `else:` branch
(`:563-566`) that handles a genuinely well-formed `UARTWriteFinalizer`.

**Does not invalidate either test.** Computed the budget both ways directly:
```
malformed ("data"):    before=37.0  after=100.0  delta=63.0
well-formed ("text"):  before=37.0  after=100.0  delta=63.0
```
Identical. Both branches set `finalizer_reaches_uart = True` and the same
`finalizer_timeout` value for this scenario, and `include_finalizer`'s
`total += _vendor_uart_budget() + _hook_budget("uart")` line doesn't care which branch
set the flag. The budget arithmetic these tests exist to prove is genuinely exercised
either way.

**What it does mean:** across the whole suite, no test proving the doubled-budget
behavior (hook or vendor) has ever done so via a schema-valid finalizer payload — only via
the malformed-input defensive path. That path was itself deliberately reviewed and kept in
earlier iterations (`test_a_uart_finalizer_on_a_non_serial_tool_still_reserves_budget`
relies on the same fallback, intentionally, to prove budget is reserved even for malformed
input) — so this is not a *new* gap in kind, but it does mean the "real agent sends a
correct finalizer and gets the correct doubled budget" case specifically has never been
observed by any test, only inferred equivalent by this review.

**Severity rationale:** LOW. Confirmed by direct computation that no live bug is masked by
this — both code paths produce identical outcomes for the property under test.
Reported per the policy's "report everything, including things you expect to be ruled
extraneous" instruction rather than pre-filtered, since the underlying fact (a test
fixture element that would fail real schema validation in a live call) is worth having on
record even though it doesn't change the verdict on D20 itself.

---

## D24 — LOW, likely EXTRANEOUS — `write_serial`'s finalizer-budget-doubling is not independently tested

**Files:** `src/pyocd_debug_mcp/kernel/operations.py:673-683` (`write_serial`'s branch),
`tests/test_hook_gating_and_budget.py` (no matching test).

`ELIGIBLE_FINALIZER_TOOLS = frozenset({"read_serial", "write_serial"})`
(`kernel/finalizers.py:38`) — `write_serial` can carry a `uart_write` finalizer exactly
like `read_serial` can, and its own branch in `operation_timeout_seconds`
(`:673-683`) adds `_vendor_uart_budget() + _hook_budget("uart")` in the identical shape to
`read_serial`'s. Grepped the test file for `write_serial` combined with `on_exit`: no
test constructs this combination. Both the pre-existing hook-doubling test and the new
D20 vendor-doubling test use `read_serial` only.

**Why this is likely not material:** `include_finalizer` is a single closure defined once
per call to `operation_timeout_seconds`, not duplicated per tool — the doubling behavior
under test lives entirely inside that one shared function, and `tool_name` only affects
*whether* `parse_finalizer` accepts the input (both `read_serial` and `write_serial` are
accepted identically). This is structurally different from the original D17 gap, which
was a *literal, separate line* missing at one of several duplicated call sites — the kind
of bug that genuinely can diverge per site. Here, a `read_serial`-only test is a
reasonably strong proxy for `write_serial`'s identical code path, unlike D17's situation.

**Severity rationale:** LOW, expected EXTRANEOUS. Reported per policy rather than
pre-filtered.

---

## Targets 3 and 4 — remaining vendor/budget territory and independent sweep

**Re-read `operation_timeout_seconds` end to end fresh** (`kernel/operations.py:532-693`),
tracing `action_batch`'s interaction with the vendor/hook budget terms specifically, since
it is the one control-flow shape (recursive, sums children) not covered by the six-site
enumeration in prior rounds. Each child action's own recursive
`operation_timeout_seconds(child_name, child_arguments)` call already includes whatever
vendor/hook budget that child needs; `action_batch`'s own `include_finalizer(...)` wrapper
additionally reserves for *its own* batch-level finalizer, which runs once after all
children complete. This is additive and architecturally consistent — no double-count and
no gap found. `action_batch` is a member of none of `_PROBE_INVENTORY_TOOLS`/
`_UART_ACTION_TOOLS`/`_DISCOVERY_HOOK_TOOLS`, so an empty or malformed `actions` list falls
through to the bare default timeout with no vendor/hook budget, which is correct since no
snapshot would be taken in that case.

**Ledger accuracy spot check.** Read the D20/D21/D22 ledger rows (`reviews/ledger.md:46-48`)
against what actually shipped: D20's row correctly describes the test added and the
`31.5 != 63.0` break-and-revert proof; D21's row correctly and candidly documents the
first-draft error and its correction (including citing the exact wrong claim); D22's row
correctly records the EXTRANEOUS verdict and reasoning. No drift found.

**Grepped every `patch.object(ops, "SERIAL_FALLBACKS"` / `patch.object(hardware_inventory,
"SERIAL_FALLBACKS"` call site across the test suite** (16 total) specifically hunting for
a second instance of D19's before/after-against-the-ambient-default vacuous shape. Found
none — every remaining instance either compares two explicitly-patched, genuinely
different states (like D20's test) or is a single-state setup for an unrelated assertion.

No further findings from targets 3/4.
