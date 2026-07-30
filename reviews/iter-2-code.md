# Iteration 2 — Code Adversary

Scope: the resulting code as it stands after `d4b1a14`. Fresh pass, hardest scrutiny on
whether the six iteration-1 fixes are correct/complete, and on `server.py` wiring
generally. IDs continue from iteration 1 (`C1`-`C6`); this file adds `C7`-`C11`.

## Summary

| Severity | Count |
| --- | --- |
| CRITICAL | 1 |
| HIGH | 1 |
| MEDIUM | 2 |
| LOW | 1 |

**On the iteration-1 fixes specifically:** all six are correct in isolation and each
closes exactly the mechanism its finding described (verified by reading the code, not
the ledger's prose):

- FIX 1 (`uart_snapshot()`): genuinely shares `_collect_uart_rows()` with `snapshot()`
  — confirmed no logic duplication, confirmed the UART hook gate cannot diverge between
  the two entry points. `snapshot.probes` is hardcoded empty on the UART-only path and
  I confirmed (via `grep -n "snapshot\.probes"` across `server.py`) that nothing
  downstream of `_resolve_serial_port_for_session` ever reads it.
- FIX 2 (bounded `ProbeSelectionStore`): the cap/eviction/re-record-refreshes-position
  mechanics are all correct as implemented and are exercised by real, non-tautological
  tests (`tests/test_probe_selection_records.py::BoundedStoreTests`). *However* — see
  C7/C8/C9 below — bounding this store surfaces a pre-existing gap in the store's own
  fallback path that iteration 1 (my own review) missed, and makes that gap's fallback
  path reachable in a case (eviction) it wasn't originally reachable in.
- FIX 3(a)(b)(c): each of the three unhandled-exception paths from C3 is now closed.
  Confirmed `except Exception` (not `except BaseException`) is used throughout, so
  `KeyboardInterrupt`/`SystemExit` still propagate; confirmed `cancellation_checkpoint()`
  is never called anywhere inside `discovery_hooks.py` or `hardware_inventory.py`
  (`grep -rn cancellation_checkpoint` across both files: zero matches), so
  `OperationCancelledError` (a `RuntimeError`/`Exception` subclass) cannot originate
  from inside any of the three new `try` blocks in the first place — there is nothing
  for the new broad catches to swallow on that front.
- FIX 4 (`MAX_HOOKS_TOTAL`): correctly closes the sequential-execution-time cliff. See
  C10 for a narrower, lower-severity gap in *when* the cap is checked.
- FIX 6 (`hooks_available` removed): fully removed; `grep -rn hooks_available src/
  tests/` shows only a docstring mention explaining the removal.
- FIX 7 (retry-store test rename): the new
  `test_refresh_is_kind_agnostic_by_design_and_accepts_any_valid_ticket` genuinely
  calls `refresh(...)` and asserts success, which is what the iteration-1 finding said
  was missing.

The one real problem this iteration is **C7/C8**: not a defect *introduced* by any of
the six fixes, but a pre-existing gap in the same connection-identity area (step 5) that
I did not find in iteration 1, surfaced now by looking harder at exactly the interaction
the coordinator asked about.

---

## C7 — CRITICAL — `_setup_overview` silently drops a physically distinct probe when two providers report the same UID text

**File:** `src/pyocd_debug_mcp/server.py` (`_setup_overview` connection-row loop), `src/pyocd_debug_mcp/services/connections.py:20-23`

`_setup_overview` mints each row's `connection_id` from UID text alone —
`probe_connection_id(probe.usb_serial)` — with no provider in it. The unified inventory
layer (`HardwareInventoryService._matching_probe_index`) correctly keeps two different
providers' rows distinct even when their UID text is identical (this is a guide-mandated
invariant, tested at that layer by
`tests/test_unified_inventory.py::test_two_providers_with_identical_uid_text_stay_distinct`).
But `_setup_overview` collapses them right back together, because its dedup key
(`_setup_connection_key(connection_id)`) is derived from that same provider-blind
string:

```python
for probe in inventory.probes:
    connection_id = probe_connection_id(probe.usb_serial) if probe.usb_serial is not None else probe.probe_id
    key = _setup_connection_key(connection_id)
    if key in connection_rows_by_identity:
        continue          # <-- the second provider's row is silently discarded here
    ...
```

**Reproduction** (ran directly against the shipped code, not a hypothetical):

```python
from pyocd_debug_mcp.hardware_inventory import ProbeRow, InventorySnapshot, validation_inventory_from
from pyocd_debug_mcp.probe_inventory import EMPTY_NATIVE_PROBE_LISTING
from pyocd_debug_mcp.services.connections import probe_connection_id

def row(provider, uid):
    return ProbeRow(provider=provider, probe_id=uid, unique_id=uid, row_id="r-"+provider,
        description=provider+" probe", stable_identity=uid, provenance=("native",),
        hook_source_sha256=None, identity_scope="stable", snapshot_id="s")

snapshot = InventorySnapshot(snapshot_id="s", probes=(row("cmsisdap","12345"), row("jlink","12345")),
    uarts=(), native_probe_diagnostics=EMPTY_NATIVE_PROBE_LISTING, native_uart_available=True, hook_diagnostics=())
inventory = validation_inventory_from(snapshot)
for probe in inventory.probes:
    print(probe.probe_family, probe_connection_id(probe.usb_serial)))
```
```
cmsisdap probe:12345
jlink    probe:12345
```

Both rows produce `connection_id == "probe:12345"`. `_setup_overview` only shows one of
them in `connections[]`; whichever provider's row happens to sort/iterate second in
`inventory.probes` (order depends on native-listing/hook-merge order — not something a
caller controls or can predict) is invisible to the agent for the rest of that
snapshot's lifetime. There is no way to select, set up, or validate the hidden probe.
This is not the "ambiguity" case the guide explicitly carves out for the existing
friendly-selection flow (`preflight.py:290-305`) — ambiguity presents *both* choices;
this silently removes one.

**Impact:** in a domain where the entire point of the exercise is "open the physical
device the user actually attached," silently substituting one real, connected debugger
for a different one because their UID text happens to collide is a safety-relevant
defect, not a cosmetic one. J-Link's decimal UIDs (subject to the codebase's own
leading-zero normalization) and short/simple UIDs from DIY or clone probes make this
more than a theoretical concern.

**Suggested fix direction:** mint `connection_id` from `(provider, unique_id)`, not UID
text alone — e.g. `f"probe:{provider}:{uid.strip().casefold()}"` — and update the five
sites that parse it accordingly. This is exactly the kind of single-canonical-minting-site
work step 5/`probe_connection_id` already did once for the casefold issue; it did not
go far enough to also disambiguate by provider.

---

## C8 — HIGH — `derive_selection_from_token` has the same provider-blindness, on the resolution side

**File:** `src/pyocd_debug_mcp/hardware_inventory.py:752-776`

```python
def derive_selection_from_token(connection_id, snapshot):
    candidate = connection_id.strip()
    if candidate.casefold().startswith("probe:"):
        candidate = candidate.split(":", 1)[1]
    for row in snapshot.probes:
        if stable_identity_equal(candidate, row.probe_id) or stable_identity_equal(candidate, row.stable_identity):
            return ProbeSelection.from_row(connection_id, row)   # <-- no provider filter
    ...
```

Contrast with `find_selected_row` (`hardware_inventory.py:779-793`), used for the
*recorded* path, which correctly filters by provider first:

```python
def find_selected_row(selection, snapshot):
    for row in snapshot.probes:
        if row.provider != selection.provider:
            continue
        ...
```

`derive_selection_from_token` is `ProbeSelectionStore.resolve()`'s fallback when
`self.recorded(connection_id) is None` (`hardware_inventory.py:649-663`). Verified
empirically:

```python
snapshot = InventorySnapshot(..., probes=(row("cmsisdap","12345"), row("jlink","12345")), ...)
derive_selection_from_token("probe:12345", snapshot).provider
# -> 'cmsisdap' (first match wins, silently)
```

If the token actually referred to the `jlink` probe (e.g. that's the physical device
originally selected before the recorded entry aged out), the derived selection is wrong,
and `_resolved_probe_uid_for_connection`/`_assigned_probe_uid_for_connect` will hand
pyOCD the UID with the wrong provider association attached — same class of consequence
as C7.

**On reachability, since FIX 2 (`ProbeSelectionStore` bounding) is directly relevant
here:** before FIX 2, this fallback fired only for a connection_id that had genuinely
never been recorded in this store (the comment's stated intent: "an assignment can
predate this store"). After FIX 2, it *also* fires for a connection_id that **was**
recorded and then evicted — and since eviction is oldest-insertion-first
(see C9) rather than least-recently-*used*, a board's selection can be evicted purely
because ~256 *other* connection_ids were recorded elsewhere in the run, regardless of
how actively that board's own connection is still being used. FIX 2 does not create
this gap, but it does add a new, non-obvious way to reach it, which is exactly the
coordinator's question — the answer is: yes, it can, and the consequence when it does
is silent misidentification, not a clean error.

No test in `tests/test_probe_selection_records.py` (including the new
`BoundedStoreTests`) constructs a cross-provider UID collision through the derive path;
`test_two_providers_with_identical_uid_text_stay_distinct` in
`tests/test_unified_inventory.py` only exercises the merge layer, not resolution.

---

## C9 — MEDIUM — `ProbeSelectionStore` eviction is insertion-order, not access-order; a successful `resolve()` does not protect an in-use entry

**File:** `src/pyocd_debug_mcp/hardware_inventory.py:642-684` (`resolve`)

```python
row = find_selected_row(recorded, snapshot)
if row is None:
    raise SelectionDisappeared(...)
...
return ProbeSelection.from_row(connection_id, row)   # <-- no self.record() here
```

The only two places that touch the `OrderedDict`'s recency ordering are `record()`
itself (called from `_setup_overview`'s row loop, and from the derive-fallback inside
`resolve()`) and eviction. A *successful* resolution against an already-recorded entry
(the common case — every `connect`, `board_validate`, and status check for an
already-set-up board) does not call `record()` again, so it does not move that entry to
the back of the `OrderedDict`. This means an actively-used, long-lived board connection
is **not** protected from eviction by its own continued use — only by `_setup_overview`
happening to be called again for it before ~256 unrelated connection_ids accumulate
elsewhere. This is the mechanism that makes C8's eviction path realistic rather than
theoretical: normal LRU semantics would have made an actively-resolved entry among the
least likely to be evicted; this implementation makes it no more protected than an
entry nobody has touched since it was created.

Suggested fix direction: have the `row is not None` branch in `resolve()` call
`self.record(...)` (or an equivalent recency-touch) on success, matching the "re-recording
refreshes position" behavior `record()` already documents and tests for the explicit-record
case.

---

## C10 — MEDIUM — `MAX_HOOKS_TOTAL` is checked after all declarations are resolved and hashed, not before

**File:** `src/pyocd_debug_mcp/discovery_hooks.py:598-665`

See diff-review D9 for the full description; restated briefly for the code-as-it-stands
lens: a manifest pair at the individual per-source cap (32 + 32 = 64) causes
`load_hook_snapshot` to perform 64 `resolve_declaration` calls — each doing symlink-safe
path containment resolution and a full SHA-256 hash of up to `MAX_HOOK_FILE_BYTES` (1MB)
— before the aggregate check rejects the result. Worst case: up to ~64MB of file I/O and
64 filesystem stat/resolve round-trips on every `refresh_discovery_hooks` call against
such a manifest, repeatable by anyone (or anything) that can write into the project's
`.firm/discovery_hooks/` directory or point `BYO_MCP_DISCOVERY_HOOK_REGISTRY` at a large
operator registry. Not the sequential-subprocess-time cliff FIX 4 targets (that remains
fixed — nothing executes), but a real, uncapped-until-the-end I/O cost that a
count-first check (count `declarations` from both manifests and reject before the
per-declaration resolve/hash loop) would avoid entirely.

---

## C11 — LOW — the FIX 3b backstop in `refresh_discovery_hooks` returns a payload missing `retry_id`/`refresh_call`/`board_id`

**File:** `src/pyocd_debug_mcp/tools/discovery.py:359-385`

See diff-review D10 for the full write-up. From the code-correctness angle: the early
`return _json({...})` inside the new `except Exception` handler is constructed
independently of the `ticket is not None` block at the bottom of the function
(lines 406-417), so it does not carry the fields that block would have attached. Since
`execute_hook` (FIX 3a) now absorbs essentially every exception this backstop exists
for, it is unlikely to fire against the real `run_hooks` wiring in production — but as
written, a test double or future refactor that does trigger it gets a materially thinner
response than the equivalent failure reached through the normal `executions`-based path.

---

## Areas re-checked this iteration with no findings

- **`uart_snapshot()` / `snapshot()` divergence:** none. `_collect_uart_rows()` is
  called by both, verified by direct reading (not by trusting the docstring) that no
  UART-related logic exists outside that shared method in either caller.
- **Cancellation swallowing:** `cancellation_checkpoint()` is not reachable from inside
  any of the three new `except Exception` blocks (confirmed by grep across
  `discovery_hooks.py`/`hardware_inventory.py`: zero calls). `KeyboardInterrupt`/
  `SystemExit` are `BaseException`-only and still propagate through every new
  `except Exception` in this diff.
- **`launch_failed` outcome exhaustiveness:** `HookExecution.ok`, `.failure_code`
  (falls back to `discovery/hook-failed`, an existing, valid code), `.diagnostic_row()`,
  and every `HookOutcome` string comparison in `discovery_hooks.py`/`server.py`/
  `tools/discovery.py` handle the new value correctly; no exhaustive match was left
  unaware of it.
- **New test quality:** spot-checked `LaunchFailureTests`,
  `ActiveConnectionRowsToctouTests`, `AggregateHookCapTests`, and
  `HotPathNativeProbesTests` (the `uart_snapshot()` regression class in
  `test_hook_gating_and_budget.py`). All construct real scenarios that fail against the
  pre-fix code (verified for several by reading what the pre-fix code would have done),
  none mock so heavily they only assert their own mock, and the ones using real hook
  fixtures (`AggregateHookCapTests`) genuinely exercise path resolution and hashing
  rather than short-circuiting on a missing file. No test in this batch silently
  exercises the physical bench — all patch `server._hardware_inventory` (or construct a
  bare `HardwareInventoryService`) rather than relying on the unpatched
  `_validation_inventory` legacy shape, which is exactly the trap
  `reviews/phase0-notes.md` warns about.
- **K1 TOCTOU fix (`maybe_connection`):** correct and completely closes the race as
  described; the new `ActiveConnectionRowsToctouTests` test would fail against the
  pre-fix `connection_for`-based code.
