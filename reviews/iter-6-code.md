# Iteration 6 — Code Adversary

Scope: the whole codebase as it stands at HEAD (`8a51138`), attacked fresh as a hostile
reviewer seeing it for the first time, per `reviews/REVIEW_POLICY.md`. Priority weight on
the least-reviewed code the coordinator named: `hardware_inventory.py`'s vendor path,
`kernel/operations.py`'s six budget sites, and the D17/D18/D19 fixes (`820a559`), which had
never been reviewed before this iteration. `reviews/ledger.md` (C1–C16, D1–D19, M1–M6) and
all prior iteration reports read first; nothing re-raised.

**Verification performed this round:** `uv run --locked ruff check src tests` → all checks
passed. `uv run --locked pyright src` → 0 errors. `PYTHONPATH=src python -m unittest
discover -s tests` → 664 passed, 7 skipped, OK (single run at the end, per the policy —
not rerun after each intermediate check; scoped single-test/single-class runs used while
iterating, all reverted via `Edit` with `git diff`/`git status` confirmed empty after each).

## Summary

| ID | Severity | One-line |
| --- | --- | --- |
| C17 | MEDIUM (cross-reference to D20) | Cold-reading `include_finalizer` (`kernel/operations.py:568-576`) and then searching the test tree for its vendor-CLI-doubling counterpart to the existing `test_read_serial_with_a_uart_finalizer_reserves_both_hook_budgets` turns up nothing — same fact as D20 in `reviews/iter-6-diff.md`, independently reachable by a reviewer who has never seen the diff, just this file and its test suite side by side. |

No other findings from a fresh, diff-blind read. Everything else checked below came back
clean, with what was checked shown rather than asserted.

---

## Cold read: `hardware_inventory.py`'s vendor path (`VendorUartRow`, `vendor_uart_rows`, `_VENDOR_FALLBACK_ADAPTERS`, `_nrfjprog_vendor_rows`, `_stlink_vendor_rows`)

Read `hardware_inventory.py:160-260` end to end as if seeing it for the first time,
independent of `reviews/narrow-d15-m6.md`'s prior analysis (which reached the same
conclusions from the diff-review angle; re-derived here rather than cited).

- **Dispatch table cannot silently swallow a misconfigured parser.** `_VENDOR_FALLBACK_ADAPTERS`
  (`:216-219`) is keyed by `spec.parser`, and `vendor_uart_rows` treats an unrecognized key
  as a silent no-op (`if adapt is None: continue`). Traced whether this could mask a real
  operator typo: `serial_resolver.py:139` defines `_SUPPORTED_FALLBACK_PARSERS =
  frozenset({"nrfjprog_com", "stm32_programmer_list"})`, and `_load_serial_fallbacks`
  (`serial_resolver.py:168-169`) raises `RuntimeError(f"Unsupported serial fallback
  parser: {parser}")` at **load time** (module import, since `SERIAL_FALLBACKS` is computed
  once at import from `PYOCD_SERIAL_FALLBACK_REGISTRY`) for anything outside that set. So
  every `spec` that ever reaches `vendor_uart_rows` has already passed this validation —
  the `adapt is None` branch in `vendor_uart_rows` is genuinely unreachable in practice,
  exactly as its own comment claims ("this never needs a third branch... only a safe no-op
  fallback below for defense in depth"). Confirmed, not assumed.
- **`VendorUartRow.usb_serial`/`vid`/`pid` are structurally always `None`.** Read both
  adapters (`_nrfjprog_vendor_rows:191-199`, `_stlink_vendor_rows:202-210`) — neither
  constructor call passes those three keyword arguments, so every vendor row takes the
  dataclass defaults. Traced forward into `_uart_scope` (`:1041-1051`): falls to
  `"session"` the moment `usb_serial` is falsy, checked first. No vendor row can ever be
  misclassified `"stable"` and therefore can never reach `AttachmentCache` — verified this
  is the *correct* direction to fail in, not merely the code's own claim, by reading what
  `AttachmentCache._validated_identity` (`firmstore/cache.py:217-227`, unchanged since
  earlier iterations) does with a `"session"`-scope row: nothing, by construction, since
  callers route session-scope rows to a different, run-scoped store instead.
- **Provenance-property blind spots checked.** Grepped every consumer of
  `.native`/`.from_hook` (`UartRow`/`ProbeRow` properties, `hardware_inventory.py:115,119,
  139,143`) across `src/`: the only consumers are `InventorySnapshot.native_probes`/
  `.native_uarts` (`:276,280`), which filter *for* native rows — nothing branches on "is
  this row native, hook, or neither" as an exhaustive three-way switch that could treat a
  vendor row (neither) as an error case. A vendor row silently being "neither native nor
  hook" is a safe, already-anticipated state, not an unhandled one.
- **Exit-code/exception handling** — re-verified independently of `narrow-d15-m6.md`'s
  prior pass rather than trusting it: `_run_cmd` (`server.py:864-889`) only catches
  `FileNotFoundError` and `subprocess.TimeoutExpired`; any other `OSError` (e.g.
  `PermissionError`) propagates uncaught out of `vendor_uart_rows`, exactly like
  `native_probes=lambda: list_connected_probes_detailed(_run_cmd)` one line above it at
  the sole construction site (`server.py:3010-3016`) — same exposure, same adjacent
  precedent, which the policy's own EXTRANEOUS example names verbatim. Re-confirmed (not
  re-cited) that all four `server.py` call sites that can reach `.snapshot()`/
  `.uart_snapshot()` wrap it in a broad `except Exception`
  (`server.py:1640-1643`/`4578`+`4588`/`5048`+`5088`/`1064-1067`), so an uncaught
  `PermissionError` from either source degrades to a typed response rather than crashing
  the process, on every path.

**No defect found.** Consistent with, and independently re-derived from, the narrow
review's Question 2/3 conclusions.

---

## Cold read: `kernel/operations.py`'s six vendor-budget sites

Located every site that adds `_vendor_uart_budget()` by reading the file top to bottom
rather than trusting the enumeration in `narrow-d15-m6.md`:

1. `_PROBE_INVENTORY_TOOLS` block (`:622-634`)
2. `_UART_ACTION_TOOLS` generic block (`:642-643`)
3. `read_serial`'s own branch (`:644-654`)
4. `serial_exchange`'s own branch (`:655-668`)
5. `write_serial`'s own branch (`:669-679`)
6. `include_finalizer`'s `finalizer_reaches_uart` branch (`:568-576`)

Six, matching the fix commit's own count. Confirmed `_DISCOVERY_HOOK_TOOLS`
(`:635-641`) and `get_discovery_hook_contract` (reaches none of the above — falls through
to the bare `include_finalizer(resolved_timeout)` at the function's end, `:689`) correctly
have **no** vendor term, both by reading the code and by the break-and-observe check
recorded in `reviews/iter-6-diff.md`.

Checked each of the three UART-action-tool named branches (`read_serial`,
`serial_exchange`, `write_serial`) for whether the generic `_UART_ACTION_TOOLS` block's
addition to `resolved_timeout` (site 2) is live or dead code, since a prior finding in this
task (M6's own history) turned on exactly this distinction. All three named branches
`return include_finalizer(max(planned_timeout, ...) + _vendor_uart_budget() +
_hook_budget("uart"))` using `planned_timeout`, not `resolved_timeout` — so site 2's
mutation of `resolved_timeout` only matters when the tool-specific argument
(`read_seconds`/`timeout_seconds`) is absent or not a positive finite number, falling
through past the named branch to `return include_finalizer(resolved_timeout)` at the very
end. Not dead code, just a narrower live case than it looks (the omitted-argument
fallback) — consistent with, not contradicting, the fix as shipped.

**No defect found** beyond C17/D20 (the missing regression test, not a code defect).

---

## Cold read: the three touched test files

- **`tests/test_hook_gating_and_budget.py::BudgetTests`** (30 tests total, vendor section
  re-read fresh): the two rewritten "excludes vendor term" tests now pin absolute,
  independently-derived values rather than comparing an input against itself — reads as a
  correct, load-bearing test on inspection, confirmed by breaking (see
  `reviews/iter-6-diff.md`). No other test in the class shares the vacuous-comparison
  shape (`patch.object(..., X)` compared against an unpatched default that already equals
  `X`) — grepped every `patch.object(ops, "SERIAL_FALLBACKS"` and `patch.object(ops,
  "configured_probe_cli_commands"` call in the file; the two fixed ones were the only
  instances of the pattern.
- **`tests/test_phase2_uncovered.py::VendorUartRowsTests`** (8 tests): re-read fresh
  independent of the diff-review pass. `test_valid_nrfjprog_output_produces_rows` and
  `test_valid_stm32_programmer_output_produces_rows` assert on real parsed field values
  (`port_path`, `provenance`, count) against realistic multi-line fixture text — genuinely
  load-bearing (confirmed in the narrow review by mis-dispatching the adapter and watching
  the STM32 test fail against nrfjprog-shaped output). `test_executable_absent_skips_spec`
  asserts `run_cmd_called == []`, a direct behavioral observation, not an output-shape
  inference — strong. The two previously-vacuous exit-code tests are now confirmed
  load-bearing (D18 re-verification above).
- **`tests/test_phase2_uncovered.py`'s other six classes**
  (`NormalizePortNameTests`, `DecodeHookStdoutTests`, `ParseHookDeclarationTests`,
  `ResolveDeclarationTests`, `RetryContextTests`, `DiscoveryFailureTests`) — these predate
  the D15/M6/D17 chain (Phase 2 straggler tests, never previously adversary-reviewed since
  Phase 1's loop was scoped to the feature implementation, not the straggler tests written
  after it). Read all six classes in full. All exercise real parsing/validation logic with
  concrete, non-tautological assertions (e.g. `RetryContextTests::
  test_retry_call_deep_copies_arguments` mutates the *returned* structure and asserts the
  *original* is unchanged — a genuine deep-copy proof, not a shape check).
  `DecodeHookStdoutTests::test_valid_utf8_json_output`'s name overstates what it tests
  (its own docstring admits the call is expected to raise `DiscoveryHookError` for schema
  reasons, not succeed) — a naming/clarity nit, not a defect, and explicitly the kind of
  thing the policy directs effort away from. Not filed as a numbered finding.
- **`tests/test_swd_process_isolation.py`'s D16 change** — re-read the current diff
  against base (`git diff 418b17d..8a51138 -- tests/test_swd_process_isolation.py`)
  fresh. Construction is outside `assertRaises`, and the added
  `self.assertEqual(client._request_id, 1)` proves `.call()` was actually entered before
  raising, closing the exact gap D16 identified. Not re-broken this round (already proven
  twice: once when D16 was filed, again in the narrow review's adversarial stress test) —
  re-reading the code was sufficient to confirm the fix that was verified twice before is
  still present and unchanged (`git diff` shows this file untouched since `8a51138`, which
  is HEAD).

---

## Independent sweep, diff-blind

Spent remaining budget looking for anything a fresh reader would flag that the guide-vs-
diff lens might not surface, per the policy's priority list (wrong hardware, wrong data,
corrupted state, crash, hang):

- **Hang/crash surface of `vendor_uart_rows`**: it calls exactly one `_run_cmd(...)` per
  `SERIAL_FALLBACKS` spec, each independently bounded by
  `DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS` (30s) inside `run_owned`'s own `timeout=`
  parameter — a hung vendor CLI cannot hang the server past that per-spec ceiling (`_run_cmd`
  catches `subprocess.TimeoutExpired` and returns exit code 124). No unbounded wait
  possible here regardless of the operation-timeout budget question (D17/D20's subject is
  *reporting* the cost accurately to the A-11 cancellation machinery, not an actual
  uncapped hang).
- **Wrong-hardware-selection surface**: traced whether a vendor row could ever be
  *selected* and *opened* as if it were a different, stable-identity endpoint. Session-scope
  UART selection (`_resolve_recorded_uart`/`_record_uart_selection`, `server.py`,
  unchanged since iteration 5's step-7 review) treats every session-scope row uniformly
  regardless of provenance string — a vendor row gets exactly the same
  re-resolve-against-a-fresh-snapshot treatment as a session-scope hook row, with the same
  "more than one candidate → refuse, route to friendly selection" behavior. No
  vendor-specific branch exists that could special-case (and get wrong) a vendor row's
  handling.
- **Data-corruption surface**: `VendorUartRow`/`UartRow` are both frozen, slotted
  dataclasses; `_merge_uart_rows`/`_combine_uart_rows` build new instances rather than
  mutating, and `InventorySnapshot` itself is frozen. No path for one snapshot's vendor
  rows to leak into or mutate a later snapshot's state.

No new finding from this sweep.
