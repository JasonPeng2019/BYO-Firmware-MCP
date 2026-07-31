# Narrow review — D15 fix and M6 (post-cap, scoped)

Not iteration 6. Scoped to `git diff 418b17d..459524b -- src/` plus the tests added
alongside: `hardware_inventory.py` (`vendor_uart_rows`, `_nrfjprog_vendor_rows`,
`_stlink_vendor_rows`, `_VENDOR_FALLBACK_ADAPTERS`), `server.py` (the `vendor_uarts=`
wiring), `kernel/operations.py` (`_vendor_uart_budget()` and its five application sites),
and `tests/test_phase2_uncovered.py::VendorUartRowsTests`,
`tests/test_hook_gating_and_budget.py::BudgetTests` (vendor section),
`tests/test_swd_process_isolation.py` (the D16 change). No full-codebase sweep attempted.

**Verification performed:** `uv run --locked ruff check src tests` → all checks passed.
`git status --short` clean before and after (every experimental break described below was
reverted; confirmed via `git diff`/`git status` immediately after each revert — see the
"Reverts" note at the end). Targeted test files re-run clean: 105/105
(`test_hook_gating_and_budget` + `test_phase2_uncovered` + `test_swd_process_isolation`).

## Summary

| ID | Severity | One-line |
| --- | --- | --- |
| D17 | MEDIUM | `include_finalizer` (`kernel/operations.py:568-576`) adds `_hook_budget("uart")` for a UART `on_exit` finalizer's own separate resolution call, but not `_vendor_uart_budget()` — the exact gap M6 fixed for the main action, left open for the finalizer leg. Empirically proven: one configured vendor spec adds only 31.5s (one reservation), not 63.0s (two), to `read_serial` with a `uart_write` finalizer. |
| D18 | MEDIUM | `VendorUartRowsTests::test_nonzero_exit_code_124_timeout_skips_spec` and `::test_nonzero_exit_code_127_not_found_skips_spec` are vacuous. Proven by deleting the `if exit_code != 0: continue` guard from `vendor_uart_rows` entirely: all 8 tests in the class, including these two, still pass. No other test in the suite exercises this guard. |
| D19 | LOW | Two of the four new "budget unaffected by vendor specs" `BudgetTests` (`test_probe_inventory_budget_unchanged_when_no_vendor_specs_configured`, `test_uart_action_budgets_unchanged_when_no_vendor_specs_configured`) patch `SERIAL_FALLBACKS` to `()`, the same value it already defaults to in the test environment — structurally unable to distinguish a correct implementation from one that ignores `SERIAL_FALLBACKS` entirely. Proven via a deliberate break; lower severity than D18 because *other*, unrelated, pre-existing tests in the same file do catch that specific break, so the practical blind spot is narrower. |

Questions 2 and 3 (vendor_uart_rows correctness, D15 design) came back clean — both
verified independently by reading source, not by inheriting the prior conclusions. Details
below.

---

## Question 1 — is the M6 budget arithmetic correct and complete?

**No — D17.** Full path enumeration, each checked against a budget site:

**Paths to `vendor_uart_rows` (via `_collect_uart_rows`, shared by both
`snapshot()` and `uart_snapshot()`):**

| Path | Reached from | Tool(s) | Budgeted? |
| --- | --- | --- | --- |
| `_resolve_serial_port_for_session` → `uart_snapshot()` | `read_serial`, `write_serial`, `serial_exchange` bodies (`resolve_port=` in `SerialToolServices`, `server.py:2526`) | `_UART_ACTION_TOOLS` | Yes — each tool's own branch adds `_vendor_uart_budget() + _hook_budget("uart")` directly (`operations.py:652,666,677`), confirmed present in the diff. |
| `_resolve_serial_port_for_session` → `uart_snapshot()` | `_finalizer_uart_write` (`server.py:6273-6283`), invoked by `include_finalizer` when `on_exit` is a `uart_write` finalizer | `read_serial`, `write_serial` (the only two `ELIGIBLE_FINALIZER_TOOLS`, `kernel/finalizers.py:38` — confirmed `reset_and_run` and any other tool cannot legitimately carry this finalizer, though the timeout resolver reserves budget defensively for malformed input regardless of eligibility, per its own `except FinalizerValidationError` fallback) | **No — D17.** `include_finalizer` adds `_hook_budget("uart")` for this second, later resolution call, but not `_vendor_uart_budget()`. |
| `.snapshot()` (full) | `_get_setup_status` (`server.py:4581`) | `get_setup_status` | Yes — `_PROBE_INVENTORY_TOOLS`. |
| `.snapshot()` (full) | `_setup_overview` (`server.py:5049`) | `setup_overview` | Yes — `_PROBE_INVENTORY_TOOLS`. |
| `.validation_inventory()` → `.snapshot()` | `_assigned_probe_uid_for_connect` (`server.py:1013`) → `_connect_impl` | `connect`, `connect_override`, `connect_under_reset` | Yes — `_PROBE_INVENTORY_TOOLS`. |
| `.validation_inventory()` → `.snapshot()` | `_setup_inventory` (`server.py:3809`) → `SetupWorkflow` | `board_setup`, `board_fix_setup` | Yes — `_PROBE_INVENTORY_TOOLS`. |
| `.snapshot()` (full) | `_resolved_probe_uid_for_connection` (`server.py:1056-1069`) → `_setup_continue` | `continue_setup` | **No, but pre-existing — see note below, not filed as a numbered finding.** |

**`_DISCOVERY_HOOK_TOOLS` exclusion** (`refresh_discovery_hooks` never gains vendor
budget): verified genuinely right, not just asserted. `refresh_discovery_hooks`'s handler
(`tools/discovery.py`) calls `execute_eligible_hooks` directly against an already-loaded
`DiscoveryHookSnapshot`; it never calls `HardwareInventoryService.snapshot()` or
`.uart_snapshot()`, so it structurally cannot reach `vendor_uart_rows`. Confirmed by
deliberately adding `_vendor_uart_budget()` to the `_DISCOVERY_HOOK_TOOLS` branch
(`operations.py:635-641`) and re-running
`test_refresh_discovery_hooks_budget_unaffected_by_vendor_specs`: it correctly failed
(`94.0 != 31.0`). Reverted immediately; `git diff` confirmed clean after.

### D17 — MEDIUM — `include_finalizer` reserves the hook budget for a UART finalizer's second resolution call but not the vendor-CLI budget

**File:** `src/pyocd_debug_mcp/kernel/operations.py:568-576`.

```python
def include_finalizer(timeout: float) -> float:
    if finalizer_timeout is None:
        return timeout
    total = timeout + finalizer_timeout + ARGUMENT_TIMEOUT_GRACE_SECONDS
    if finalizer_reaches_uart:
        # `_finalizer_uart_write` calls `_resolve_serial_port_for_session`, so the
        # finalizer itself can execute a UART hook after the main action finished.
        total += _hook_budget("uart")
    return total
```

`_finalizer_uart_write` (`server.py:6273-6283`) calls
`_resolve_serial_port_for_session(handle, override=None)` — the *same* function the main
`read_serial`/`write_serial` action already called once. This is a second, independent,
sequential invocation, after the main action completes: `_resolve_serial_port_for_session`
→ `_hardware_inventory.uart_snapshot()` → `_collect_uart_rows()`, which (when native UART
enumeration is empty) can invoke `vendor_uart_rows()` all over again — a fresh, separate
`_run_cmd` subprocess launch, exactly the cost `_vendor_uart_budget()` exists to reserve.
`include_finalizer` already understands this for hooks (the `+= _hook_budget("uart")`
line, and the comment explaining why) but the analogous vendor-CLI term was not added
alongside it when M6 introduced `_vendor_uart_budget()`.

**Proven, not inferred.** Ran the exact scenario directly against `operation_timeout_seconds`:

```python
arguments = {
    "read_seconds": 3,
    "on_exit": {"action": "uart_write", "data": "q", "timeout_seconds": 2},
}
before = operation_timeout_seconds("read_serial", arguments)          # -> 37.0
with patch.object(ops, "SERIAL_FALLBACKS", ("nrfjprog_spec",)):
    after = operation_timeout_seconds("read_serial", arguments)       # -> 68.5
```

Delta is **31.5s** (one `DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS +
MAX_OWNED_PROCESS_CLEANUP_SECONDS` reservation), not **63.0s** (two) — confirming only the
main action's vendor budget is reserved, not the finalizer's. For comparison, the existing
(pre-M6, still-correct) hook case does double correctly:
`test_read_serial_with_a_uart_finalizer_reserves_both_hook_budgets` asserts exactly
`2 * ONE_HOOK` and passes. There is no vendor-CLI equivalent of that test, and none was
added by this commit.

**Confirmed as a genuine gap, not a deliberate design choice:** added
`_vendor_uart_budget()` to the `finalizer_reaches_uart` branch (mirroring the `_hook_budget`
line exactly) and re-ran `test_hook_gating_and_budget`, `test_phase2_uncovered`, and
`test_swd_process_isolation` in full (105 tests) — all still passed. Nothing currently
locks in the missing term as intentional. Reverted immediately after (`git diff` confirmed
clean).

**Failure scenario, concrete:** An operator configures `PYOCD_SERIAL_FALLBACK_REGISTRY`
naming an `nrfjprog` fallback (realistic — this is exactly the deployment this mechanism
exists for). An agent calls `read_serial(read_seconds=3, on_exit={"action": "uart_write",
"text": "...", "timeout_seconds": 2})` on a board where native pyserial enumeration is
empty at the time of the call (e.g., the board's only UART bridge needs vendor-CLI
correlation to be found at all, or pyserial genuinely can't see it and the vendor CLI must
run for the finalizer just as it did for the main action). The main read reserves its own
vendor-CLI budget correctly (31.5s) and completes. The `on_exit` finalizer then runs
*after* the main action, re-resolving the port from scratch — if native UART is still
empty, it can launch the vendor CLI subprocess again, but the operation's deadline was
never widened for that second launch. Under load or a slow vendor tool, the finalizer's
own subprocess can be running when the operation's overall deadline (computed without this
second reservation) expires, and gets cancelled mid-flight — the same "read is cancelled
before it starts" failure class step 9 exists to prevent, reopened for exactly the
finalizer sub-case.

**Severity rationale:** MEDIUM. The main action itself is correctly budgeted (this is not
a full regression of the original UART-budget defect); the gap is specific to the
combination of an `on_exit: uart_write` finalizer *and* a configured vendor fallback *and*
native UART being empty at finalizer-resolution time — narrower than the class of bug that
motivated step 9 in the first place, but real and unguarded by any test.

**Note on the `continue_setup` path (not filed as a numbered finding):** `_setup_continue`
(tool `continue_setup`, `tools/setup.py:509`, `server.py:5583`) calls
`_resolved_probe_uid_for_connection` (`server.py:1056`), which takes a fresh
`_hardware_inventory.snapshot()` with no special-case exception handling around exceeding
its deadline. `continue_setup` is not in `_PROBE_INVENTORY_TOOLS`, `_UART_ACTION_TOOLS`, or
`_DISCOVERY_HOOK_TOOLS`, and has no `plan_defs.py` entry, so it resolves to the bare
`DEFAULT_OPERATION_TIMEOUT_SECONDS` (30.0s) — already less than a single hook's own
`MAX_HOOK_TIMEOUT_SECONDS` (60.0s), so this gap **predates the reviewed diff** (it exists
for hooks alone, with or without vendor CLI, and is unrelated to any commit in
`418b17d..459524b`; `server.py`'s diff for this range is only the two-line `vendor_uarts=`
wiring). Flagging it only because question 1 asked for full path enumeration: the D15/M6
diff does make this pre-existing gap's blast radius larger (a second, independent
unbudgeted subprocess source now flows through the same unguarded path), but the
underlying omission is not attributable to these commits and was not introduced or
worsened in kind, only in degree. Recommend a separate, explicitly-scoped look at
`continue_setup`'s timeout membership if the coordinator wants it addressed; not filed as
D20 because it fails the "in scope" test for this review.

---

## Question 2 — is `vendor_uart_rows` correct? (cold read, failure modes)

**Clean.** Read `hardware_inventory.py:185-259` end to end plus
`serial_resolver.py::resolve_command_path` (`:556` area) and both parsers
(`parse_nrfjprog_com_output:303-322`, `parse_stm32_programmer_list_output:325-365`) in
full, independent of the coordinator's stated conclusions.

- **Both parsers are genuinely raise-proof on arbitrary text**, verified by reading them,
  not assumed. `parse_nrfjprog_com_output` is a `re.compile(...).match()` per line inside a
  `for raw_line in text.splitlines():` loop with `if not match: continue` — no operation in
  the loop body can raise for any `str` input. `parse_stm32_programmer_list_output` uses
  only `.strip()`, `.startswith()`, `.lower()`, `str.split(":", 1)` (only called after
  `":" in line` is already checked, so it's always exactly 2 parts), and `dict.get()` —
  again, no path that raises on malformed input. Confirmed independently.
- **`_run_cmd` genuinely does not catch `PermissionError`/`OSError`** — read `_run_cmd`
  (`server.py:864-889`) directly: only `except FileNotFoundError` and
  `except subprocess.TimeoutExpired` are handled; any other `OSError` subclass propagates
  uncaught, exactly as the coordinator described. Verified the "identical to the
  pre-existing `native_probes` exposure" ruling is correct by reading
  `list_connected_probes_detailed` (`probe_inventory.py:264`): its own `run_cmd(...)` call
  has no surrounding try/except either — same shape, same exposure. **Then went one step
  further and checked whether that exposure is actually mitigated anywhere**, since
  "identical exposure" only matters if something catches it: read all four call sites in
  `server.py` that invoke `.snapshot()`/`.uart_snapshot()`/`.validation_inventory()`
  (`server.py:1064-1067`, `4578-4581`+`4588`(`except Exception`), `5048-5049`+`5088`(`except
  Exception`)) plus `_resolve_serial_port_for_session`'s own wrapper (`server.py:1640-1643`,
  `except Exception as exc: raise RuntimeError(...)`). **All four are wrapped in broad
  `except Exception` handlers** that convert any uncaught exception (including a
  `PermissionError` from either `vendor_uart_rows` or `native_probes`) into a typed
  response rather than crashing the process. The ruling holds, confirmed by checking the
  mitigation directly rather than trusting the analogy alone.
- **No unbounded-buffer exposure beyond the already-accepted one.** `vendor_uart_rows`
  reuses `_run_cmd`/`run_owned`'s `capture_output=True` path (the same one Trap 1's caller
  audit already covers for `configured_probe_cli_commands()`), which does buffer
  `communicate()` without a hard cap. This is a new *caller* of an already-accepted
  pattern, not a new *class* of exposure — vendor CLI tool listings (`nrfjprog --com`,
  `STM32_Programmer_CLI -list`) are inherently small (one line per attached device), same
  trust/output-volume profile as the pyOCD probe listing this pattern was already reviewed
  for. Not filed as a finding.

---

## Question 3 — was the D15 design (parsers, not `resolve_serial_port`) right?

**Right, verified by reading both the new code and its dependency.** The docstring's claim
— that `_resolve_nordic_serial`/`_resolve_stlink_serial` can only *disambiguate among*
ports pyserial already returned, never discover one pyserial missed, because both end with
`_find_port_by_name(ports, entry.port)` which returns `None` if the OS-visible `ports` list
doesn't already contain that port — checked directly against
`serial_resolver.py:470-512`: correct. `_resolve_nordic_serial` and `_resolve_stlink_serial`
both terminate with exactly that pattern. Routing `vendor_uart_rows` through the raw
parsers instead is therefore the only design that can actually serve the case this
provenance source exists for (native pyserial found nothing at all).

- **Not duplicated against native rows**, structurally: `_collect_uart_rows`
  (`hardware_inventory.py:308-342`) only calls `_vendor_uart_rows()` inside
  `if not uart_rows:` — vendor rows are never even *requested* when native rows exist, so
  there is no code path in which a vendor row and a native row for the same device could
  both appear in one snapshot.
- **`_uart_scope` classification verified correct.** `_uart_scope(usb_serial, vid, pid)`
  (`hardware_inventory.py:1041-1051`) returns `"session"` whenever `usb_serial` is falsy,
  checked first. `_nrfjprog_vendor_rows`/`_stlink_vendor_rows` never set `usb_serial`,
  `vid`, or `pid` on the `VendorUartRow`s they build (both dataclass fields default to
  `None` and neither adapter passes them) — confirmed by reading both functions
  (`hardware_inventory.py:191-208`) — so every vendor row is unconditionally `"session"`.
  This is the *safe* direction: neither parser's fields (`probe_serial` for nrfjprog,
  `manufacturer`/`location` for STM32) are a genuine USB-descriptor serial number for the
  *port itself* (nrfjprog's `probe_serial` identifies the debug probe, not the UART
  endpoint — feeding it into `usb_serial` would conflate two different identities, which
  the code correctly avoids doing). Matches guide trap 14 (session-local rows must never
  reach `AttachmentCache`) exactly; `AttachmentCache` never sees these rows because
  `_uart_scope` never marks them stable.
- **Cross-spec collision behavior is consistent with the existing hook-row merge design,
  not a new defect.** Two vendor specs (or a vendor row and a hook row) reporting the same
  `port_path` dedupe only when `provenance` also matches (`_matching_uart_index`,
  `hardware_inventory.py:1067-1083`); different-provenance rows for the same port_path
  stay as separate rows. This is the same session-local dedup rule already reviewed and
  accepted for hook rows in earlier iterations, applied unchanged to the new provenance
  source — not something D15 introduced or weakened.

No defect found on any of the three sub-questions (wrong rows, duplication, scoping).

---

## Question 4 — do the new tests fail when the guarded behavior breaks?

Picked the load-bearing assertions per the coordinator's instruction and broke the
guarded behavior directly in `src/`, one change at a time, reverting immediately after
each and confirming `git status`/`git diff` were clean before moving to the next.

**D18 — proven vacuous.** Removed the `if exit_code != 0: continue` guard from
`vendor_uart_rows` (`hardware_inventory.py:256-257`) entirely — i.e., made it process
stdout regardless of exit code, exactly the regression
`test_nonzero_exit_code_124_timeout_skips_spec` and
`test_nonzero_exit_code_127_not_found_skips_spec` are named to catch. **All 8 tests in
`VendorUartRowsTests` still passed, unchanged.** Root cause: both tests' fake `run_cmd`
returns an *empty* stdout string alongside the nonzero exit code
(`return (124, "", "")` / `return (127, "", "")`), and both parsers correctly produce zero
rows from empty text regardless of whether the exit-code guard ran — so the assertion
`self.assertEqual(result, [])` holds identically whether the guard exists or not. Neither
test can ever fail from removing the guard, because their fixtures don't include the one
ingredient (non-empty, parseable-looking text alongside a nonzero exit code — realistic,
since a vendor CLI's *stderr* diagnostic text could coincidentally satisfy the loose
regex/section-header parsers) that would make the guard's absence visible. Reverted; full
targeted-file run (105 tests) confirmed clean afterward.

**D19 — proven vacuous by construction, but the regression class is caught elsewhere.**
Changed `_vendor_uart_budget()` to return a flat `999.0` unconditionally (ignoring
`SERIAL_FALLBACKS` entirely) and re-ran `BudgetTests`. Ten tests failed — but
`test_probe_inventory_budget_unchanged_when_no_vendor_specs_configured` and
`test_uart_action_budgets_unchanged_when_no_vendor_specs_configured` were **not** among
them; both still passed. Both tests patch `ops.SERIAL_FALLBACKS` to `()` inside the `with`
block and compare against a `before` value computed against the *real*, unpatched
`SERIAL_FALLBACKS` — which is *already* `()` by default in the test environment
(`test_serial_fallbacks_defaults_to_empty` confirms this explicitly). Patching `()` to
`()` is a no-op; any deterministic function of `SERIAL_FALLBACKS`, correct or broken,
necessarily returns the same value both times, so `assertEqual(after, before)` is
mathematically guaranteed to pass regardless of what `_vendor_uart_budget()` actually
computes. This is provable from the test's structure, not just the one mutation tried.
**However**, this specific break (unconditional nonzero budget) *was* caught — by four
older, unrelated tests in the same file that assert an absolute value instead of a delta
(`test_uart_action_budgets_equal_todays_value_with_no_hooks`,
`test_the_snapshot_store_is_a_valid_provider_and_starts_at_zero`,
`test_a_failing_provider_never_breaks_a_deadline`,
`test_malformed_provider_counts_are_ignored_not_fatal`) — so the suite as a whole is not
blind to this class of regression, only these two specific tests are, despite their names
claiming to be the ones guarding it. Reverted; confirmed clean.

**The other two vendor `BudgetTests` are genuinely load-bearing, not vacuous** — verified
by the same method:
- `test_probe_inventory_budget_grows_with_configured_vendor_specs` and
  `test_every_uart_action_gains_budget_from_configured_vendor_specs` correctly **failed**
  (4 failures) when `_vendor_uart_budget()` was broken to always return `0.0`.
- `test_refresh_discovery_hooks_budget_unaffected_by_vendor_specs` and
  `test_get_discovery_hook_contract_budget_unaffected_by_vendor_specs` patch
  `SERIAL_FALLBACKS` to a *non-empty* tuple (genuinely different from the real default),
  so they are structurally capable of catching a wrongly-added budget term — confirmed by
  the `_DISCOVERY_HOOK_TOOLS` mutation in Question 1, which
  `test_refresh_discovery_hooks_budget_unaffected_by_vendor_specs` correctly failed on.

**D16 (`test_swd_process_isolation.py`), re-verified.** The fix restores construction to
*outside* `assertRaises` (matching the pre-iteration-4 structure, so a construction-side
timeout is once again a distinguishable test ERROR rather than absorbed into a false
PASS) and adds a direct proof that `.call()` was actually entered:
`self.assertEqual(client._request_id, 1)`, checked after the `assertRaises` block, relying
on `_request_id` only incrementing inside `.call()`'s locked section before it can raise.
This is exactly what D16 asked for. Ran the single test in a tight back-to-back loop (six
separate `python -m unittest` invocations in immediate succession) as an adversarial
stress probe: **3 of 6 runs surfaced as ERROR** (construction itself timed out against the
2.0s startup budget), which is the fix working as intended — the same scenario that used
to silently pass now visibly fails. This tight-loop pattern (repeated fresh interpreter
launches with minimal spacing) is a harsher probe than normal suite execution: running the
full `test_swd_process_isolation.py` file (30 tests, natural spacing, one interpreter
launch for the whole file) five times back to back was **clean 5/5**, consistent with
iteration 4's own verification method and result. Not filed as a new finding — this is the
expected, and correct, consequence of the fix, not a regression it introduced; noted here
because it's directly responsive to "prove the D16 fix works," not just "assume it does."

---

## Reverts

Every experimental break made during this review was to `src/pyocd_debug_mcp/
hardware_inventory.py` or `src/pyocd_debug_mcp/kernel/operations.py`, applied one at a
time via `Edit`, and reverted via a second `Edit` restoring the exact original text
immediately after observing the test result — never `git checkout`/`restore`/`stash`.
`git status --short` and `git diff -- src/ tests/` were both empty (no output) after every
revert, confirmed before starting the next experiment and again at the end of the review.
No file was left modified.
