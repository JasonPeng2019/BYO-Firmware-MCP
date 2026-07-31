# Iteration 5 — Diff Adversary (safety cap)

Scope: `git diff 6f3da0a..HEAD` (the full feature, 19 commits, 4174/-125 lines across 13
`src/` files) attacked against what
`DEBUGGER_UART_DISCOVERY_HOOK_IMPLEMENTATION_GUIDE.md` actually specifies. `reviews/ledger.md`
(38 rows, C1–C15/D1–D14, all adjudicated) and `reviews/iter-1..4-*.md` read in full first;
nothing already ruled INVALID is re-raised. This is the safety-cap pass: five directed
targets in priority order, then an independent sweep.

## Summary

| ID | Severity | One-line |
| --- | --- | --- |
| D15 | MEDIUM | Step 4's "legacy vendor helpers become a third provenance source" requirement is fully scaffolded and unit-tested but never wired to real data in production — `HardwareInventoryService.vendor_uarts` is left at its `lambda: ()` default at the sole `server.py` construction site. |
| D16 | MEDIUM | The iteration-4 C15 fix (`tests/test_swd_process_isolation.py::test_startup_and_first_call_share_one_absolute_deadline`) moved worker construction inside the same `assertRaises` as the call it's supposed to guard. Empirically proven: when construction itself exhausts the shared deadline, `call()`'s deadline-sharing logic is invoked zero times and the test still passes clean. |

No CRITICAL or HIGH findings. Targets 1, 2 (shared with the code file), 4 are otherwise
clean; details and negative results below.

---

## Target 1 — audit the guide for false baseline claims

This was flagged as the highest-value target: the guide's false claim at lines 494–496
("`configured_probe_cli_commands` already routes pyOCD through `sys.executable`") produced
M4/D14 and survived four passes because it lived in code no diff touched. The brief for
this iteration was explicit: find that claim's siblings.

**Method.** Grepped the guide for every assertion phrased as a claim about pre-existing
behavior (`already`, `currently`, `today`, `now the`, `does now`, `Nothing misbehaves
today`, etc. — 24 hits), then verified each one directly against the code, at whichever
commit the claim is actually about (base `6f3da0a` for "what exists today" claims used to
justify a design decision; HEAD for claims about code the guide says will remain
unchanged). Full list checked:

| Guide claim | Anchor | Verified against | Result |
| --- | --- | --- | --- |
| `_same_setup_connection`/`_setup_connection_key`/`_stable_identity_equal` all casefold both operands already | server.py:4416-4432, 3281 (base) | `git show 6f3da0a:src/pyocd_debug_mcp/server.py` | TRUE — read all three functions, all casefold. |
| Sites 2,4,5,6,7 already funnel through `_validation_inventory()` | §1.1 table | `git show 6f3da0a:...server.py \| grep _validation_inventory(` | TRUE — exactly 4 call sites (969, 3416, 4182, 4519) plus the definition (2433), matching the guide's 5 sites almost line-for-line. |
| `popen_owned` already raises on `shell=True` | processes.py:467 (base) | same file | TRUE — `if kwargs.get("shell"): raise ValueError(...)`. |
| `run_owned` does `process.communicate(timeout=timeout)`, buffers unbounded | processes.py:548 (base) | same file | TRUE. |
| `PERSISTED_AUTHORITY_KEYS` / `ensure_no_persisted_authority` already enforce no-persisted-authority | firmstore/store.py:29,51 (base) | same file | TRUE — recursive Mapping/list walk, raises `PersistedAuthorityError` on any of the listed keys at any depth. |
| `FirmStore._owned_target` refuses writes outside `.firm` | store.py:171 (base) | same file | TRUE. |
| `secrets` already imported in server.py:20 | — | HEAD | TRUE. |
| `PROBE_CLASSES` already imported in probe_inventory.py:9, used as provider source of truth | — | base + HEAD | TRUE, and confirmed the new code (`registered_provider_ids()`, probe_inventory.py:57-65) correctly derives from it, not from `probe_families.json`. |
| `_run_cmd` already returns 124 for timeout, 127 for `FileNotFoundError` | server.py:821 (base) | same file | TRUE. |
| `SerialEndpoint.stable_key()` / `has_stable_identity` already the exact predicates | cache.py:77-95 (base) | same file | TRUE. |
| `normalize_port_name` already strips `\\.\` and lowercases | serial_resolver.py:246 (base) | same file | TRUE. |
| `_validated_identity` (cache.py:217) already raises for non-stable probe/UART | cache.py:217-227 (base) | same file | TRUE — raises `AttachmentCacheError` for either missing field. |
| `CacheResolution(False, "multiple_matches")` already models ambiguity | cache.py:339 (base) | same file | TRUE. |
| `ValidationProbe.choice()` already warns for session-local | validate.py:63-70 (base) | same file | TRUE. |
| `configured_probe_cli_commands` already routes pyOCD through `sys.executable` | probe_families.py:176 | base | **FALSE at base — already found and fixed as M4/D14. Not re-raised; verified the fix (`f4d5ecf`) is still in place at HEAD (see Target 2 below).** |

**Result: no new false claim found.** Every other "already"/"today"/"currently" assertion
in the guide checked out true against the commit it actually describes. The M4 claim
remains the only outright-false one.

**However**, chasing the same claim's *consequences* one level further than iteration 4 did
(iteration 4 confirmed the `sys.executable` fix itself was correctly adopted, but did not
trace every other place step 4 promised behavior it didn't fully deliver) surfaced a
distinct, real gap — not a false claim about *pre-existing* behavior, but an unmet
guide requirement in the *new* code. Reported as D15 below.

---

## D15 — MEDIUM — step 4's "legacy vendor helpers, third provenance source" requirement is scaffolded and tested but never wired to production data

**File:** `src/pyocd_debug_mcp/hardware_inventory.py:225-234` (the `HardwareInventoryService`
dataclass), `src/pyocd_debug_mcp/server.py:3010-3015` (the sole production construction site).

**What the guide requires (step 4, "Legacy vendor helpers," lines 683-688):**

> `SERIAL_FALLBACKS` and its two parsers (`parse_nrfjprog_com_output` `:303`,
> `parse_stm32_programmer_list_output` `:325`) move behind the unified layer as a third
> provenance source, `("vendor:nrfjprog",)` etc. Keep the parsers exactly as they are...
> Keep `PYOCD_SERIAL_FALLBACK_REGISTRY` working for one release, document precedence,
> deprecate only after migration tests pass.

**What was built.** `hardware_inventory.py` fully implements this as a data type and a
merge path:

- `VendorUartRow` (`:160-173`) — `provider_id`, `port_path`, `description`,
  `usb_serial`, `vid`, `pid`, and a `provenance` property returning `f"vendor:{provider_id}"`.
- `HardwareInventoryService.vendor_uarts: Callable[[], Sequence[VendorUartRow]] = lambda: ()`
  (`:233`) — an injectable callable, defaulting to empty.
- `_vendor_uart_rows()` / `_collect_uart_rows()` (`:308-342`) — vendor rows are merged in
  (via `_merge_uart_rows`) whenever native UART rows come back empty, sharing the same UART
  gate hooks use, with a comment explicitly citing the guide's language: "Legacy vendor
  helpers are a third provenance source behind this layer, and share the UART gate so they
  never reach the serial hot path either."
- `tests/test_unified_inventory.py::VendorProvenanceTests` (`:505-560`+) exercises this
  thoroughly: vendor rows fill an empty native inventory, vendor rows are *not* consulted
  when native UARTs exist (proving the gate), and a vendor row + a hook row for the same
  device merge into one row.

**What's missing.** Grepped every call site of `HardwareInventoryService(` in `src/`: there
is exactly one, at `server.py:3010-3015`:

```python
_hardware_inventory = HardwareInventoryService(
    native_probes=lambda: list_connected_probes_detailed(_run_cmd),
    native_uarts=list_serial_ports,
    active_connections=_active_connection_rows,
    hook_snapshot=_hook_snapshot_store.current,
)
```

`vendor_uarts` is never passed. It is the *only* one of the five constructor parameters
left at its default. `grep -rn "VendorUartRow" src/ tests/` confirms `VendorUartRow` is
constructed nowhere in `src/` at all — only in the test file, by hand, with literal
fixture values (`VendorUartRow("nrfjprog", "COM9", "nRF UART", "SER9", 1, 2)`). There is no
function anywhere that calls `serial_resolver.SERIAL_FALLBACKS`,
`parse_nrfjprog_com_output`, or `parse_stm32_programmer_list_output` and turns the result
into a `VendorUartRow`. `serial_resolver.py` itself is not in the diff at all
(`git diff 6f3da0a..HEAD --stat -- src/` — absent from the 13 changed files), despite the
guide's own §2 "Touch" list explicitly naming it.

**Consequence.** In production, `self.vendor_uarts()` always returns `()`, so
`_vendor_uart_rows()` always returns `[]`, so the merge is always a no-op. The unified
inventory snapshot (`InventorySnapshot.uarts`, as surfaced through `_setup_overview`'s
`serial_choices[]`, `_get_setup_status`, and preflight) can never contain a row with
`provenance == ("vendor:nrfjprog",)` or `("vendor:stlink",)`, no matter what
`PYOCD_SERIAL_FALLBACK_REGISTRY` names. The legacy mechanism is not fully dead — it
continues to work through its *original* call path: `resolve_serial_port`
(`serial_resolver.py:556-620`) still calls into `SERIAL_FALLBACKS` internally
(`_resolve_nordic_serial`/`_resolve_stlink_serial`, `:583-594`) and is still invoked by
`_resolve_serial_port_for_session`'s "fall back to the existing scoring" branch
(`server.py:1666-1676`) — so `read_serial`/`write_serial`/`serial_exchange` still correctly
disambiguate among several visible generic serial ports using vendor CLI correlation. What
is missing is *only* the unified-layer surface the guide asked for: agent-facing
`setup_overview` never shows a vendor-tagged provenance row, and `docs/*.md` correspondingly
never mentions `vendor:` provenance (`grep -rn "vendor:" docs/ SERVER_GUIDE.md README.md`
— zero hits), consistent with the feature never having actually shipped end-to-end.

**Verified by reading source, not inferred**, including tracing `_resolve_nordic_serial`
(`serial_resolver.py:470-497`) to confirm its own vendor-CLI correlation logic requires
`ports` (pyserial's already-visible list) to be non-empty (`_find_port_by_name(ports,
entry.port)`), which is a separate, orthogonal observation: even a properly-wired
`vendor_uarts` would only ever populate `InventorySnapshot.uarts` in the case where native
pyserial finds *zero* ports — the exact case the existing vendor-CLI correlation mechanism
is structurally unable to serve (it can only disambiguate among ports pyserial already
sees, never discover a port pyserial can't see). This does not change the finding
(the guide's explicit instruction was still not implemented) but bounds its practical
severity: even fully wired, this specific gating design would rarely if ever produce a
populated vendor row. Flagging both facts together rather than only the wiring gap.

**Failure scenario, concrete:** An operator relies on `PYOCD_SERIAL_FALLBACK_REGISTRY` to
identify Nordic boards' VCOM ports via `nrfjprog --com`. Before this feature,
`_resolve_serial_port_for_session`'s only caller (the UART hot path) already used
`resolve_serial_port`, which already worked. After this feature, that exact behavior is
unchanged (still works, same code path) — but the promised new capability, an agent
calling `setup_overview` and seeing which UART candidate the vendor tool identified
*before* selecting or connecting, never appears; every serial choice looks identically
generic regardless of vendor correlation, and nothing in the contract or docs explains why.

**Severity rationale:** MEDIUM, not HIGH — the pre-existing, load-bearing behavior
(`resolve_serial_port`'s internal vendor lookup for the actual read/write UART path) is
provably intact and unaffected; this is a shipped-incomplete *display/diagnostic* feature
relative to an explicit guide instruction, backed by tests that create the appearance of
completion without production wiring, not a regression that breaks working hardware
access.

---

## Target 3 — did the C15 fix weaken its own tests?

Directed question: worker construction was moved inside the same `assertRaises` as the
call under test in `test_startup_and_first_call_share_one_absolute_deadline`. If
construction itself can raise the expected exception type, the deadline behavior is never
actually exercised. Prove it, don't just suspect it. See **D16** below — proven
empirically, not inferred.

Also asked: is the 2.0s/2.5s widening loose enough to pass even if the shared deadline
isn't shared at all? Tested this directly (script in scratchpad, not touching any repo
file): monkeypatched `_WorkerClient.call` to ignore its caller-supplied `deadline` entirely
and compute a fresh `time.monotonic() + 10` instead, then ran the exact sequence the test
runs. Result: raised after **10.27s**, so `self.assertLess(elapsed, 2.5)` fails — **the
widened window does not mask a fully-broken deadline share**. The widening itself is not
the problem; see D16 for the actual gap, which is orthogonal to the widened numbers.

---

## D16 — MEDIUM — the C15 fix's regression test can pass without exercising the invariant it names

**File:** `tests/test_swd_process_isolation.py:310-315`
(`test_startup_and_first_call_share_one_absolute_deadline`), fixed by commit `2e07463`
(iteration 4). Production code under test: `src/pyocd_debug_mcp/adapters/swd_process.py`
(`_WorkerClient.__init__`, `:219-245`; `_invalidate`, `:339-354`, typed `NoReturn`).

**The fix, as committed:**

```python
started = time.monotonic()
deadline = started + 2.0
with self.assertRaises(TargetConnectionError):
    client = self._client("ready_then_hang", deadline=deadline)
    client.call("get_state", {}, deadline=deadline)
self.assertLess(time.monotonic() - started, 2.5)
```

The pre-fix version (`git show 2e07463^:tests/test_swd_process_isolation.py`) had
construction *outside* `assertRaises`:

```python
deadline = started + 0.5
client = self._client("ready_then_hang", deadline=deadline)   # NOT inside assertRaises
with self.assertRaises(TargetConnectionError):
    client.call("get_state", {}, deadline=deadline)
self.assertLess(time.monotonic() - started, 0.7)
```

**Why this matters.** `_WorkerClient.__init__` (`swd_process.py:239-245`) itself performs
a deadline-bound blocking read of the worker's startup handshake and, on any exception —
including its own `startup_deadline` expiring — calls `self._invalidate(...)`, whose return
type is `NoReturn`: it *always* raises `TargetConnectionError` (`:339-354`). So construction
alone, not just `.call()`, can and does raise the exact exception type the test asserts on.

**Proven, not inferred — reproduced against the actual `_WorkerClient` class, not a
substitute:**

1. Measured normal construction time for `ready_then_hang`: **~0.27s**, comfortably under
   the widened 2.0s startup budget (headroom improved roughly 4x versus the original
   0.5s/~0.27s ratio — this is why the widening genuinely reduces the *organic* flake rate).
2. Forced the exact scenario the coordinator asked about: passed an already-expired
   deadline (`time.monotonic() - 0.001`) to `_client(...)`, wrapped construction + `.call()`
   in `assertRaises(TargetConnectionError)` + `assertLess(elapsed, 2.5)` exactly as the real
   test does, and instrumented `.call()` with a spy to count invocations.
   **Result: `TargetConnectionError` was raised (from `__init__`, via `_invalidate`),
   `.call()` was invoked 0 times, elapsed was 0.031s, and `assertLess(elapsed, 2.5)` passed
   cleanly.** The test's two assertions are both satisfied while the code path they exist
   to guard — `.call()` reusing the caller's absolute deadline rather than computing a
   fresh one — was never reached.

**Consequence.** Before the fix, this exact scenario (construction alone exhausting the
budget) surfaced as a test **ERROR** — visible, distinguishable from a real assertion
failure, and (per the iteration-4 ledger entry) is precisely what caused the observed
~1-in-5 flake. The fix's structural change — moving construction inside `assertRaises` —
converts that same scenario from a loud ERROR into a silent PASS. The widened margin
(2.0s vs. 0.5s, ~7.4x more headroom before construction alone could exhaust it) makes this
scenario much less likely to occur *organically* than before, which is real and valuable —
but it does not close the structural gap, it only shrinks the window in which it matters.
If a future regression in `_WorkerClient.call`'s deadline handling coincided with any
degree of construction-side slowness (a loaded CI runner, antivirus scanning a freshly
spawned `python.exe`, etc.), this specific test would report green while giving zero
signal that `.call()`'s deadline-sharing logic ran at all.

**Scope check — does anything else in the suite independently guard this invariant?**
`test_actual_worker_dispatches_short_request_while_stdin_remains_open` and other tests in
the same file construct `_WorkerClient` with a *generous* deadline (`time.monotonic() +
10`) and don't attempt to race it, so they don't cover the shared-deadline contract either.
No other test in `tests/test_swd_process_isolation.py` (or elsewhere, grepped for
`_WorkerClient` and `startup_deadline`) exercises "construction succeeds, then `.call()`
fails at the *same* originally-supplied absolute deadline" as a scenario distinct from this
one. This is the only test covering that specific invariant, and it is the one with the
gap.

**Severity rationale:** MEDIUM. Test-only; no production defect. The masking requires a
compound coincidence (construction-side slowness at the same moment a deadline-sharing
regression would otherwise be caught) that the widened margin makes materially less likely
than before. But it is a real, reproduced structural weakness in the one test that guards a
named, previously-flagged-as-concerning production timing contract ("more concerning than
[the other C15 flake] because the timeout being raced is production code," per the
iteration-4 ledger entry), and the fix's own framing ("the invariant under test is that the
shared deadline is honored, not specifically where") is the thing that's actually not true
of the test as written — it can no longer *prove* the deadline was honored across the
startup/call boundary specifically, only that some `TargetConnectionError` occurred within
budget from *some* cause.

---

## Target 2 — probe_families.py cold review

Covered jointly with the code-adversary pass; see `reviews/iter-5-code.md` for the full
cold review (frozen interpreter, spaces in path, `PYOCD_CLI` override coverage, `-m pyocd`
argv/exit-code shape versus `probe_inventory.py`'s exit-124 dependency). Summary for the
diff lens specifically: M4's fix (`f4d5ecf`) is unchanged since iteration 4 and still
correctly in place at HEAD (`probe_families.py:161-180`); nothing in the diff since then
touches this file. **No new diff-side finding.**

---

## Target 4 — ledger integrity

Spot-checked ledger descriptions against HEAD rather than re-verifying all 38 rows from
scratch (iteration 4 already did a full skip/test-integrity audit):

- `MAX_HOOKS_TOTAL = 48` (`discovery_hooks.py:65`) — matches C4/FIX 4's description.
- `MAX_PROBE_SELECTIONS = 256` (`hardware_inventory.py:590`) — matches C2/FIX 2.
- `PROBE_CONNECTION_PREFIX = "probeid:"` / `LEGACY_PROBE_CONNECTION_PREFIX = "probe:"`
  (`services/connections.py:21,26`) — matches C12/D11's structural-prefix fix, with the
  docstring at `parse_probe_connection_id` (`:52-60`) explicitly narrating the exact defect
  it replaced.
- `probe_connection_id(provider, probe_uid)` (`connections.py:33`) — matches C7/D8/FIX 8's
  provider-qualified signature.
- C14/D13's fix — `tests/fake_discovery_hook.py:53` catches
  `(FileExistsError, PermissionError)` — present, matches description.
- C15's fix — all three `subprocess.run(...)` sites in
  `tests/test_swd_process_isolation.py` (lines 68, 108, 167) use
  `timeout=DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS`, not a bare `10` — present, matches
  description. (The fourth site, the shared-deadline test, is the subject of D16 above —
  the ledger's C15 entry describes *what was changed* accurately; it just didn't anticipate
  the new structural gap the change itself introduced, which is exactly what D16 exists to
  add.)
- D14's resolution ("the tree now legitimately reproduces 617 passed / 7 skipped from a
  clean checkout") — reconfirmed: `git status --short` shows only the untracked
  `reviews/RESUME.md` (a review artifact, not source), zero uncommitted/untracked
  `src`/`tests` files. Matches.

**No ledger drift found.** One addition recommended: once D16 is adjudicated, its
resolution should be cross-referenced from the existing C15 row so a future reader doesn't
mistake "C15: fixed" as meaning the shared-deadline test is airtight.

---

## Target 5 — independent sweep

Beyond the four directed items, re-read `discovery_failures.py`'s structural guarantee that
`probe/open-failed`/`uart/open-failed` payloads cannot carry `hook_contract_call`
(`open_failure_payload`, `:277-308`) — confirmed the payload dict is built from a fixed,
enumerable key set with no code path that adds that key, plus a defense-in-depth `assert`.
Matches the guide's "structurally impossible, not merely documented" requirement (§8).

Re-read `tools/discovery.py::DiscoveryRetryStore` (`:88-177`) end to end against the guide's
retry-store spec (§2): bounded via `OrderedDict` + oldest-evict-on-insert, TTL checked on
read (not on a background sweep, so no timer/thread to leak), kind-mismatch refuses without
consuming the ticket (lets the agent retry with the right kind), `claim`/`consume`/`issue`
all serialize under one `RLock`. No defect found.

No further findings beyond D15/D16.
