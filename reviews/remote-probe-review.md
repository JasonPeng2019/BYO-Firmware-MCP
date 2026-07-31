# Review: remote probe endpoints (`register_remote_probe` / `unregister_remote_probe`)

Reviewed against `REMOTE_PROBE_PLAN.md`, `.codex/design_charter.md`, and
`reviews/REVIEW_POLICY.md`. Scope: `git diff` (`firmstore/store.py`,
`hardware_inventory.py`, `server.py`, `tools/discovery.py`) plus the four new files
(`remote_probes.py`, `tools/remote_probes.py`, `tests/test_remote_probes.py`,
`tests/manual/manual_remote_probe_hardware_check.py`).

## Verdict

**One MUST-FIX finding (R1): a real, reproducible data-loss race on concurrent
registration calls.** Everything else checked out. The feature is wired end to end
into production (not a D15 repeat), the "no registration = byte-identical behavior"
invariant holds, the plan's three deliberate anti-gating/prefix/uart-untouched
decisions are implemented exactly as specified, and the implementer's own test suite
is not vacuous -- every load-bearing test I picked was proven able to fail. The
hardware test passes clean against the real ST-LINK.

## Findings, severity order

### R1 -- MUST-FIX: concurrent `register_remote_probe` calls silently lose data

**What it is.** `remote_probes.py`'s registry is a single shared JSON file updated by
read-modify-write (`load_remote_probes` -> `upsert_remote_probe`/`remove_remote_probe`
-> `save_remote_probes`) with no lock anywhere in the call chain, and nothing in
`kernel/operations.py`'s dispatch serializes two calls to a board-less tool.
`RegistryFastMCP.call_tool` -> `dispatch()` only takes a mutual-exclusion lock keyed on
`board_id` (`worker_lock(board_id)`); `register_remote_probe`/`unregister_remote_probe`
take no `board_id` parameter, so `manager.worker_lock(None)` returns a plain
`nullcontext()` (`kernel/operations.py:429-431`) and every sync tool call without a
`board_id` -- including these two -- runs on its own fresh `threading.Thread`
(`kernel/operations.py:876-883`) with zero mutual exclusion against any other such
call.

Two independent, ordinary `register_remote_probe` calls -- e.g. an agent registering
two different bench probes it just learned about in the same turn, which is exactly
the shape of call a tool-calling agent makes and not a contrived input -- can
interleave as: A reads the registry (sees nothing), B reads the registry (also sees
nothing), B upserts+saves (file now has B), A upserts its own entry onto its *stale*
snapshot and saves (file now has only A). B's registration is gone, even though B's
tool call already returned `"status": "remote_probe_registered"` to the agent --
a fabricated success report on a normal-usage path.

**Evidence (reproduction, not argument).** Ran directly against the real handler
functions built by `build_remote_probe_handlers` (see
`tests/test_remote_probe_smoke.py::ConcurrentRegistrationTests::test_concurrent_registrations_do_not_lose_an_entry`
for the deterministic version, which forces the interleave via a blocking
`check_endpoint` stub instead of racing on thread timing). A quick two-thread
probe reproduced it on the first try:

```
entries after concurrent registration: [('hostB', 5556)]
count: 1 (expected 2 if no race, race shows <2 sometimes)
```

The deterministic version in the new smoke test file fails the same way every run:

```
AssertionError: Items in the second set but not the first:
'host-b' : a concurrent registration was silently lost even though its tool call
reported 'remote_probe_registered' -- got: {'host-a'}
```

**MUST-FIX, per REVIEW_POLICY.** This is "data loss ... reachable in normal
operation" and "wrong data returned" on an ordinary path with realistic inputs (two
independent registrations, no adversarial timing needed -- any concurrent dispatch of
two board-less tool calls hits it). It is not gated behind a hypothetical: this
server's own dispatch code documents that only `board_id`-carrying tools get
serialized, and these two tools deliberately don't take one.

**What fixing it risks.** Low risk if scoped narrowly -- e.g. a module-level
`threading.Lock` around the load-upsert-save (or load-remove-save) cycle in
`remote_probes.py`, or in `tools/remote_probes.py` around both handlers. Keep it
scoped to just this registry's own read-modify-write; do not reach for
`kernel/operations.py`'s board-lock machinery (that's keyed on real board contention,
not applicable here) and do not add a lock anywhere broader than this one file, or
it risks becoming exactly the kind of guarding-past-the-point-of-value this repo's
`HANDOFF.md` warns against (D25 -> D30).

### R2 -- EXTRANEOUS (recorded, not fixed): the real registry file is a new un-mocked dependency for existing `server`-singleton tests

**What it is.** `server._hardware_inventory`'s new `remote_probes` field resolves to
`load_remote_probes(_firm_store.layout.remote_probes)`, i.e. a real,
persistent, cwd-relative file at `<project_root>/.firm/remote_probes.json`
(gitignored, `_project_root` defaults to `Path.cwd()`). This is exactly what the
plan asks for -- persistence across restarts is the point of the feature -- and in a
clean checkout it is invisible: a full `python -m unittest discover -s tests` run with
no such file present is 724/724 green with zero remote-provider pollution, confirmed
by running it before writing any new tests.

But at least 11 existing test files (`test_server_assignment_connect.py`,
`test_setup_overview_no_probe.py`, `test_probe_selection_records.py`, others) import
`server` directly and call through its real `_hardware_inventory` /
`_validation_inventory()` singletons, and those tests already patch out other
real-world dependencies of that singleton (`_run_cmd`, `list_serial_ports`,
`_profile_repository`) for exactly this class of reason -- but none of them patch the
new `remote_probes` dependency, because it didn't exist when they were written and
this diff doesn't touch them.

**Evidence.** Caught by accident, then confirmed deliberately. An early draft of my
own new wiring test (see history below) wrote a real entry to
`.firm/remote_probes.json` under the repo root. That alone was enough to break an
entirely unrelated, pre-existing test with no clue to the real cause:

```
FAIL: test_empty_cli_inventory_includes_active_uidless_connection_as_session_local
AssertionError: 2 != 1
```

Deleting the stray file made it pass again immediately, confirming the file -- not
the test's own logic -- was the cause. `.firm/` is gitignored and the file was purely
a leaked test artifact of mine, not something the implementer's diff writes; I removed
it and reran the full suite clean (729/729 minus the one intentional R1 failure).

**Why EXTRANEOUS, not MUST-FIX.** In a clean checkout with the default suite, nothing
is broken -- this required an external actor (a manual `pyocd server` bring-up test,
or a buggy test like my own first draft) to leave state behind first, and the
mechanism (a real cwd-relative file under `.firm/` that `server`-singleton tests must
explicitly mock away) is the codebase's pre-existing convention, not something this
diff invented structurally -- it's the same shape of exposure `_run_cmd` /
`list_serial_ports` / `_profile_repository` already have, and per REVIEW_POLICY
"if the adjacent, already-reviewed code has the identical exposure, it is not a new
defect." Recording it because the failure mode, once tripped, is confusing and
far from the real cause (I burned real time chasing it) -- worth a one-line mention in
`HANDOFF.md`-style documentation for the next person, not a code change.

**What fixing it would risk.** Nothing on the production side needs to change. If the
orchestrator wants to harden this, the safe fix is test-side only: a `setUp`/
`tearDown` guard in the base test case used by `server`-importing tests, or a note in
the manual hardware script / developer docs to always run from a scratch directory.
Touching production code for this would be solving a test-hygiene problem in `src/`,
which the charter would call out as the wrong layer for the fix.

### R3 -- EXTRANEOUS (recorded, not fixed): re-registering with no `description` silently blanks a previously-set one

**What it is.** `register_remote_probe(host, port)` called a second time without a
`description` argument overwrites the stored description with `""` rather than
preserving the existing value, because the handler always calls
`upsert_remote_probe(entries, host, port, description or "")` -- there is no
"description was omitted, keep the old one" branch.

**Evidence (reproduction).**

```
after first register: bench ST-LINK, rack 3
after re-register with no description: ''
```

**Why EXTRANEOUS.** Purely cosmetic -- `description` never participates in probe
selection, `resolve()`, or `open_session`; it's a human-readable label only. The
scenario requires an agent to re-call `register_remote_probe` without repeating the
label (plausible if re-checking reachability, but not the primary documented
recovery path -- the tool's own `agent_prompt` for an unreachable endpoint tells the
user to start the server and take a fresh *snapshot*, not to re-call
`register_remote_probe`). No data-integrity or hardware-selection consequence per the
REVIEW_POLICY materiality test.

## Tests I tried to break (and could not) -- proving they are load-bearing, not vacuous

Per the task, I picked what looked like the most load-bearing tests in
`tests/test_remote_probes.py` and tried to break the production code under each one
without failing it. All three attempts were caught. Each edit was made, the specific
test run to confirm the failure, then reverted exactly (confirmed via
`git diff --stat` matching the original diff before and after every experiment):

1. **Anti-gating test** (`test_remote_rows_stay_visible_alongside_a_native_probe`).
   Reintroduced the exact anti-pattern the plan calls out by name -- gated the remote
   merge behind `if not probe_rows:` in `hardware_inventory.py:360`. Test failed
   (`1 != 2`) as expected. Reverted.
2. **Prefix-preservation tests**
   (`test_a_registered_endpoint_appears_as_a_remote_provider_row` and
   `test_the_remote_selector_survives_selection_and_resolve_unmangled`). Stripped the
   `remote:` prefix from `unique_id` in `_remote_probe_rows`
   (`hardware_inventory.py:604`), i.e. exactly the mistake the plan warns would
   "silently stop the feature from working" since `TCPClientProbe.get_probe_with_id`
   requires `is_explicit`. Both tests failed with `'bench.local:5555' !=
   'remote:bench.local:5555'`. Reverted.
3. **Malformed-file guards** (`test_malformed_json_loads_as_empty_rather_than_crashing`
   and `test_a_corrupt_registry_file_does_not_crash_a_snapshot`). Removed the
   `try/except json.JSONDecodeError` in `load_remote_probes` (`remote_probes.py:91`).
   Both tests failed with an unhandled `JSONDecodeError` propagating out of
   `snapshot()` -- confirming the second test genuinely exercises the crash-safety
   path through a real `HardwareInventoryService.snapshot()` call, not just the loader
   function in isolation. Reverted.

I did not find any test in the implementer's suite that passed for the wrong reason.
I did not exhaustively try to break every one of the ~40 tests in that file (see
blind spots below), but the ones covering the plan's stated highest-risk items (the
no-registration invariant, the anti-gating rule, the prefix requirement, and
crash-safety) all held up.

## New tests added: `tests/test_remote_probe_smoke.py`

Did not edit `tests/test_remote_probes.py` (implementer-owned). New file covers two
things the implementer's suite structurally cannot, since every test there constructs
its own fresh `HardwareInventoryService/RemoteProbeToolServices` and never touches
`server`'s real module-level objects:

- **`ProductionWiringTests`** (4 tests, all currently passing) -- exercises the real
  `server.mcp` / `server.tool_registry` / `server._hardware_inventory` /
  `server.remote_probe_tool_handlers` singletons directly, the same way
  `tests/test_setup_overview_no_probe.py` does. Proved these can fail: temporarily
  changed the tool-registration loop in `server.py` to `continue` before
  `mcp.add_tool(...)` (simulating a D15-style "built but never wired" regression) --
  the wiring test failed correctly on both tool names, then the change was reverted
  and confirmed via `git diff --stat` matching the original.
- **`ConcurrentRegistrationTests`** (1 test, currently **failing** -- this is R1,
  left red deliberately as the concrete demonstration rather than papered over).

Both classes' file-redirection tests patch `server._firm_store.layout` rather than
`server._remote_probes_registry_path` -- see the docstring on
`test_the_real_registered_tool_handler_writes_through_the_real_path_resolver` for why
(the tool-services wiring in `server.py` binds the function object directly at import
time rather than doing a late name lookup, so patching the module-level name alone
does not redirect it; this is not a production defect, just a fact about how to test
it correctly, and I got it wrong on the first attempt -- which is also how R2 above
got triggered and then cleaned up).

## Hardware test -- verbatim

Ran `python tests/manual/manual_remote_probe_hardware_check.py` against the real
ST-LINK (`0668FF514988525067213913`). Script did not need any fixes.

```
Generic 'cortex_m' target type is selected by default; is this intentional? You will be able to debug most devices, but not program flash. To set the target type use the '--target' argument or 'target_override' option. Use 'pyocd list --targets' to see available targets types.
Waiting for pyocd server to open 127.0.0.1:57050 ...
pyocd server is accepting connections.
Registering remote:localhost:57050 ...
{'agent_prompt': "remote:localhost:57050 answered a TCP connect attempt just now and is registered. It will appear as a 'remote' provider row in every future inventory snapshot; use its unique_id verbatim as the probe selector.", 'description': 'manual hardware check', 'host': 'localhost', 'port': 57050, 'reachable': True, 'selector': 'remote:localhost:57050', 'status': 'remote_probe_registered'}
snapshot() probe rows: ['remote:localhost:57050']
Opening a session through the server's own connect path ...
Read back PC = 0x080015DE
Unregistering remote:localhost:57050 ...
{'agent_prompt': 'remote:localhost:57050 was removed; it will no longer appear in inventory snapshots.', 'host': 'localhost', 'port': 57050, 'removed': True, 'selector': 'remote:localhost:57050', 'status': 'remote_probe_unregistered'}
PASS: the remote probe route works end to end against real hardware.
```

The `cortex_m` generic-target warning is expected per the plan (a remote probe reports
no board identity). PC readback `0x080015DE` matches the plan's own reference
measurement (`0x80015de`) on this board. Confirmed no leftover `pyocd`/`python server`
process after the run (`Get-Process` showed nothing matching).

## Checks, verbatim tails

`python -m unittest discover -s tests` (after adding the new smoke test file --
the one failure is R1, left in deliberately):

```
======================================================================
FAIL: test_concurrent_registrations_do_not_lose_an_entry (test_remote_probe_smoke.ConcurrentRegistrationTests.test_concurrent_registrations_do_not_lose_an_entry)
Two independent `register_remote_probe` calls for two different endpoints,
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\Users\Jason\Documents\Jason\FirmCLI-WIP\BYO-Firmware-MCP\tests\test_remote_probe_smoke.py", line 230, in test_concurrent_registrations_do_not_lose_an_entry
    self.assertEqual(
AssertionError: Items in the second set but not the first:
'host-b' : a concurrent registration was silently lost even though its tool call reported 'remote_probe_registered' -- got: {'host-a'}

----------------------------------------------------------------------
Ran 729 tests in 132.858s

FAILED (failures=1, skipped=7)
```

(Before adding any new tests, the baseline was 724 ran / 724 passed / 7 skipped,
confirming the diff alone breaks nothing pre-existing.)

`python -m ruff check src/ tests/`:

```
All checks passed!
```

`python -m pyright src/`:

```
0 errors, 0 warnings, 0 informations
```

`python -m pyright tests/` (must stay exactly 19 -- confirmed unchanged, no new
errors from either the implementer's file or my smoke-test file):

```
19 errors, 0 warnings, 0 informations
```

## What I did NOT check

- Did not attempt to break every one of the ~40 implementer tests -- picked the four
  the plan flags as highest-risk (no-registration invariant already indirectly
  covered by R2's discovery, anti-gating, prefix-preservation, crash-safety) and left
  the rest (dedupe casing, boundary ports, host/port validation messages, the
  `unregister`-specific tests) unverified by direct mutation, though I read all of
  them and none looked structurally vacuous.
- Did not test behavior under a genuinely corrupted-but-partially-valid registry file
  beyond the implementer's own `test_a_bad_entry_is_skipped_but_a_good_sibling_entry_still_loads`
  case (e.g. duplicate `(host, port)` pairs already present in a hand-edited file --
  `load_remote_probes` doesn't dedupe on load, only `upsert_remote_probe` does on
  write, so a manually crafted file with two rows for the same endpoint would produce
  two probe rows in a snapshot; did not chase whether that's reachable outside manual
  file editing).
- Did not check behavior on a second OS (this machine is Windows; the
  `PYTHONIOENCODING=utf-8` requirement and the manual script's process-teardown path
  were only exercised here).
- Did not check what happens if `register_remote_probe`/`unregister_remote_probe` are
  called with a `unique_id`-shaped host string containing a colon (e.g. someone passes
  an IPv6 literal as `host`) -- `normalize_host` only strips and rejects empty; a
  bare IPv6 address would produce a selector like `remote:::1:5555`, which is
  ambiguous for pyOCD's own first-colon split. This is a real edge case but requires
  a caller to actually have an IPv6-only probe-server host, which the plan's
  non-goals section implicitly puts out of scope (no URL/address-format handling
  beyond "two parameters"); did not verify pyOCD's actual behavior against such a
  selector.
- Did not review `RemoteProbeError`/exception-message wording for consistency with
  other error-message conventions elsewhere in the codebase beyond a read-through.
- Left the R1-demonstrating test failing in the tree rather than fixing `src/` myself,
  per the hard rule not to edit production code -- the suite will show one failure
  until the orchestrator decides how to fix R1.

## Round 2 addendum -- R1's regression test was vacuous, and why

R1 (above) is fixed: `remote_probes.py` now has `_registry_lock` (a `threading.Lock`),
and `register_entry`/`unregister_entry` run the whole load -> modify -> save cycle
under it, with `check_endpoint` deliberately moved to run *before* the lock is taken
(correctly -- holding the lock across a multi-second TCP connect would serialize two
unrelated registrations behind each other's network timeout).

**My round-1 regression test (`test_concurrent_registrations_do_not_lose_an_entry`)
went vacuous the moment that fix landed, and it was the fifth test in this codebase's
history to pass for the wrong reason.** The coordinator caught it by replacing
`_registry_lock` with `contextlib.nullcontext()` and watching the test still pass.

**Why it went vacuous.** The test's only mechanism for forcing a deterministic
interleave was blocking host-a's `check_endpoint` stub until host-b's registration had
fully completed. That mechanism assumed `check_endpoint` ran *after* the registry was
loaded but *before* it was saved -- true of the pre-fix code, where the whole handler
body was one unbroken sequence (load, check_endpoint, upsert, save) with nothing
marking a critical section. The fix reordered that sequence specifically so
`check_endpoint` now runs *before* `register_entry` is called at all, i.e. strictly
before the registry is ever loaded. So when host-a's stub blocked and then resumed, it
went on to call `register_entry`, which does its own fresh `load_remote_probes` *after*
host-b had already finished and saved -- host-a's load already saw host-b's entry, and
both survived, lock or no lock. The test's blocking point was no longer inside the
window it needed to be inside; it was testing that check_endpoint eventually returns,
nothing about the registry write.

**Why a like-for-like interleave test cannot be rebuilt against the fixed code.** The
only point strictly between the real load and the real save is now inside
`_registry_lock` itself, inside `register_entry`. There is no hook exposed to a test
double at that point -- `RemoteProbeToolServices` only lets a test control
`check_endpoint`, which the fix moved to outside the section entirely, on purpose.
Blocking a thread *inside* the critical section from test code would require the test
itself to acquire `_registry_lock` first, which just deadlocks against correct code --
real proof the lock works, but not something you can assert on.

**The fix: an invariant test instead of an interleave test.** Rewrote
`ConcurrentRegistrationTests` in `tests/test_remote_probe_smoke.py` with two
`threading.Barrier`-synchronised tests that fire many real, concurrent calls at the
actual production handlers and assert every one of them survives intact:

- `test_many_concurrent_registrations_all_survive` -- 24 threads, released together by
  a barrier, each registering a distinct `(host, port)`; asserts all 24 are present
  afterward.
- `test_concurrent_register_and_unregister_do_not_corrupt_the_registry` -- pre-seeds 12
  entries, then barrier-releases 6 concurrent `unregister` calls against half of them
  alongside 12 concurrent `register` calls for 12 new endpoints (every operation on a
  distinct key, so the only way to fail is a lost write, never two threads disagreeing
  about one key); asserts the exact expected final set.

This is not flaky in the direction that matters. Proved both directions directly
rather than taking it on faith:

- **Against the current (locked) code**, both pass:

  ```text
  test_concurrent_register_and_unregister_do_not_corrupt_the_registry ... ok
  test_many_concurrent_registrations_all_survive ... ok

  Ran 2 tests in 0.787s
  OK
  ```

- **With `_registry_lock` replaced by `contextlib.nullcontext()`** (same technique the
  coordinator used), both fail -- and fail hard, with real `PermissionError`s from
  concurrent unlocked `os.replace` calls on Windows, not just a lost-entry count:

  ```text
  FAIL: test_concurrent_register_and_unregister_do_not_corrupt_the_registry
  AssertionError: Lists differ: [PermissionError(13, 'Access is denied'), ...] != []
  ... : threads raised: [PermissionError(13, 'Access is denied'), PermissionError(13, 'Access is denied'), ... (13 total)]

  FAIL: test_many_concurrent_registrations_all_survive
  AssertionError: Lists differ: [PermissionError(13, 'Access is denied'), ...] != []
  ... : registration threads raised: [PermissionError(13, 'Access is denied'), ... (18 total)]

  Ran 2 tests in 0.231s
  FAILED (failures=2)
  ```

  Reverted the `contextlib.nullcontext()` edit immediately afterward and confirmed
  `src/pyocd_debug_mcp/remote_probes.py` matches the pre-edit content exactly (checked
  the `_registry_lock` definition and surrounding lines by hand, and confirmed no
  stray `contextlib` import remained).

Definition-of-done, re-run after the rewrite: `python -m unittest discover -s tests`
-- 730 ran, 0 failures, 7 skipped, fully green (the R1 test that was deliberately red
in round 1 is now green because the production fix is correct).
`python -m ruff check src/ tests/` -- clean. `python -m pyright src/` -- clean.
`python -m pyright tests/` -- still exactly 19 pre-existing errors, unchanged.
