# Iteration 3 — Diff Adversary

Scope: `git diff d4b1a14..HEAD` (eb8e375 "FIX 9-13" + 0fff3f1 "FIX 8 + M3 addendum")
against the implementation guide. `reviews/ledger.md` read in full first. Suite at
610 passed / 7 skipped, ruff clean — reproduced independently, see D13 for a caveat on
one test's stability under load.

IDs continue from iteration 2 (`D1`-`D10`); this file adds `D11`-`D13`.

## Summary

| Severity | Count |
| --- | --- |
| CRITICAL | 1 |
| HIGH | 0 |
| MEDIUM | 2 |
| LOW | 0 |

**On FIX 9-13 (eb8e375):** all five are correct and each closes exactly the mechanism
described in the ledger — verified by reading the code, not the prose. FIX 9 (`resolve()`
touches recency on success) is tested with a real eviction-pressure scenario that proves
both survival and genuine churn, not a vacuous assertion. FIX 10 (count before
resolve/hash) moved the check to before the expensive loop, verified by reading the new
control flow, and its `test_no_hashing_occurs_when_the_aggregate_cap_is_exceeded` spy
test would fail against the pre-fix ordering. FIX 11's `_failure_retry_fields` genuinely
eliminates the duplication the finding was about. FIX 12 (dead class deletion) and FIX
13 (`test_probe_inventory.py`) are exactly what the ledger describes.

**On FIX 8 (0fff3f1), the riskiest change:** the headline fix (`connection_id` keyed on
`(provider, uid)`) is correct and well-tested for the case it targets — cross-provider
UID-text collisions no longer collapse in `_setup_overview`, confirmed by
`CrossProviderCollisionTests`. But the coordinator was right to flag this commit as the
one deserving the most scrutiny: **D11 below is a real, reproduced defect in the
mechanism FIX 8 introduces to stay backward-tolerant with the pre-FIX-8 token shape.**
Not a defect in the headline collision fix itself — a defect in its legacy-compatibility
shim.

---

## D11 — CRITICAL — legacy-token detection in `parse_probe_connection_id` misparses a UID containing a colon, causing either a false "probe disappeared" refusal or (worse) silent resolution to a different physical probe

**File:** `src/pyocd_debug_mcp/services/connections.py:47-66` (`parse_probe_connection_id`), consumed by `src/pyocd_debug_mcp/hardware_inventory.py:762-819` (`derive_selection_from_token`), `src/pyocd_debug_mcp/server.py:3665-3691` (`_connection_matches_probe`), `src/pyocd_debug_mcp/server.py:4821-4852` (`_same_setup_connection`), `src/pyocd_debug_mcp/server.py:4854-4883` (`_setup_connection_key`)

The guide's hook output contract makes `unique_id` free-form vendor text (only NUL-byte
and length limits are enforced by `_row_text` in `discovery_hooks.py`); the guide's own
cross-platform guidance section explicitly points hook authors at USB topology data
(`/sys/bus/usb/devices`, IOKit, Windows PnP), whose native path format
(`3-1.4:1.0` on Linux, for one) contains a literal colon. A **legacy** (pre-FIX-8,
unencoded) `probe:{uid}` token for such a device has that colon sitting unescaped
inside the very shape `parse_probe_connection_id` is asked to tell apart from the new
canonical `probe:{provider}:{uid}` form:

```python
def parse_probe_connection_id(connection_id: str) -> tuple[str, str] | None:
    ...
    rest = connection_id[len(PROBE_CONNECTION_PREFIX):]
    provider_part, separator, uid_part = rest.partition(":")
    if not separator:
        return None                       # <-- only a *zero*-colon token is "legacy"
    ...
    return unquote(provider_part), unquote(uid_part)
```

Any legacy token whose uid contains one or more colons has `separator` truthy, so this
function confidently returns a `(provider, uid)` split that is not the pair anyone
minted — the text before the *first* colon becomes a bogus "provider," and only the
tail becomes the "uid." Every caller listed above treats a non-`None` return as
"trustworthy, provider-qualified" and stops applying the legacy (UID-text-only,
cross-provider) tolerance from that point on.

**Reproduction 1 — a real, present device is reported as gone (`derive_selection_from_token`):**

```python
uid_with_colon = "3-1.4:1.0"                       # plausible hook-reported unique_id
snapshot = InventorySnapshot(..., probes=(row("cmsisdap", uid_with_colon),), ...)
legacy_token = f"probe:{uid_with_colon}"            # exactly what pre-FIX-8 minted
parse_probe_connection_id(legacy_token)             # -> ("3-1.4", "1.0")  -- wrong split
derive_selection_from_token(legacy_token, snapshot) # -> None
```
`ProbeSelectionStore.resolve()` then raises `SelectionNotRecorded`, and the agent is
told "the assigned probe is no longer present; rerun setup routing" for a probe that
is, in fact, still attached and was legitimately selected.

**Reproduction 2 — worse: a different, real probe is silently substituted:**

```python
intended_uid = "jlink:001"       # a hook's unique_id that happens to start "jlink:"
snapshot = InventorySnapshot(..., probes=(
    row("cmsisdap", intended_uid),   # the device this token actually names
    row("jlink", "001"),             # a second, unrelated, real jlink probe
), ...)
legacy_token = f"probe:{intended_uid}"
derive_selection_from_token(legacy_token, snapshot)
# -> resolves to provider='jlink', unique_id='001' -- the WRONG physical probe
```
This is not a refusal; it is a selection. `_resolved_probe_uid_for_connection`/
`_assigned_probe_uid_for_connect` would hand pyOCD the wrong provider/UID pair, and
whatever opens next opens the unrelated jlink device instead of the cmsisdap one the
token was minted for.

**Scope:** since `probe_connection_id` (the only minting function, post-FIX-8) always
percent-encodes both fields, a **freshly minted** token can never trigger this — a
literal colon in either field is always `%3A` in anything minted by this version of the
code. The exposure is specifically a token minted by a pre-FIX-8 server that an agent
still holds (a live conversation spanning the FIX-8 upgrade) being replayed against the
post-FIX-8 comparison/resolution helpers, which is exactly the compatibility path
`parse_probe_connection_id`'s "legacy two-part token" handling exists to serve. No test
in `CrossProviderCollisionTests` or elsewhere in this diff constructs a UID containing a
colon.

---

## D12 — MEDIUM — `_known_provider_for_board`'s profile-fallback safety claim (M3) is asserted, not tested, for the one case that would matter

**File:** `src/pyocd_debug_mcp/server.py:3464-3491`

The docstring's safety argument is: the live-connection source is "always correct when
present," and the profile fallback only fires "when the connection has already gone."
What it does not establish is whether the profile's `probe_family` is guaranteed to
still match the provider of the connection that *just* vanished — i.e., whether the
fallback ever produces a canonical-*looking* key for the wrong probe (worse than the
documented "safely fails the exact match" outcome, which is what happens only when
*neither* source is available).

The only test exercising this fallback,
`tests/test_server_assignment_connect.py::test_a_mismatch_after_the_connection_already_vanished_still_matches_via_the_profile`,
constructs the profile and the original assignment from the **same** `_board("board-1")`
helper, so `profile.board.probe_family` is true by construction, not by anything the
production code verifies at the moment it matters. It proves the fallback *can* recover
the right answer; it does not probe whether the fallback can be fed a stale one. I could
not construct a concrete reproduction of profile/live divergence within this review's
scope (doing so would require establishing whether this codebase ever permits a live
connection's `probe_family` to differ from its board's declared one, which is a larger
question than this diff), so this is reported as an unverified assumption load-bearing
enough to deserve either a targeted test or a structural argument for why the divergence
is impossible — not as a proven defect.

---

## D13 — Flaky-test claim verified: `test_concurrent_snapshots_are_internally_consistent` is genuinely pre-existing and is a test-scaffolding defect, not a race in the code under test

**File:** `tests/fake_discovery_hook.py:47-70` (`_counter_value`)

Verified independently rather than accepted:

- `git log --oneline -- tests/fake_discovery_hook.py` shows exactly one commit,
  `3bcdb7a` ("Step 1: discovery_hooks.py"), the very first commit of this entire
  feature. Neither this file nor `tests/test_inventory_snapshot_concurrency.py`
  (also a single-commit file, `af37870`) has been touched by any of the three
  iterations' fixes. The "pre-existing" claim holds.
- Reproduced the flake directly: 8 parallel processes each running the full
  concurrency test file failed once in 8 with `AssertionError: 0 != 1` on
  `len(snapshot.uarts)`. A standalone diagnostic harness (not a test-file edit) that
  prints `hook_diagnostics` on a bad snapshot instead of just asserting caught the
  underlying cause directly:
  ```
  PermissionError: [Errno 13] Permission denied: '...\uart.count.lock'
    at: handle = os.open(str(path) + ".lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
  ```
- `_counter_value`'s retry loop only catches `FileExistsError`:
  ```python
  try:
      handle = os.open(str(path) + ".lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
  except FileExistsError:
      time.sleep(0.005)
      continue
  ```
  On Windows, `O_CREAT | O_EXCL` can raise `PermissionError` instead of
  `FileExistsError` when the create races another process's delete-then-recreate of
  the same lock file (a well-known Windows/NTFS quirk absent on POSIX, where the
  analogous race reliably raises `FileExistsError`). When that happens here, the
  exception is uncaught, the hook script crashes with a Python traceback and exit
  code 1, and the discovery-hook execution machinery under test does exactly what it
  is supposed to do with a hook that exits nonzero: report it as failed and contribute
  zero rows. `HardwareInventoryService`, `execute_hook`, and the atomicity guarantees
  the test file exists to check all behaved correctly; the test's own fixture is what
  broke under real concurrent load.

**Verdict:** genuine pre-existing test-scaffolding flakiness, confined to
`tests/fake_discovery_hook.py`'s counter-lock helper, platform-specific to Windows, and
not indicative of a race in `HardwareInventoryService`/`discovery_hooks.py`. It reliably
does not reproduce under the suite's normal sequential execution (12/12 clean
sequential runs) and only surfaces under artificial parallel-process stress well beyond
what the suite itself applies — consistent with it not showing up as suite-level
flakiness in normal CI. Not something I would mark green over without comment, though:
the underlying gap (`except FileExistsError` should be `except (FileExistsError,
PermissionError)`, matching how `popen_owned`/`terminate_process_group` elsewhere in
this codebase already handle Windows-specific OS error variance defensively) is real
and would keep surfacing under real-world contention. Reported for your call; not fixed,
per instruction.
