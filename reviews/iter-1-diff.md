# Iteration 1 — Diff Adversary

Scope: commit range `a633438^..HEAD` (base `6f3da0a`) against
`DEBUGGER_UART_DISCOVERY_HOOK_IMPLEMENTATION_GUIDE.md` and
`DEBUGGER_UART_DISCOVERY_HOOK_PLAN.md`. `probe_families.py`,
`test_preflight_probe_guidance.py`, `test_probe_cli_command.py` excluded per
instructions. `setup_flow/preflight.py` reviewed only for this change's edits.

567 tests pass, ruff clean — reproduced independently.

## Summary

| Severity | Count |
| --- | --- |
| CRITICAL | 1 |
| HIGH | 2 |
| MEDIUM | 2 |
| LOW | 2 |

---

## D1 — CRITICAL — Invariant 2 broken: every UART action now launches a probe-listing subprocess

**File:** `src/pyocd_debug_mcp/server.py:1614-1631`, `src/pyocd_debug_mcp/hardware_inventory.py:231-237`

The guide states three invariants to hold through every step (`DEBUGGER_UART_DISCOVERY_HOOK_IMPLEMENTATION_GUIDE.md:36-46`), the second of which is:

> 2. With no manifest present, every code path must behave byte-identically to today.

Guide §1.1 site 8 describes the pre-change `_resolve_serial_port_for_session` as **"pyserial only"** — confirmed against the base commit:

```
$ git show 6f3da0a:src/pyocd_debug_mcp/server.py | sed -n '1488,1500p'
def _resolve_serial_port_for_session(...):
    board = _require_loaded_board(handle)
    ports = list_serial_ports()
    ...
```

No pyOCD/probe CLI call anywhere in it.

The new implementation (`server.py:1614`) opens with:

```python
snapshot = _hardware_inventory.snapshot()
```

and `HardwareInventoryService.snapshot()` (`hardware_inventory.py:231-242`) unconditionally does:

```python
native_listing = self.native_probes()          # line 237 — always runs, no gate
probe_rows = self._native_probe_rows(native_listing, counter)
native_ports = self.native_uarts()
...
```

`native_probes` is wired in `server.py` (around line 2990 in the current file) as:

```python
native_probes=lambda: list_connected_probes_detailed(_run_cmd),
```

`list_connected_probes_detailed` runs every command from `configured_probe_cli_commands()` through `_run_cmd` — a real, non-hook subprocess (typically `sys.executable -m pyocd list --probes`), with up to `DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS` (30s) per configured command.

Consequence: `read_serial`, `write_serial`, `serial_exchange`, and the `on_exit` UART finalizer — none of which touched pyOCD before this change — now spawn a probe-enumeration subprocess on **every single call**, even with zero hooks configured, even on a board that has no debug probe at all. This is not a hook cost (the §0 gating rule correctly keeps *hook* subprocesses off this path); it is the *native* probe listing that got pulled in as a side effect of routing through the unified snapshot.

The new docstring on the same function claims the opposite:

```python
# server.py:1627
Under the §0 gating rule a UART hook runs only when pyserial reports nothing, so a
machine with a working native port pays no subprocess cost here at all.
```

This is false as written — the function pays a probe-CLI subprocess cost unconditionally, regardless of hooks.

**Reproduction:** patch `configured_probe_cli_commands` to return a command that sleeps 10s, then call `read_serial` on any board with `read_seconds=1`. Before this change, `_resolve_serial_port_for_session` would return almost immediately (pyserial only). After this change, it blocks for the probe-listing subprocess's duration first. No test in the suite exercises `_resolve_serial_port_for_session` at all (`grep -rn "_resolve_serial_port_for_session" tests/` returns nothing), and the "byte-identical with no hooks" regression class (`NoHookConfigurationTests` in `tests/test_discovery_hook_safety.py`) only asserts the *timeout formula* (`test_every_operation_budget_is_unchanged_with_no_hooks`), never that the function's actual subprocess behavior is unchanged. The regression is real and untested.

See code-review finding C1 for the runtime-behavior write-up of the same defect.

---

## D2 — HIGH — UART tool timeout budget doesn't cover the new probe-listing call (Trap 10, reintroduced through a different door)

**File:** `src/pyocd_debug_mcp/kernel/operations.py:75, 603-622`

Trap 10 in the guide is explicitly about this exact failure mode for *hooks*:

> `read_serial` with `read_seconds=3` yields 8 s against a 10 s hook allowance. Missing this makes UART unusable whenever a hook actually runs.

The implementer correctly added `_hook_budget("uart")` to `_UART_ACTION_TOOLS`' budget (`operations.py:621-622`, and inside each of the `read_serial`/`write_serial`/`serial_exchange` early-return branches). But D1 shows that `_resolve_serial_port_for_session` now also unconditionally calls the *native* probe-listing subprocess (via `_hardware_inventory.snapshot()`), and that cost is only budgeted for tools in `_PROBE_INVENTORY_TOOLS` (`operations.py:603-613`, `+ len(configured_probe_cli_commands()) * (DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS + MAX_OWNED_PROCESS_CLEANUP_SECONDS)`). `_UART_ACTION_TOOLS` is a disjoint set and never receives this addend.

Net effect: a `read_serial(read_seconds=3)` call resolves to `max(default, 3+grace) + _hook_budget("uart")` — with zero hooks configured, that's just `max(default, ~5s)`. If the probe-listing command the snapshot now silently invokes takes 10-30s (slow enumeration, a misbehaving driver, or simply a multi-command fallback list), the operation is cancelled by its own deadline before the UART read that was actually requested even starts — the identical symptom Trap 10 was written to prevent, caused by the same call chain, just via native listing instead of a hook.

---

## D3 — HIGH — Guide's explicit "append, don't replace" instruction violated for the probe branch (honored for UART, two lines below)

**File:** `src/pyocd_debug_mcp/setup_flow/preflight.py:283-309` (probe branch) vs. `:336-347` (UART branch)

Guide, step 6:

> `tests/test_preflight_probe_guidance.py` asserts on that exact prompt string — keep the existing sentence and append, don't replace.

Base commit's probe-missing sentence (verbatim):

```
"No compatible debug probe is currently visible. Tell the user to attach "
"the intended board and retry; do not begin documentation research."
```

Current code (`preflight.py:285-291`):

```python
message = (
    "No compatible debug probe is currently visible. Tell the user to attach "
    "the intended board and retry. If it remains absent, from the BYO Server "
    "checkout run `uv run --locked python -m pyocd list --probes`; this checks "
    "whether the server's locked Python environment can enumerate the USB "
    "debugger. Do not begin documentation research."
)
```

The original sentence was not preserved: `"retry;"` became `"retry."`, new content was spliced in *before* the trailing clause, and `"do not begin..."` was moved and re-capitalized to `"Do not begin..."`. The substring `"the intended board and retry; do not begin documentation research."` no longer exists anywhere in the output. This is a rewrite, not an append.

Contrast with the UART branch two dozen lines below (`preflight.py:337-346`), which does it correctly — the original sentence is left completely intact and the new guidance is appended as a new sentence afterward:

```python
uart_message = (
    "The intended setup requires a serial port, but none is visible. Tell "
    "the user to attach or enable the board UART and retry; do not research."
)
if current.uart_hook_contract_call is not None:
    uart_message += (
        " If none appears and the user can still see the port in a vendor "
        ...
    )
```

`test_preflight_probe_guidance.py` is out of scope for this review (untracked, someone else's file per the task brief), so I cannot confirm whether it currently fails against this string — but the guide's instruction was unambiguous, the UART sibling shows the implementer knew the correct pattern, and the probe branch does not follow it. This is a genuine, precisely located guide-conformance defect, independent of whether any test currently catches it.

---

## D4 — MEDIUM — J-Link UID-less-retry hook-immunity requirement covered only by a source-text substring test, not a behavioral one

**File:** `tests/test_unified_inventory.py:684-699`

Guide Trap 7 / step 4:

> Do not feed hook rows to the J-Link UID-less retry ... and add a test asserting a hook row does not change its verdict.

The only test addressing this is `test_the_retry_condition_consults_native_discovery_only`:

```python
source = inspect.getsource(swd_pyocd._single_matching_probe_visible_for_board_family)
self.assertIn("list_connected_probes_cli", source)
self.assertIn("get_all_connected_probes", source)
for forbidden in ("hardware_inventory", "HardwareInventoryService", "snapshot", "hook"):
    self.assertNotIn(forbidden, source, ...)
```

This never actually constructs a scenario with a hook-discovered probe row present and checks that `_should_retry_without_uid` / the J-Link retry verdict is unaffected — it inspects the *function's source text* for forbidden identifiers. It is a reasonable static guard against someone wiring the inventory service in later, but it is not the behavioral regression test the guide asked for, and it would not catch, for example, a refactor that routes hook data into the same function through a differently-named parameter or an existing variable that happens not to contain the literal substrings checked. `test_unified_inventory.py:701-705` (checking `hasattr`) is the same style of proxy test.

---

## D5 — MEDIUM — Guide's "stay green untouched" instruction for two existing test files was not honored (justified, but not disclosed as a deviation)

**Files:** `tests/test_server_assignment_connect.py`, `tests/test_validation_honesty.py`

Guide test plan, step 0:

> `tests/test_server_assignment_connect.py` and `tests/test_validation_honesty.py` must stay green untouched — they exercise the comparison helpers whose inputs step 0b changes.

Both files were modified (`git diff --stat` shows `+8` and `+8/-8` respectively). Unlike D1/D2 discovered independently, this one *is* disclosed in `reviews/phase0-notes.md` under "Test contract changes required by step 5 (not test rot)," with a specific justification: step 5 deletes the "`connection_id.removeprefix('probe:')` is always the UID" assumption these tests exercised, so their expected values legitimately change shape (`session:hardware-uid` → `probe:session:hardware-uid`).

I reviewed both diffs directly rather than accepting the note at face value. The changes are narrow and consistent with that justification — `test_validation_honesty.py` only updates the literal token asserted, not the assertion's intent (two boards still cannot be conflated, mismatch case still checked); `test_server_assignment_connect.py` only adds the two `ConnectionManager` double methods (`assigned_board_ids`, `connection_for`) the real class has always had, which the new `_active_connection_rows()` call site now requires. Neither change weakens what the test verifies. Rated MEDIUM rather than dismissed, because the guide's instruction was unconditional ("untouched") and not "unless step 5 requires it" — this is technically a deviation from an explicit written constraint, even though the specific deviation is defensible on inspection.

---

## D6 — LOW — `hooks_available` parameter is always `True`; dead conditionality

**File:** `src/pyocd_debug_mcp/discovery_failures.py:129, 158`; call sites `server.py:4532, 4812` (grep confirms these are the only two production call sites, plus test call sites)

`no_native_probe_failure(hooks_available=...)` / `no_native_uart_failure(hooks_available=...)` gate whether `hook_contract_call` is included in the failure payload. Every call site in the codebase (production and tests) passes `hooks_available=True` unconditionally — `grep -rn "hooks_available" src/ tests/` shows no call site ever passes `False`. The parameter exists and is documented via the type as meaningful, but nothing in the implementation ever computes it from whether hooks are actually configured or reachable; it is indistinguishable from a constant. Not a functional bug (the guide's intent — always offer the contract call when native discovery is empty — is met), but it is dead API surface that misleads a reader into thinking the field is conditional.

---

## D7 — LOW — Misleadingly named retry-store test doesn't exercise what its name claims

**File:** `tests/test_discovery_retry_store.py:228-236`

```python
def test_a_refused_wrong_kind_ticket_runs_nothing_through_refresh_either(self) -> None:
    ticket = self.store.issue("probe")
    # The ticket is valid for refresh (which is kind-agnostic), so prove the
    # contract-level refusal is what stops execution, not luck.
    payload = self.contract("uart", ticket.retry_id)
    ...
```

Despite the name ("...through refresh either") and the guide's own test-plan wording ("A `retry_id` from `get_discovery_hook_contract(kind="probe")` is refused by `refresh_discovery_hooks` if presented for a UART contract instead"), this test never calls `self.refresh(...)` — it calls `self.contract("uart", ...)` a second time, duplicating `test_a_probe_ticket_presented_for_a_uart_contract_is_refused` immediately above it. Because `refresh_discovery_hooks` takes no `kind` argument at all (by design — it is kind-agnostic per the plan's own tool contract: "Input: the opaque run-scoped `retry_id`"), a same-named test that actually called `refresh(ticket.retry_id)` would find it *succeeds* (refresh doesn't validate kind), which is presumably why the test as written avoids calling it. The comment even says "the ticket is valid for refresh," confirming the author knew this but wrote a test whose name claims otherwise. This isn't wrong behavior, but it's a test that asserts something different from what its name and the guide's test-plan sentence describe — precisely the "looks thorough, doesn't test what it claims" pattern this review was asked to hunt for.

---

## Traps and acceptance-table cross-check

Walked all 15 traps in guide §4 and the §6 acceptance-mapping table. Findings beyond the above:

- Traps 1-3, 5, 6, 8, 9, 11-15: implemented as specified; spot-checked each against source (capped reader threads in `discovery_hooks.py:_execute`, step-0a/0b fixes in `server.py`, env read inside `load_hook_snapshot` not at import, `PROBE_CLASSES` used as truth in `probe_inventory.registered_provider_ids`, provider-callable timeout budget in `operations.py`, session-local UART kept out of `AttachmentCache` via `_record_uart_selection`'s scope check, `forbid_unknown_tool_arguments` wired for both new tools, `.gitignore` untouched).
- Trap 4 (connection_id casefold) / Trap 7 (J-Link retry): see D4 above for the weak test coverage on Trap 7; Trap 4's fix (`probe_connection_id`) is correctly threaded through all three server.py sites that needed it.
- Acceptance row "Hooks never run on a machine where native discovery works": true for *hook* subprocesses (gating rule correctly implemented in `hardware_inventory.py:250-251`), but see D1 — the row's spirit ("no path gains subprocess latency," guide §0) is violated by the *native* probe-listing call newly reachable from UART actions.
- No scope creep found: no built functionality beyond what the guide specifies. `tests/discovery_hook_fixtures.py` (declared as D1 in phase0-notes, the implementer's own deviation) is a plain test helper with no execution-path mocking — reasonable, not flagged as a concern.
