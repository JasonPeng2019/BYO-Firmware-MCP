# Iteration 3 — Code Adversary

Scope: the code as it now stands (after eb8e375 + 0fff3f1), attacked fresh, ignoring
what changed. Hardest scrutiny on the new provider-qualified token format and its
legacy-compatibility path, per the coordinator's direction. 610 tests pass (verified:
`Ran 610 tests in 116.762s / OK (skipped=7)`), ruff clean.

IDs continue from iteration 2 (`C1`-`C11`); this file adds `C12`-`C14`.

## Summary

| Severity | Count |
| --- | --- |
| CRITICAL | 1 |
| HIGH | 0 |
| MEDIUM | 2 |
| LOW | 0 |

---

## C12 — CRITICAL — a legacy connection token whose UID contains a colon resolves to the wrong physical probe, or is wrongly reported as gone

**Files:** `src/pyocd_debug_mcp/services/connections.py:47-66`, `src/pyocd_debug_mcp/hardware_inventory.py:762-819`, `src/pyocd_debug_mcp/server.py:3665-3691, 4821-4852, 4854-4883`

Full mechanism and both reproductions are in `reviews/iter-3-diff.md` (D11); restated
here from the pure code-correctness angle since it holds regardless of how the token
got into this shape.

`parse_probe_connection_id` distinguishes "canonical, provider-qualified" from "legacy,
provider-less" purely by whether the text after the `probe:` prefix contains **any**
colon:

```python
provider_part, separator, uid_part = rest.partition(":")
if not separator:
    return None
```

This is not a safe discriminator, because the legacy shape it is trying to detect the
*absence* of is `probe:{uid}` with no constraint that `uid` itself be colon-free. A UID
containing a colon (realistic for hook-reported values — the guide's own platform
guidance points hook authors at Linux USB topology paths like `3-1.4:1.0`) makes a
plain legacy token indistinguishable from a genuine canonical one, and the function
picks the canonical interpretation, splitting the UID's own text into a fabricated
"provider" and a truncated "uid."

Concretely reproduced twice against the shipped code:

1. **False negative** (`derive_selection_from_token`): a legacy token for a real,
   present `cmsisdap` probe with `unique_id = "3-1.4:1.0"` resolves to `None` because
   the misparsed "provider" (`"3-1.4"`) matches no row. The caller
   (`ProbeSelectionStore.resolve`) raises `SelectionNotRecorded`/`SelectionDisappeared`
   — the agent is told to rerun setup for a probe that never left.

2. **Wrong selection** (`derive_selection_from_token`, worse): a legacy token for a
   `cmsisdap` probe whose `unique_id` is `"jlink:001"` misparses into
   `provider="jlink", uid="001"`, which matches a *different*, unrelated, real jlink
   probe also present in the snapshot. The function returns that row's
   `ProbeSelection` — not a refusal, a **successful resolution to the wrong physical
   device**. Downstream, `_resolved_probe_uid_for_connection` hands pyOCD that UID with
   that provider, and whatever opens next opens a debugger the caller did not select.

`_connection_matches_probe`, `_same_setup_connection`, and `_setup_connection_key`
share the identical `parse_probe_connection_id(...) is not None` branch structure, so
the same misparse propagates into every comparison and dedup-key path that accepts a
legacy token on either side — not just probe selection.

**Severity note:** this is rated CRITICAL specifically because of reproduction #2 —
resolving to a *different, real* piece of hardware is exactly the class of defect this
whole token-format rework (FIX 8, closing C7/D8 and C8) exists to prevent, and it now
happens through the one code path FIX 8 added specifically to stay compatible with
what it replaced.

**Suggested fix direction:** don't infer canonical-vs-legacy from colon *count*.
Either (a) make the legacy shape unambiguous — e.g., only ever accept a legacy token
when `unquote(rest)` round-trips to something that was never itself produced by
`quote(..., safe="")` (a percent-encoded canonical uid segment cannot contain a raw
`:`, so a colon inside a percent-decoded segment is diagnostic), or more simply
(b) require both segments of a canonical token to consist only of the character set
`quote(..., safe="")` can produce (`[A-Za-z0-9_.~%-]`) before accepting the split,
rejecting (falling back to legacy handling) the moment either segment contains a raw
character that encoding would never have left there — including a literal, unencoded
`:`.

---

## C13 — MEDIUM — `_known_provider_for_board`'s profile fallback is untested for the divergence case that would make it unsafe

**File:** `src/pyocd_debug_mcp/server.py:3464-3491`

Restated from `reviews/iter-3-diff.md` D12 for the code-as-it-stands lens: the function
prefers a live connection's `probe_family`, falling back to the board's profile
`probe_family` only when the live connection is already gone. The fallback is safe if
and only if the profile's declared provider still matches the provider of the
connection that just vanished. Nothing in the code asserts or checks that; the
docstring's safety claim rests entirely on that always being true, and the one test
covering this path constructs both sides from the same fixture, so it cannot detect a
divergence even if one were introduced. I was not able to construct a concrete
divergence scenario within this pass (it would require establishing whether this
codebase ever permits a live connection's `probe_family` to differ from its board's
declared one — a question this diff doesn't answer either way), so this is flagged as
an unverified, load-bearing assumption rather than a proven defect. Given `M3`'s own
framing ("worse than failing to match" is the failure mode being guarded against here),
it deserves either a targeted regression test that actually varies the two sources
independently, or an explicit comment establishing why they can never diverge.

---

## C14 — MEDIUM (test infrastructure, not product code) — the fake hook's counter-lock helper doesn't handle a Windows-specific `PermissionError` on `O_EXCL`, causing rare, reproducible flakiness in the snapshot-atomicity concurrency tests

**File:** `tests/fake_discovery_hook.py:47-70` (`_counter_value`)

Full investigation and root-cause evidence in `reviews/iter-3-diff.md` (D13); included
here because it is a real defect in code that ships in the repository, even though it
is test infrastructure rather than the server. `_counter_value`'s lock-acquisition
retry loop only treats `FileExistsError` as "someone else holds the lock, retry":

```python
try:
    handle = os.open(str(path) + ".lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
except FileExistsError:
    time.sleep(0.005)
    continue
```

On Windows, the same race (this process's `O_CREAT|O_EXCL` landing while another
process is mid-way through creating or deleting the identical lock file) can surface as
`PermissionError` (`WinError 5`) instead of `FileExistsError` — reproduced directly
under 6-way concurrent subprocess contention, with the exact traceback captured:
`PermissionError: [Errno 13] Permission denied: '...\uart.count.lock'` at the `os.open`
call. Uncaught, this crashes the fake hook script (exit code 1), which
`HardwareInventoryService`/`execute_hook` then — correctly — report as a failed hook
contributing zero rows, which is what trips
`test_concurrent_snapshots_are_internally_consistent`'s `len(snapshot.uarts) == 1`
assertion.

This is confined to the test fixture; `HardwareInventoryService`'s own concurrency
handling (per-snapshot `_RowIds`, the `HookSnapshotStore` lock, per-call fresh
`snapshot_id`) showed no inconsistency in any of the runs performed for this review —
every failure traced to this one lock-acquisition gap, never to a mismatched
`snapshot_id`, a cross-contaminated row, or a duplicate counter value. Minimal fix
(not applied, per instruction): catch `(FileExistsError, PermissionError)` in the retry
loop, matching how this codebase's own production process-management code
(`kernel/processes.py`) already treats Windows OS-error variance as expected rather
than exceptional.
