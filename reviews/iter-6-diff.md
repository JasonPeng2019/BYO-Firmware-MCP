# Iteration 6 — Diff Adversary

First iteration under `reviews/REVIEW_POLICY.md`: report everything found, materiality is
adjudicated downstream, clearing now requires zero MUST-FIX (not zero VALID). Cap raised
5→10.

Scope: `git diff 6f3da0a..HEAD` in full — the whole feature — with priority weight on the
parts the policy calls out as least-reviewed: `hardware_inventory.py`'s vendor code,
`kernel/operations.py`'s six budget sites, and the tests added for both, plus the D17/D18/
D19 fixes (commit `820a559`) which landed after `reviews/narrow-d15-m6.md` and have not
been through a review pass. `reviews/ledger.md` (C1–C16, D1–D19, M1–M6), all six prior
iteration reports, and `reviews/narrow-d15-m6.md` read in full first. Nothing already
ruled INVALID is re-raised.

**Verification performed:** `uv run --locked ruff check src tests` → all checks passed.
`uv run --locked pyright src` → 0 errors, 0 warnings. `PYTHONPATH=src python -m unittest
discover -s tests` → **664 passed, 7 skipped, OK** (single run, 140.7s, matches the
coordinator's stated baseline). `git status --short` clean before and after every
experimental break described below (each reverted individually via `Edit`, confirmed via
`git diff`/`git status`, never `git checkout`/`restore`/`stash`).

## Summary

| ID | Severity | One-line |
| --- | --- | --- |
| D20 | MEDIUM (test-coverage, currently-correct production code) | The D17 fix (`kernel/operations.py:568-576`) is numerically verified correct — but no test proves the UART finalizer's vendor-CLI budget doubles, unlike its hook-budget sibling `test_read_serial_with_a_uart_finalizer_reserves_both_hook_budgets`. The exact class of gap that let D17 itself go unnoticed for a full round. |
| D21 | LOW | The guide's step-4 instruction to "document precedence" for the vendor-helper-as-third-provenance-source mechanism is unmet. `docs/architecture.md:381-383` still describes only the pre-D15 embedded vendor lookup; no doc mentions `vendor:` provenance, the unified-layer merge, or precedence against hook rows. |
| D22 | LOW (reported per "report everything," likely EXTRANEOUS) | When both a vendor helper and a UART hook are configured and native UART enumeration is fully empty, a vendor row and a hook row describing the *same physical port* do not merge into one row (session-local dedup requires matching `provenance`, which differs by construction) — the agent sees two candidates for one port instead of one. Handled by the existing friendly-selection/ambiguity flow, not a wrong-hardware-selection bug; flagged because the instruction is to report even likely-extraneous items rather than pre-filter. |

D17/D18/D19 (commit `820a559`) re-verified independently, all three hold up under a fresh
adversarial break-and-observe pass — see below. No fifth instance of "the fix introduces
the next defect" found in that commit.

---

## Re-verification of D17/D18/D19 (the unreviewed fixes named in scope)

All three were fixed in `820a559`, after `narrow-d15-m6.md` was written, and had not been
reviewed by anyone until this pass. Read the diff (`git diff 459524b..8a51138 --
src/pyocd_debug_mcp/kernel/operations.py tests/`) fresh, then re-proved each one by
breaking the exact guarded behavior and observing — not by re-reading my own prior
narrative and assuming it still holds.

**D17 (`kernel/operations.py:568-576`).** Fix adds `_vendor_uart_budget()` alongside the
existing `_hook_budget("uart")` inside `include_finalizer`'s `finalizer_reaches_uart`
branch. Computed directly (read-only, no source edit):

```
arguments = {"read_seconds": 3, "on_exit": {"action": "uart_write", "timeout_seconds": 2}}
before = operation_timeout_seconds("read_serial", arguments)                    # 37.0
with SERIAL_FALLBACKS patched to one spec:
    after = operation_timeout_seconds("read_serial", arguments)                 # 100.0
delta = 63.0 == 2 * (DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS + MAX_OWNED_PROCESS_CLEANUP_SECONDS)
```

Confirmed doubled (main action's reservation + the finalizer's own separate reservation),
matching the fix's stated intent exactly. **Verified correct by computation.** (See D20
for the coverage gap this same check exposes — nothing in the test suite asserts this.)

**D18 (`tests/test_phase2_uncovered.py::VendorUartRowsTests`).** Fix replaces the two
"nonzero exit code" tests' empty-stdout fixtures with realistic parseable output
(`"680123456 COM5 VCOM0\n"` etc.) paired with the nonzero exit code. Re-broke the guard
(`hardware_inventory.py:255-258`, temporarily removed `if exit_code != 0: continue`) and
ran only the two affected tests:
`python -m unittest tests.test_phase2_uncovered.VendorUartRowsTests.
test_nonzero_exit_code_124_timeout_skips_spec
tests.test_phase2_uncovered.VendorUartRowsTests.test_nonzero_exit_code_127_not_found_skips_spec`
— **both now correctly fail**, each naming the spuriously-produced `VendorUartRow` in the
diff output. Reverted the exact two lines removed; `git diff` confirmed empty afterward.
**No longer vacuous — fix verified load-bearing.**

**D19 (`tests/test_hook_gating_and_budget.py::BudgetTests`).** Fix replaces the two
before/after-against-itself comparisons with absolute-value assertions derived
independently (one patches `configured_probe_cli_commands` to a known 3-element return and
computes the expected total by hand; the other asserts equality against
`DEFAULT_OPERATION_TIMEOUT_SECONDS` directly). Re-broke `_vendor_uart_budget()`
(`kernel/operations.py:144-146`, temporarily added a constant `5.0 +` term unconditionally)
and ran only
`tests.test_hook_gating_and_budget.BudgetTests.test_probe_inventory_budget_excludes_vendor_term_when_no_specs_configured
tests.test_hook_gating_and_budget.BudgetTests.test_uart_action_budgets_exclude_vendor_term_when_no_specs_configured`
— **4 failures**, all naming the wrong totals (e.g. `35.0 != 30.0`). Reverted; confirmed
clean. **No longer vacuous — fix verified load-bearing.**

Also re-checked the `_DISCOVERY_HOOK_TOOLS` exclusion this same round, since it is the
other half of the "is the arithmetic complete" question and the fix's commit message
claims it was "traced and correctly excluded": added `_vendor_uart_budget()` to the
`_DISCOVERY_HOOK_TOOLS` branch (`operations.py:635-641`) and ran
`test_refresh_discovery_hooks_budget_unaffected_by_vendor_specs` alone — failed
(`94.0 != 31.0`), confirming the exclusion is both correct and tested. Reverted.

**No fifth instance of the fix-introduces-the-next-defect pattern found in this commit.**
The chain (C7→C12, C15→D16, D15→M6, M6→D17) does not extend to D17→(something in 820a559);
this fix is complete and correctly tested for what it changes. D20 below is a coverage gap
on the D17 fix specifically, not a new behavioral defect in it.

---

## D20 — MEDIUM (test-coverage gap on currently-correct production code)

**File:** `src/pyocd_debug_mcp/kernel/operations.py:568-576` (the fix); no corresponding
test exists anywhere in `tests/`.

The hook-budget sibling of this exact scenario has a dedicated regression test:
`tests/test_hook_gating_and_budget.py:419`,
`test_read_serial_with_a_uart_finalizer_reserves_both_hook_budgets`, which asserts the
`read_serial` + `on_exit: uart_write` combination reserves `2 * ONE_HOOK` when a UART hook
is loaded — proving the main action and the finalizer each get their own hook-budget
reservation. Grepped `tests/test_hook_gating_and_budget.py` and
`tests/test_phase2_uncovered.py` for `on_exit` and `finalizer`: the only two hits are the
existing hook-budget test above and
`test_a_uart_finalizer_on_a_non_serial_tool_still_reserves_budget` (also hook-only). **No
test anywhere asserts the vendor-CLI equivalent** — that `read_serial`/`write_serial` with
a `uart_write` finalizer reserves `2 * ONE_VENDOR_SPEC` when `SERIAL_FALLBACKS` is
non-empty, mirroring the hook case.

I verified by direct computation (see the D17 re-verification above) that the production
code is currently correct: the delta is 63.0s (doubled), not 31.5s (single). This is not a
live bug. It is the specific shape of gap that let D17 itself ship unnoticed for a full
round before this task's own narrow review caught it by hand — the fix that closed D17 has
exactly the same absence of a locking-in regression test that let D17 exist in the first
place. Given this task has now produced four consecutive "the fix introduces the next
defect" instances, each one caught only because someone happened to hand-verify the
preceding fix rather than because a test would have caught a regression automatically, an
untested numeric fix in this exact file is the highest-probability location for a fifth.

**Concrete risk this leaves open:** if a future change to `_vendor_uart_budget()`,
`include_finalizer`, or the `finalizer_reaches_uart` detection logic silently drops the
`_vendor_uart_budget()` term again (e.g., a refactor that consolidates the hook/vendor
terms and mishandles one branch), the full suite would still pass — nothing would notice.
That is precisely the "test that passes when the behaviour it names is broken" category
the policy calls always-material, except here it is the *absence* of a test rather than a
vacuous one; flagging it under the same reasoning since the practical exposure (a silent
regression with no test to catch it) is identical.

**Severity rationale:** MEDIUM, not HIGH — current behavior is verified correct by direct
computation, so no realistic user is affected today. Framed honestly as a coverage gap on
correct code, not a behavioral defect, so the coordinator can weigh it against the
materiality rubric's "does a realistic user get a wrong result" test as written (today,
no) against the rubric's explicit callout that a false-green test is "always material"
(this is the inverse — a true-green with no test backing it, on code with a four-for-four
track record of being wrong the next time it's touched).

---

## D21 — LOW — guide's "document precedence" instruction for the vendor third-provenance-source remains unmet

**File:** `docs/architecture.md:380-383` (the only vendor-related documentation in the
repo, confirmed by `grep -rln "vendor:" docs/ SERVER_GUIDE.md README.md` — zero hits for
the new provenance-tag string; `grep -rn "vendor" docs/architecture.md
docs/client-contract.md` — three hits total, none newer than the pre-D15 mechanism).

Guide, step 4, "Legacy vendor helpers": *"Keep `PYOCD_SERIAL_FALLBACK_REGISTRY` working
for one release, **document precedence**, deprecate only after migration tests pass."*
The functional requirement (route `SERIAL_FALLBACKS` behind the unified inventory layer as
a `("vendor:...",)`-tagged provenance source) is now correctly implemented and tested
(D15, confirmed again this round). The documentation sub-clause is not: `docs/
architecture.md:380-383`'s "Serial association uses stable USB identity and generic
metadata scoring first. Optional vendor helpers may be selected from an explicitly
configured external registry..." describes the *original*, pre-D15 embedded lookup inside
`resolve_serial_port` (still true and still working, per the narrow review's Question 2/3
verification) but says nothing about the new unified-inventory path: that native-empty
UART enumeration now also surfaces vendor rows as distinct, agent-visible
`serial_choices[]` entries tagged `vendor:{provider_id}`, or how those are ordered/merged
relative to hook rows when both are configured (see D22). No document in the repo
describes this at all.

**Severity rationale:** LOW. Purely a documentation gap — the guide's explicit acceptance
criterion table (§6) lists "Windows/macOS/Linux covered by automated tests and agent
guidance" as satisfied by "Step 1 `current_platform`, contract `platform_guidance`, CI
matrix," which is unaffected; this is a narrower, specific sub-instruction. No realistic
user gets a wrong result from missing documentation, but it is a guide requirement that
was never implemented, which the rubric names as its own MUST-FIX category independent of
user impact — reported for the coordinator to weigh against that clause specifically.

---

## D22 — LOW (reported per policy, likely EXTRANEOUS) — a vendor row and a hook row for the same physical port do not merge

**File:** `src/pyocd_debug_mcp/hardware_inventory.py:1067-1083` (`_matching_uart_index`),
`:308-342` (`_collect_uart_rows`).

Verified by reading the merge sequence: when native UART enumeration is empty,
`_collect_uart_rows` first merges vendor rows in (`if not uart_rows: ... vendor_rows =
self._vendor_uart_rows(...)`), then — independently, gated only on native being empty, not
on whether vendor rows were found — runs and merges UART hooks (`if run_uart_hooks: ...`).
Both conditions can be true simultaneously (an operator can configure both
`PYOCD_SERIAL_FALLBACK_REGISTRY` and a UART discovery hook manifest at once; nothing
prevents it). If a vendor helper and a hook both report the same physical port (e.g. both
say `"COM7"`), `_matching_uart_index`'s session-local dedup branch (rows without a
`stable_key()`, which is always true for vendor rows since they never carry
`usb_serial`/`vid`/`pid`) requires **both** `normalize_port_name(...)` match **and**
`row.provenance == candidate.provenance` — and a vendor row's provenance is always
`("vendor:{id}",)` while a hook row's is always `("hook:{hook_id}",)`, so they can never be
recognized as the same device by this path. Result: two separate `UartRow`s for what is
physically one port, both surfaced to the agent as distinct `serial_choices[]` candidates.

**Why this is likely not material:** the guide's own merge-rule mandate ("Same stable
device from both sources → one row") is scoped to *stable* identity (`stable_key()` via
usb_serial/vid/pid); vendor rows are deliberately, correctly never stable (see
`narrow-d15-m6.md`, Question 3 — fabricating a stable identity from vendor-CLI text was
explicitly identified and avoided as a worse defect). Session-local rows from genuinely
different provenance are supposed to stay distinct per the same merge design already
reviewed and accepted for hook-vs-hook and hook-vs-native session rows in earlier
iterations; this is that same rule applying to a new provenance pair, not new behavior.
The existing friendly-selection/ambiguity flow (guide §7, "More than one row could satisfy
a selection → fail and route back to friendly setup selection") is designed to handle
exactly "the agent sees two candidates it must disambiguate" and was reviewed and accepted
in earlier iterations. No wrong hardware gets selected — worst case is a redundant-looking
extra choice in an already-narrow configuration (both a vendor registry *and* a UART hook
manifest configured, *and* native enumeration fully empty).

**Severity rationale:** LOW, reported despite my own assessment leaning EXTRANEOUS,
because the review policy is explicit that under-reporting is the failure mode to avoid
and materiality adjudication belongs downstream. Not verified by breaking anything (no
source change would meaningfully "prove" a UX redundancy); verified by reading the merge
code path directly, as stated.

---

## Independent sweep of the rest of `git diff 6f3da0a..HEAD`

The diff outside `hardware_inventory.py`/`kernel/operations.py`/`server.py`'s two-line
wiring is **unchanged since iteration 5's HEAD** (`418b17d`) — confirmed via
`git diff 418b17d..8a51138 --stat`, which touches only those three source files plus
tests and review docs. `discovery_hooks.py`, `tools/discovery.py`,
`services/connections.py`, `setup_flow/*`, `probe_families.py`, `probe_inventory.py`,
`firmstore/store.py`, `timeouts.py`, and `tools/setup.py` have had one to five prior
review passes each (iterations 1–5) with no open findings. Spot-re-checked a handful of
their most-load-bearing invariants rather than re-reading them cold end to end, since the
policy directs effort toward the least-reviewed code specifically:

- `parse_probe_connection_id`'s structural `probeid:`/`probe:` prefix discrimination
  (C12/D11's fix) — unchanged, still a pure prefix check, no colon-counting.
- `MAX_HOOKS_TOTAL = 48` enforced pre-hash (C10/D9's fix) — unchanged.
- `configured_probe_cli_commands`'s `sys.executable` routing (M4) — unchanged since its
  own cold review in iteration 5.

No new finding from this spot-check; consistent with the policy's framing that this
territory has already had adequate coverage and the fresh risk is concentrated in the
files this iteration's brief named.
