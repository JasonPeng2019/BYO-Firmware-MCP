# Iteration 2 — Diff Adversary

Scope: fresh pass against the guide, focused on `git diff af37870..HEAD` (the 11
adjudicated fixes in `d4b1a14`) plus a deeper look at areas iteration 1 only briefly
checked. `reviews/ledger.md` read in full first. 584 tests pass, ruff clean —
reproduced independently.

IDs continue from iteration 1 (`D1`-`D7`); this file adds `D8`-`D10`.

## Summary

| Severity | Count |
| --- | --- |
| CRITICAL | 1 |
| HIGH | 0 |
| MEDIUM | 2 |
| LOW | 1 |

**Iteration-1 fix verdict, up front:** the six code changes in `d4b1a14` (FIX 1-4, 6, 7)
each do exactly what the ledger claims and are each backed by a real regression test
that fails against the pre-fix code (verified by reading the pre/post diffs directly,
not by trusting the ledger's description). FIX 5 (the J-Link behavioral test) is a
genuine strengthening, not a token addition. None of iteration 1's fixes are wrong. D8
below is not a regression *introduced* by a fix — it is a pre-existing gap in the same
area (`connection_id` construction from step 5) that I failed to find in iteration 1
and only surfaced by digging harder into the exact interaction the coordinator asked
about (`ProbeSelectionStore` eviction). Flagging that plainly rather than dressing it up
as a new regression.

**D3 and D5 (iteration 1, ruled invalid):** independently re-verified. `git show
6f3da0a:src/pyocd_debug_mcp/setup_flow/preflight.py` confirms the base commit does not
contain the locked-environment sentence — D3 was a genuine misattribution on my part,
correctly ruled invalid. Not re-raising it.

---

## D8 — CRITICAL — `setup_overview` can silently drop a physically distinct probe when two providers report the same UID text

**File:** `src/pyocd_debug_mcp/server.py` (`_setup_overview`'s connection-row loop, ~line 4900), `src/pyocd_debug_mcp/services/connections.py:20-23` (`probe_connection_id`)

Guide step 4 states the merge rule as a hard requirement:

> Two providers reporting identical UID text stay distinct rows: the text is a
> provider-scoped selector, not a global identity.

and the guide's own test plan (§5, `test_unified_inventory.py`) lists "Two providers
with identical UID text remain distinct" as a required case. This *is* honored at the
`HardwareInventoryService` merge layer — `_matching_probe_index` correctly filters by
`row.provider` before comparing UID text, and `tests/test_unified_inventory.py:300-307`
(`test_two_providers_with_identical_uid_text_stay_distinct`) proves it holds at that
layer.

It is not honored at the layer an agent actually sees. `_setup_overview` builds each
row's `connection_id` from UID text alone:

```python
connection_id = (
    probe_connection_id(probe.usb_serial) if probe.usb_serial is not None else probe.probe_id
)
```

`probe_connection_id(uid) = f"probe:{uid.strip().casefold()}"` — no provider in the
string. Two `ProbeRow`s that the merge layer correctly kept distinct (different
`provider`, same `unique_id` text) produce the *identical* `connection_id`, and
`_setup_overview`'s own dedup (`if key in connection_rows_by_identity: continue`)
then drops the second one from `connections[]` entirely. Verified directly against the
adapter the function actually calls:

```
$ PYTHONPATH=src .venv/Scripts/python.exe -c "..."
Distinct ProbeRow count in snapshot: 2
ValidationProbe count after adapter: 2
probe_family='cmsisdap' usb_serial(unique_id)='12345' -> connection_id='probe:12345'
probe_family='jlink' usb_serial(unique_id)='12345' -> connection_id='probe:12345'
Distinct connection_id values an agent would see: 1 {'probe:12345': 'cmsisdap'}
```

Two genuinely different, simultaneously-connected physical debuggers collapse to one
agent-visible choice. Whichever one is *not* shown cannot be selected, set up, or
validated at all in that run — not an ambiguity the friendly-selection flow can resolve
(the guide's "ambiguity is not a hook case" carve-out does not apply; this isn't
ambiguity, it's silent loss of a choice).

This is pre-existing (introduced with `probe_connection_id`/step 5, untouched by
iteration 1's fixes) and I missed it in iteration 1. Full reproduction, the resolution
half of the same gap (`derive_selection_from_token`), and a fix direction are in
`reviews/iter-2-code.md` findings C7/C8.

---

## D9 — MEDIUM — `MAX_HOOKS_TOTAL` (FIX 4) is enforced after, not before, the expensive per-hook work

**File:** `src/pyocd_debug_mcp/discovery_hooks.py:598-665` (`load_hook_snapshot`)

FIX 4's own comment states the goal plainly: "Refuse before anything executes rather
than let the aggregate cliff be reachable at all." That's true for *execution*
(subprocess spawn) — nothing runs before the cap check. But `load_hook_snapshot`
resolves and hashes every declaration from both sources into `specs` *before* the
`if len(specs) > MAX_HOOKS_TOTAL: raise` check at the end:

```python
for declaration in declarations:               # project source, up to 32
    specs.append(resolve_declaration(declaration, root=root, source="project"))
...
for declaration in declarations:               # operator source, up to 32
    specs.append(resolve_declaration(declaration, root=operator_root, source="operator"))
...
specs.sort(...)
if len(specs) > MAX_HOOKS_TOTAL:                # only checked here, after all 64 resolved+hashed
    raise DiscoveryHookError(...)
```

`resolve_declaration` does path-containment resolution (`_contained_entrypoint`, including
a `realpath` symlink check) and a full content hash (`_hash_hook_file`, up to
`MAX_HOOK_FILE_BYTES = 1MB` read per file) for *every* declaration, regardless of
whether the total will end up over the cap. A maximal 32+32 manifest pair — the exact
scenario the guide's arithmetic and FIX 4's own test
(`test_two_individually_maxed_manifests_are_refused_in_aggregate`) construct — pays up
to 64 file-hash reads (up to 64MB of I/O in the worst case, each file at the 1MB
ceiling) on every single `refresh_discovery_hooks` call before being rejected. This
does not reopen the sequential-execution-time cliff FIX 4 was written to close (no
subprocess is ever spawned for the rejected set), so the fix is not wrong, but its
stated rationale ("before anything executes") overstates what it actually bounds. See
code review C10 for the concrete numbers.

---

## D10 — LOW — the new defensive `except Exception` around `run_hooks` in `refresh_discovery_hooks` (FIX 3b) returns an incomplete payload compared to the normal failure path

**File:** `src/pyocd_debug_mcp/tools/discovery.py:359-385`

FIX 3b's backstop (`except Exception as exc: return _json({"status": "discovery_refresh_rejected", "code": "discovery/hook-failed", ...})`) is a correct and necessary safety net, but the payload it returns omits `retry_id`, `refresh_call`, and `board_id` — fields the *normal* partial-failure path (lines 406-417, reached when `executions` collects failures without an exception) always attaches when a ticket is present. An agent hitting this specific backstop still has the `retry_id` it originally passed as an argument, so it is not stranded, but it loses the `refresh_call`/`board_id` breadcrumbs the guide's step 8 table asks every hook-failure response to carry ("name that hook's friendly ID, failure class, and exact repair/retry call"). Low severity because `execute_hook` itself (FIX 3a) now absorbs essentially every failure this backstop was written for, making the backstop very unlikely to actually fire against the production `run_hooks` wiring — but if it does fire (a future refactor of `run_hooks`, or a test double), the response is measurably worse than the path it's standing in for. See code review C11.

---

## Traps and acceptance-table re-check (deeper pass this iteration)

Re-walked the guide's §6 acceptance table specifically for rows touched by this
iteration's fixes:

- "Concurrent setup for two boards never mixes probe/UART rows from different scans" —
  still holds; `uart_snapshot()` mints its own fresh `snapshot_id`/`_RowIds` exactly
  like `snapshot()`, and both go through the same `_collect_uart_rows` for the UART
  half, so there is no way for a UART-only scan to disagree with a full scan about what
  native/hook UART state existed at that instant.
- "Any installed pyOCD provider openable by the returned UID completes the same paths"
  — this is the row D8 actually threatens: if the wrong provider's row is the one that
  survives the `_setup_overview` collision, the *returned* UID is real and openable,
  just for a different physical device than the agent thought it was choosing. Not
  caught by any existing acceptance test.
- "Hooks never run on a machine where native discovery works" — re-verified against
  `_collect_uart_rows`; still correctly gated, and the gating decision for UART is now
  provably identical between `snapshot()` and `uart_snapshot()` since both call the
  same private method with the same counter-threading discipline.

No scope creep found in this iteration's changes: FIX 1-7 are each scoped exactly to
their adjudicated finding, with no incidental behavior changes riding along (spot-checked
by re-reading every hunk in `git diff af37870..HEAD` against its stated purpose in the
ledger).
