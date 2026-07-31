# Iteration 1 — Code Adversary

Scope: the resulting code as it stands, independent of the diff. Focused primarily on
`discovery_hooks.py`, `hardware_inventory.py`, `tools/discovery.py`,
`discovery_failures.py`, `probe_inventory.py`, `kernel/operations.py`, and the
discovery-hook wiring in `server.py`.

## Summary

| Severity | Count |
| --- | --- |
| CRITICAL | 2 |
| HIGH | 1 |
| MEDIUM | 2 |
| LOW | 2 |

---

## C1 — CRITICAL — `_resolve_serial_port_for_session` unconditionally spawns a probe-listing subprocess on every UART action

**File:** `src/pyocd_debug_mcp/server.py:1614-1631`, `src/pyocd_debug_mcp/hardware_inventory.py:231-237`

```python
# server.py:1631
snapshot = _hardware_inventory.snapshot()
```

```python
# hardware_inventory.py:231-242
def snapshot(self) -> InventorySnapshot:
    ...
    native_listing = self.native_probes()      # unconditional, every call
    probe_rows = self._native_probe_rows(native_listing, counter)
    native_ports = self.native_uarts()
    ...
```

`native_probes` is wired to `lambda: list_connected_probes_detailed(_run_cmd)`, which runs every command from `configured_probe_cli_commands()` as a real subprocess (`_run_cmd`), typically `sys.executable -m pyocd list --probes`, with up to `DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS` (30s) per configured command.

`_resolve_serial_port_for_session` is bound as `resolve_port` for `read_serial`, `write_serial`, `serial_exchange` (`server.py:2204, 2240, 2513`) and is called directly by the `on_exit` UART finalizer (`server.py:6126`). Every one of those tools now spawns a probe-enumeration subprocess before doing any UART work at all — including on a board that has no debug probe connected, and with zero discovery hooks configured.

**Failure scenario:** a project with no `hooks.json`, running `read_serial(board_id="x", read_seconds=1)` on a board that only has a UART bridge (no probe). Before this change: pyserial enumeration only, sub-millisecond. After this change: the same call first blocks on a full pyOCD probe-listing subprocess (real process spawn, module import, USB enumeration) before touching the serial port. If that subprocess is slow (loaded CI machine, USB controller hiccup, antivirus interception on Windows) or hangs up to the 30s ceiling, `read_serial` is measurably slower or spuriously cancelled by its own operation deadline (see C-adjacent operations.py budget: `_UART_ACTION_TOOLS` only adds `_hook_budget("uart")`, never the native-listing time — a `read_serial(read_seconds=3)` budget of ~5-8s can be exhausted entirely by the probe-listing call before the requested read starts).

**Untested:** `grep -rn "_resolve_serial_port_for_session" tests/` returns zero matches — no test calls this function, mocked or otherwise, so this regression is invisible to the suite. The one "no manifest, byte-identical behavior" regression class (`NoHookConfigurationTests` in `tests/test_discovery_hook_safety.py`) only re-derives the *timeout formula*, never the actual subprocess call graph.

---

## C2 — CRITICAL — `ProbeSelectionStore` has no bound, TTL, or eviction; grows unboundedly for UID-less probes over a long-running server

**File:** `src/pyocd_debug_mcp/hardware_inventory.py:529-541`

```python
class ProbeSelectionStore:
    __slots__ = ("_guard", "_selections")
    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._selections: dict[str, ProbeSelection] = {}
```

`record()`/`recorded()`/`forget()`/`clear()` are the only operations. There is no `MAX_*` cap and no TTL anywhere in this class, unlike `DiscoveryRetryStore` in the same change (`tools/discovery.py:88-136`), which is explicitly bounded (`MAX_RETRY_CONTEXTS = 32`, oldest-evicted-on-insert) with the design rationale spelled out: "a long-lived server cannot accumulate tickets."

The only thing that ever empties `ProbeSelectionStore` is `_on_discovery_hooks_refreshed` (`server.py`, wired via `on_refresh=` in the `DiscoveryToolServices` construction), which fires only when `refresh_discovery_hooks` is called — an action a server whose native discovery works normally may *never* invoke in its entire process lifetime.

`_setup_overview` records one selection per distinct probe `connection_id` it sees, every time it is called (`server.py:4912-4914`, `_probe_selection_store.record(_selection_for_validation_probe(connection_id, probe, snapshot))`). For a UID-less provider (no `probe.usb_serial`), `connection_id` falls back to `probe.probe_id`, which for a server-owned active connection is `connection.connection_id` — and `stable_connection_identity()` (`services/connections.py:26-38`) mints that as `f"session:{metadata.runtime_token}"`, where `runtime_token = uuid4().hex` (`adapters/swd_interface.py:61`) — **freshly random on every connect**.

**Failure scenario:** a board using a UID-less provider (the codebase treats this as a first-class case throughout — `identity_scope="session"`, `ValidationProbe.choice()` warnings, etc., not a corner case) is connected, disconnected, and reconnected repeatedly over a long-running server session (a realistic dev-loop or CI pattern), with `setup_overview` polled between cycles as the guide's own workflow prescribes. Each cycle mints a new `session:<uuid>` connection_id and adds a permanent entry to `_probe_selection_store._selections` that nothing ever removes. Over a long enough run this is unbounded memory growth — the exact class of defect `DiscoveryRetryStore` was deliberately hardened against in this same PR, left unaddressed one class away.

`SessionUartSelectionStore` (`hardware_inventory.py:622-670`) does not have this problem — it is keyed by `board_id`, which is naturally bounded by the number of boards, and each `record()` overwrites the prior entry for that board rather than accumulating. `ProbeSelectionStore`'s key space is unbounded by construction for any UID-less provider, which is the asymmetry worth fixing (e.g., cap + LRU eviction analogous to `DiscoveryRetryStore`, or key session-scope entries by `board_id` the way `SessionUartSelectionStore` does).

---

## C3 — HIGH — Hook execution failures (OSError/PermissionError) and the pre-existing `BoardNotConnectedError` TOCTOU propagate as unhandled exceptions on several call sites, defeating the typed-failure-response guarantee

**Files:** `src/pyocd_debug_mcp/discovery_hooks.py:928-946`, `src/pyocd_debug_mcp/tools/discovery.py:359-362`, `src/pyocd_debug_mcp/server.py:1052-1060, 1614-1631`

Two distinct exception sources reach the same conclusion — an unhandled exception surfaces instead of the typed `discovery/*` failure payload the guide's step 8 promises:

**(a) `execute_hook` does not guard `_execute`:**

```python
# discovery_hooks.py:928-946
def execute_hook(spec, *, marker_store=None) -> HookExecution:
    try:
        current_digest = hook_source_digest(spec)
    except DiscoveryHookError as exc:
        return _refusal(spec, "source_changed", ...)
    if current_digest != spec.file_sha256:
        return _refusal(spec, "source_changed", ...)
    raw = _execute(spec, spec.command(), marker_store=marker_store)   # <-- unguarded
```

`_execute` (`discovery_hooks.py:834-846`) calls `popen_owned(argv, ...)` as its first statement, *outside* its own `try:` block (the `try:` starts after `process, marker = popen_owned(...)` returns). `subprocess.Popen` construction inside `popen_owned` is itself unguarded against `OSError`/`PermissionError`/`FileNotFoundError`. Concretely: an operator `runner: "executable"` hook that loses its execute bit (`chmod -x`) *after* the refresh that admitted it hashes identically (the SHA-256 check only covers file *content*, not the permission bits), so `hook_source_digest` matches and the drift guard does not trigger — but the subsequent `Popen` raises `PermissionError` on POSIX, which propagates out of `execute_hook` uncaught.

**(b) `refresh_discovery_hooks` does not guard `run_hooks`:**

```python
# tools/discovery.py:359-362
for hook_kind in sorted(discovery_hooks.SUPPORTED_KINDS):
    if snapshot.has_hooks_for(hook_kind):
        executions.extend(services.run_hooks(snapshot, hook_kind))
```

No try/except. Any exception out of (a) crashes the entire `refresh_discovery_hooks` call — the one tool the guide designates as the safe, always-reachable fallback path an agent uses precisely *because* native discovery and everything else has already failed. Instead of the promised `discovery/hook-failed` payload with the hook's friendly ID, failure class, and exact retry call (guide step 8 table), the agent gets an unhandled-exception error with no typed remedy. State is not corrupted and no authority is granted (the retry ticket, if any, is simply never consumed and remains valid for another attempt), but the specific contract — "a hook fails ... name that hook's friendly ID, failure class, and exact repair/retry call" — is not met for this class of failure.

**(c) The pre-existing `BoardNotConnectedError` TOCTOU (declared as K1 in `reviews/phase0-notes.md`) is reachable, unguarded, from two call sites that did not have this exposure before this change:**

```python
# server.py:1052-1060  (_resolved_probe_uid_for_connection, used by _setup_continue)
snapshot = _hardware_inventory.snapshot()
```
```python
# server.py:1614-1631  (_resolve_serial_port_for_session, the UART hot path)
snapshot = _hardware_inventory.snapshot()
```

Both call `.snapshot()` → `_active_connection_rows()` → `connection_manager.assigned_board_ids()` (snapshots keys under lock) then `connection_manager.connection_for(board_id)` per id (re-acquires the lock separately per board, `services/connections.py:107-116, 155-157`). If any *other* board disconnects between those two calls, `connection_for` raises `BoardNotConnectedError`, uncaught here. Before this change, `_resolve_serial_port_for_session` never called `_validation_inventory`/`_active_connection_rows` at all (confirmed against base commit `6f3da0a` — pyserial-only), and the old `_setup_continue` string-manipulation path (`connection_id.removeprefix("probe:")`) did no inventory scan whatsoever. Both are therefore *new* exposure to this race, not a preservation of an existing one. Contrast with `_setup_overview` and `_get_setup_status`, which wrap their entire inventory section in `except Exception` and degrade to a diagnostic message — the pattern these two new call sites should also follow but don't.

---

## C4 — MEDIUM — Hook execution is strictly sequential; wall-clock cost of a refresh scales as the sum, not the max, of eligible hook timeouts

**File:** `src/pyocd_debug_mcp/discovery_hooks.py:1034-1047`

```python
def execute_eligible_hooks(snapshot, kind, *, platform=None, marker_store=None):
    selected = platform or current_platform()
    return tuple(
        execute_hook(spec, marker_store=marker_store)
        for spec in snapshot.eligible(kind, selected)
    )
```

A plain generator expression — hooks run one after another, never concurrently. `MAX_HOOKS_PER_MANIFEST = 32` applies per manifest, and there are two manifests (project + operator), each independently capable of declaring up to 32 hooks of a single kind, each with `timeout_seconds` up to `MAX_HOOK_TIMEOUT_SECONDS = 60.0`. `refresh_discovery_hooks` runs every eligible hook of *both* kinds (`tools/discovery.py:360-362`). Worst case (not prevented anywhere): 64 hooks × 60s = 3840s (~64 minutes) of wall-clock time for one `refresh_discovery_hooks` call, before any per-hook failure is even reported back.

The `_hook_budget()` formula in `kernel/operations.py:85-108` is internally consistent with this (it sums `total * (MAX_HOOK_TIMEOUT_SECONDS + MAX_OWNED_PROCESS_CLEANUP_SECONDS)` rather than taking a max), so this is not a timeout-miscalculation bug — the operation deadline is correctly sized for the sequential design. It is, however, an unadvertised scalability cliff: nothing in the manifest schema, the contract tool's `limits` payload, or the docs caps the *total* number of hooks across both sources, and nothing suggests to an operator/agent that hook execution is serial. A moderately-sized operator registry (a plausible real deployment: one hook per supported vendor tool) could make every `setup_overview`/`connect`/`board_validate` call on a machine with no native probe visibly hang for minutes at a time, entirely within the bounds the code enforces.

---

## C5 — LOW — Content hash does not cover permission bits; a chmod-only change is not "source changed"

**File:** `src/pyocd_debug_mcp/discovery_hooks.py:474-496` (`_hash_hook_file`)

The drift-detection hash (`hashlib.sha256` over file bytes) is, by construction, blind to the executable bit. For `runner: "executable"` hooks, an operator toggling `chmod -x`/`chmod +x` on the entrypoint between a refresh and a later execution is invisible to `hook_source_digest` — the hash still matches, so `execute_hook` proceeds to `_execute`, which then fails with an unhandled `PermissionError` (see C3a) rather than the intended, clearly-labeled `discovery/hook-source-changed` refusal. This is a narrow gap in an otherwise careful drift-detection design; folding a permission/executability check into the pre-execution verification (alongside the content hash) would close it and route the failure through the typed path instead of C3's uncaught-exception path.

---

## C6 — LOW — `hooks_available` parameter is dead (always `True`)

**File:** `src/pyocd_debug_mcp/discovery_failures.py:127-179`

Same finding as D6 in the diff review, included here for completeness of the code-as-it-stands lens: `no_native_probe_failure`/`no_native_uart_failure` accept `hooks_available: bool`, but every call site (`server.py:4532, 4812`, and all test call sites) passes `True`. Nothing in the current code path ever computes this from whether a hook manifest is actually loaded. Functionally harmless (the guide's intent is to always offer the contract call when native discovery is empty, which this satisfies), but the parameter's presence implies a conditionality that does not exist anywhere in the call graph — worth either wiring it to real state or removing it.

---

## Areas checked with no findings

- **Resource leaks in `discovery_hooks.py:_execute`:** the descriptor-close-after-join discipline is correct and independently verified by `tests/test_discovery_hook_process.py::test_repeated_execution_does_not_leak_descriptors` using real `psutil` handle counts (ran the suite; passes). Memory capping (`_CappedReader`) correctly bounds retained bytes regardless of child output volume; verified by the suite's RSS-based test.
- **Command injection / shell:** `popen_owned` raises on `shell=True` (`kernel/processes.py:467`); hook argv is always a list, never shell-interpolated. No finding.
- **Path traversal / symlink escape:** `_contained_entrypoint` (`discovery_hooks.py:499-523`) resolves against the hook root, checks `is_relative_to`, and additionally compares `os.path.realpath` of both root and resolved file — correctly closes the symlink-escape gap that `is_relative_to` alone would miss. No finding.
- **Race conditions in the stores actually bounded:** `DiscoveryRetryStore`, `HookSnapshotStore`, `SessionUartSelectionStore` are all correctly guarded with `RLock` and (for the retry store) bounded with eviction. Only `ProbeSelectionStore` (C2) is the exception.
- **`InventorySnapshot` atomicity:** verified both by code reading and by `tests/test_inventory_snapshot_concurrency.py`, which uses real threads and per-call-incrementing fixtures (not constant stubs), a meaningfully strong test.
- **Backward compatibility:** `list_connected_probes_cli` retained as a thin wrapper over `list_connected_probes_detailed`; `SERIAL_FALLBACKS`/`PYOCD_SERIAL_FALLBACK_REGISTRY` consumption path in `resolve_serial_port` is untouched (confirmed by reading `serial_resolver.py` call sites — unchanged). No finding.
