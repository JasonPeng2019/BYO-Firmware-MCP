# Task status — debugger UART discovery hook implementation

Branch `Proto-1-WIP`, HEAD `8ec6b02`, working tree clean.
Suite: **657 passed / 7 skipped**. `ruff check src/ tests/` clean. `pyright src/` clean.
All three verified by running them at HEAD, not from agent reports.

## Checklist

| # | Condition | State |
| --- | --- | --- |
| 1 | Phase 0 — guide implemented as written | ✅ |
| 2 | Phase 0 — every test the guide specifies passes | ✅ |
| 3 | Phase 1 — adversarial review loop run to completion | ✅ ran all 5 permitted iterations |
| 4 | **Phase 1 — a full iteration marked zero new findings VALID** | ❌ **never happened** |
| 5 | Phase 1 — every VALID finding fixed | ✅ 38 findings adjudicated, all VALID ones fixed |
| 6 | Phase 1 — every INVALID finding recorded and left untouched | ✅ D3, D5 |
| 7 | Phase 1 — no regressions | ✅ |
| 8 | Phase 2 — straggler tests written and logged | ✅ 40 tests, `reviews/new-tests.md` |
| 9 | Phase 2 — full suite passes | ✅ |
| 10 | **Final fixes carry an adversarial pass** | ❌ **D15/D16 unreviewed** |

## What is blocking

**Blocker 1 — Phase 1 never cleared.** The termination condition is "a full
iteration marks zero new findings VALID." That did not occur in any of the five
permitted iterations:

| Iter | Outcome |
| --- | --- |
| 1 | 13 findings, 11 VALID fixed, 2 INVALID. Did not clear. |
| 2 | C7–C11 + M1–M3, including the CRITICAL provider-blind `connection_id`. Did not clear. |
| 3 | C12/D11 CRITICAL — legacy-token colon misparse could resolve to a *different real probe*. Plus C13, C14. Did not clear. |
| 4 | D14 (baseline not reproducible from a clean checkout), C15 (flaky timeouts). Did not clear. |
| 5 | **Safety cap.** D15, D16 — both MEDIUM, both verified. Did not clear. |

Iteration 5 was the cap, so per the standing directive the loop stopped rather
than running a sixth. That is the correct outcome of the rule, not a failure to
follow it — but it means condition 4 is unmet, and condition 4 is a stated
precondition for GREEN.

**Blocker 2 — the last two fixes are unreviewed.** D15 and D16 were fixed after
the final review pass, so no adversary has seen them. In this codebase that is a
demonstrated risk rather than a formality: **two of this task's most severe
findings were each introduced by a previous fix.** C12 (the worst finding in the
task — a token misparse that could silently resolve to a different physical
probe) was introduced by FIX 8 in iteration 2 and caught in iteration 3. D16 was
introduced by the C15 fix in iteration 4 and caught in iteration 5. The pattern
of "a fix creates the next defect" held twice; the D15/D16 fixes have had no
iteration after them to catch a third instance.

## Unresolved disagreements

1. **D15 severity — coordinator overruled the reviewer.** The reviewer argued
   D15 was near-harmless because even fully wired the feature would be
   structurally dead, since `_resolve_nordic_serial` can only match ports
   pyserial already returned. That traced the wrong call path: the guide
   specifies moving the *parsers* behind the layer, and the parsers derive port
   names from vendor-CLI text with no pyserial involvement — which is what makes
   the feature meaningful in the only case it is consulted (native enumeration
   returned empty). Accepting the reviewer's framing would have produced an
   inert fix satisfying the letter of the finding. Fixed per the coordinator's
   design; the reviewer has not re-examined it.
2. **D3 and D5 remain ruled INVALID** and are left untouched in the code, as
   directed. Recorded in `reviews/ledger.md`.
3. **19 pre-existing `pyright` errors in `tests/`** (trust-model rounds 1/3/4,
   change-loop) are untouched. They predate this task and are outside its scope;
   fixing them would be scope expansion. `pyright src/` and the new test file are
   both clean.

## Process defects worth carrying forward

- **A cross-agent blanket revert destroyed in-flight work.** The agent proving
  D16's test could fail was told to break `swd_process.py` temporarily and revert
  it; it reverted all of `src/`, wiping the concurrently-written D15
  implementation. Caught by diffing the tree, not by the agent's report — which
  said "only `tests/` modified," true of its own edits and wrong about the tree.
  Root cause was coordinator scheduling plus a file-ownership rule that was
  advisory rather than enforced.
- **Three agent self-reports did not survive verification** this round: the
  revert above, "ruff check clean" when two `F401` errors existed, and "pyright
  0 errors" that had been scoped to `src/` only while 18 errors sat in the new
  test file. None were dishonest — each described the command the agent actually
  ran — but summaries consistently described the check rather than the tree.
- **The review loop is structurally blind to false premises in the guide.** The
  guide asserted as fact that `configured_probe_cli_commands` already routed
  pyOCD through `sys.executable`. It did not. That survived four passes because
  it lived in code no diff touched. Iteration 5 swept all 14 remaining
  "already/currently/today" claims and found no second instance, but the class of
  defect is only detectable by deliberately auditing the specification against
  the code — no amount of reviewing the diff finds it.

## Verdict

Every phase's *work* is complete and the tree is green. What is missing is the
clearance condition itself, which cannot be manufactured by declaring it. Two
conditions are unmet, both concerning review coverage rather than known defects.

**Correction (post-`f54640f`):** an earlier revision of this file stated there were
no known unfixed defects at HEAD. That is no longer true. Reassessing the round,
the coordinator found **M6** — the D15 fix added one vendor-CLI subprocess per
`SERIAL_FALLBACKS` spec inside `snapshot()`, each carrying `_run_cmd`'s 30s
default, but the `_PROBE_INVENTORY_TOOLS` operation timeout budget
(`kernel/operations.py:603-613`) accounts only for probe CLI commands and hooks.
With vendor specs configured and a hung vendor tool, a snapshot can consume time
the budget does not know about and be cancelled mid-discovery by the very timeout
meant to prevent that. Gated (opt-in via `PYOCD_SERIAL_FALLBACK_REGISTRY`, and
only when native UART enumeration is empty), so not critical — but real, and
found only because blocker 2 prompted a direct re-read of the unreviewed fix.

M6 is the **third** instance of this task's signature pattern: C12 was introduced
by the fix for C7, D16 by the fix for C15, and M6 by the fix for D15. Each was
caught by the pass that followed it. M6 was caught with no pass following it,
which is precisely what blocker 2 predicts and why it is not a formality.

**M6 is now fixed**, and the coordinator's own analysis of it was incomplete. The
brief named only `_PROBE_INVENTORY_TOOLS`; the implementer traced further and
found `_UART_ACTION_TOOLS` reaches the identical `_collect_uart_rows()` path via
`uart_snapshot()`, and that `read_serial`, `write_serial` and `serial_exchange`
each take an argument-driven early-return branch that discards `resolved_timeout`
entirely — so adding the term to the generic block alone had **no effect** for
those tools' typical call shape, proven by a test that first failed with a 0.0s
delta. The vendor term now appears at all five sites plus the helper.
`_DISCOVERY_HOOK_TOOLS` was traced and correctly excluded: `refresh_discovery_hooks`
calls `services.load_snapshot()`/`run_hooks()` directly and never reaches
`HardwareInventoryService`, with a regression test asserting its budget stays flat.
Suite 664 passed / 7 skipped; ruff, pyright clean — all verified by the
coordinator at HEAD, not from the report.

## Post-Phase-3 scoped review (not a sixth iteration)

The safety cap forbids another full loop pass. It does not forbid reviewing a
small body of production code no adversary had ever seen, so D15 and M6 were
reviewed under a scope restricted to `git diff 418b17d..459524b -- src/` and its
tests. That pass found three more, all VALID, all proven by breaking the guarded
behavior rather than by argument:

- **D17 (MEDIUM, production)** — the M6 fix covered five budget sites and missed a
  sixth. `include_finalizer` reserves `_hook_budget("uart")` for the finalizer's
  second independent port resolution but never added `_vendor_uart_budget()`;
  measured at 31.5s reserved where 63.0s is required. Fixed.
- **D18 (MEDIUM, tests)** — the two `vendor_uart_rows` "nonzero exit code" tests
  were vacuous. Deleting the exit-code guard outright left all 8 tests passing,
  because both fixtures paired the nonzero exit with *empty* stdout. Fixed with
  realistic parseable output — which is also the case that actually occurs, since
  `_run_cmd` returns exit 124 with whatever partial stdout the child produced
  before being killed.
- **D19 (LOW, tests)** — two budget tests patched `SERIAL_FALLBACKS` to `()`, its
  ambient default, and so could not fail. Rewritten as absolute-value assertions.

Substantive negative results from the same pass, recorded because they are
results: `vendor_uart_rows` is correct; the D15 design decision was verified
right; the `PermissionError` scope ruling holds (all four `server.py` call sites
have a broad `except Exception`); the `_DISCOVERY_HOOK_TOOLS` exclusion is
correct; the D16 fix works under adversarial stress.

**D17 is the fourth consecutive instance of a fix introducing the next defect:
C7→C12, C15→D16, D15→M6, M6→D17.** That chain is the most reliable finding this
task produced. The D17/D18/D19 fixes are themselves unreviewed, so blocker 2 is
reduced but not closed — it has moved one level down, which is the honest
description rather than saying it is resolved.

## Known, deliberately unfixed

`continue_setup` (`server.py` `_setup_continue`) calls
`_resolved_probe_uid_for_connection` → `_hardware_inventory.snapshot()` but is not
a member of `_PROBE_INVENTORY_TOOLS`, so it receives only the flat default
operation timeout with **no** reservation for probe CLI, vendor CLI, or hooks.
Found independently by both the reviewer and the implementer, converging from
opposite directions. It predates this entire feature and is not a missing vendor
term on a covered site — it is a tool with no inventory budget at all. Fixing it
requires a categorization decision outside this feature's scope. Flagged for
separate triage rather than folded in silently.

Suite at final state: **664 passed / 7 skipped**, ruff clean, pyright clean on
`src/` — verified by the coordinator at HEAD.

STATUS: ❌ RED
