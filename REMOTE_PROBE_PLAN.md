# Remote probe endpoints - plan and implementation guide

## 1. The problem this solves

When pyOCD cannot see a debug probe on the local USB bus, no amount of server code
fixes it. The discovery-hook fallback lets an agent write a script that *finds* a
device, but a script cannot make an unopenable device openable -- if the local libusb
stack is broken, a hook that reports the probe still fails at `open_session`.

There is exactly one route that survives that case. pyOCD ships a probe server
(`pyocd server`) that owns the probe on one machine and serves it over TCP. A client
addresses it as `remote:<host>:<port>`, and `TCPClientProbe` constructs the connection
without touching USB at all.

This is the escape hatch for:

- a Python environment whose libusb/driver cannot see the probe, while another
  environment on the same machine can;
- WSL or a container reaching a probe owned by the Windows/host side;
- a probe physically attached to a different machine.

**Verified working on this machine before this document was written.** With
`pyocd server -p 5555 -u 0668FF514988525067213913` running against the attached
ST-LINK, `ConnectHelper.session_with_chosen_probe(unique_id="remote:localhost:5555")`
opened a session and read the core program counter back as `0x80015de`. This is not a
theoretical route.

## 2. What exists today, and the actual gap

The hook contract already documents the `remote:` form (`unique_id_guidance` in
`tools/discovery.py`), and a `remote:` selector already survives the whole pipe -- hook
row to snapshot to token to `resolve()` to `open_session` -- proven by
`test_a_provider_qualified_remote_selector_survives_the_whole_pipe`.

**The gap is the delivery mechanism, not the plumbing.** To use the escape hatch today
an agent must author a discovery hook: a Python file plus a manifest, written to
`.firm/discovery_hooks/`, executed as a subprocess, whose entire job is to print a
constant that the agent already knew. There is nothing to discover. That is ceremony
standing between the user and the one route that works.

The charter names the right shape directly: *"If auto-detection fails, expose a tool
for the agent to supply the missing piece -- and persist it so the gap closes
permanently."* A registration tool is that tool. A script that prints a constant is not.

## 3. Design

Two new MCP tools and one small persistent registry.

### `register_remote_probe(host, port=5555, description=None)`

Records a probe-server endpoint so it appears as a normal probe row in every inventory
snapshot, and persists it so the gap stays closed across restarts.

**Verification policy -- read this carefully, it is the most likely thing to get wrong.**
The tool attempts to reach the endpoint and **reports honestly what it found, then
registers either way.** It does *not* refuse to register an endpoint it could not
reach.

The reasoning is the charter's own worked example: a guard fires on a *verified*
contradiction, never on an *unverified* suspicion. An endpoint that does not answer
right now is not proof of a mistake -- the user may be about to start the server, or
starting it in the order the agent suggested. Refusing would be paternalism. But
silently registering an unreachable endpoint and implying success would be fabrication.
So: probe it, say plainly in the response whether it answered, register it regardless.

The response must make the distinction unmissable, e.g. `"reachable": true|false` plus
an `agent_prompt` that tells the agent what to do next in each case.

### `unregister_remote_probe(host, port=5555)`

Removes an entry. Not speculative: without it a typo'd endpoint is permanent and
pollutes every snapshot forever, which is a real usability defect.

### The registry

A small JSON file. One home for the responsibility, following the existing FirmStore
layout pattern.

## 4. Implementation guide

### 4.1 New module: `src/pyocd_debug_mcp/remote_probes.py`

```python
@dataclass(frozen=True, slots=True)
class RemoteProbeEntry:
    host: str
    port: int
    description: str
    registered_at: str   # ISO-8601 UTC

    @property
    def selector(self) -> str:
        return f"remote:{self.host}:{self.port}"
```

Functions:

- `load_remote_probes(path: Path) -> tuple[RemoteProbeEntry, ...]` -- missing file is
  **empty tuple, not an error**. A malformed file must not crash discovery: log/skip
  and return what parsed. Discovery failing closed because a registry file got
  corrupted would be a worse bug than the one being fixed.
- `save_remote_probes(path: Path, entries: Sequence[RemoteProbeEntry]) -> None` --
  create parent dirs; write atomically if the repo has an existing helper for that,
  otherwise a plain write is acceptable.
- `check_endpoint(host: str, port: int, timeout_seconds: float) -> bool` -- a plain
  bounded `socket.create_connection`. **Do not** import pyOCD here or try to open a
  session; a TCP accept is the honest, cheap signal, and anything heavier belongs at
  connect time.

Normalization: strip the host, reject empty; port must be an int in `1..65535`. These
are correctness checks on a value that becomes a network address, not hostility
guarding. Deduplicate on `(host.casefold(), port)` -- re-registering an existing
endpoint updates its description rather than creating a duplicate row.

### 4.2 Registry location: `src/pyocd_debug_mcp/firmstore/store.py`

Add one field to `FirmLayout`, alongside the existing ones:

```python
remote_probes: Path
...
remote_probes=root / "remote_probes.json",
```

It is **not** under `discovery_hooks/` -- it is not a hook, it is not executed, and it
is not subject to hook source hashing or the per-kind gate.

### 4.3 Inventory integration: `src/pyocd_debug_mcp/hardware_inventory.py`

Add a field to `HardwareInventoryService`:

```python
remote_probes: Callable[[], Sequence[RemoteProbeEntry]] = lambda: ()
```

The default is what preserves every existing test.

In `snapshot()`, merge remote rows into `probe_rows` through the existing
`_merge_probe_rows`. Build them with a `_remote_probe_rows(counter)` helper shaped like
`_hook_probe_rows`:

```python
ProbeRow(
    provider="remote",
    probe_id=entry.selector,
    unique_id=entry.selector,      # the full remote:host:port selector, verbatim
    row_id=counter.next(),
    description=entry.description,
    stable_identity=entry.selector,
    provenance=(f"remote:{entry.host}:{entry.port}",),
    hook_source_sha256=None,
    identity_scope="stable",
    snapshot_id=counter.snapshot_id,
)
```

**Three decisions that are deliberate. Do not "improve" them:**

1. **Remote rows are NOT gated behind "native discovery came back empty."** Hook rows
   are, because running subprocesses on every snapshot is expensive and hooks are
   fallback *discovery*. A remote endpoint is an explicit registration, costs one file
   read, and must stay visible even on a machine that also has a local probe --
   otherwise a user with one working local probe could never reach a second remote one.

2. **`unique_id` carries the full `remote:host:port` selector, prefix included.**
   `TCPClientProbe.get_probe_with_id` returns `None` unless `is_explicit`, and
   `is_explicit` is only set when the prefix is present. Strip the prefix and the
   feature silently stops working.

3. **`uart_snapshot()` is untouched.** It returns `probes=()` by contract and must keep
   doing so.

No reachability check runs during a snapshot. Snapshots must stay cheap and must not
make network calls on the discovery path.

### 4.4 Timeout budget: `src/pyocd_debug_mcp/kernel/operations.py`

**No change needed, and this is intentional.** `register_remote_probe` performs one
bounded TCP connect against the default 30s operation timeout, which is ample. It does
not take an inventory snapshot. Do not add it to `_PROBE_INVENTORY_TOOLS`.

Reading the registry file adds no measurable cost to `snapshot()`, so the existing
`_PROBE_INVENTORY_TOOLS` budget stays correct.

### 4.5 Tools: `src/pyocd_debug_mcp/tools/remote_probes.py`

Mirror `tools/discovery.py` exactly -- injected services dataclass, handlers built by a
factory, JSON string returns. That file is the local convention; follow it rather than
inventing a second style.

```python
@dataclass
class RemoteProbeToolServices:
    registry_path: Callable[[], Path]
    check_endpoint: Callable[[str, int], bool] = ...
```

Register in `server.py` next to the discovery handlers, with the **same** treatment and
for the same reasons (the existing comment block there explains it):

- no `tool_registry.configure(...)` -- always visible, never locked. An agent told that
  discovery found nothing must reach this without unlocking anything.
- no `mcp.configure_layer2(...)` -- registration is configuration, not a hardware
  action, and must not report through the hardware-failure envelope.
- `forbid_unknown_tool_arguments(mcp, name)` for both.

Docstrings are the tool descriptions and must state, per the charter: what it does, when
to reach for it, parameters with units and an example, what it returns, and the common
failure modes with recovery. Include the fact that the user must run `pyocd server` on
the machine that owns the probe, and that on Windows that process needs
`PYTHONIOENCODING=utf-8` or it crashes printing its own probe table (observed, not
hypothetical).

### 4.6 Contract cross-reference: `src/pyocd_debug_mcp/tools/discovery.py`

In `_UNIQUE_ID_GUIDANCE["remote_probes"]`, add one sentence pointing at
`register_remote_probe` as the direct route, so an agent reading the hook contract
learns it does not need to write a hook for this case.

**Do not touch `discovery_failures.py` or `_no_native_probe_overview`.** That file is
318 lines of remedy text that generated two full rounds of review churn on its own, and
the tools are always visible without it. The cross-reference above is enough.

## 5. Tests

Stdlib `unittest` only -- never pytest. ruff line-length 100, target py310.

**Every test must be proven able to fail.** Break the behavior it guards, watch it fail,
revert exactly. This codebase has already produced four tests that passed for the wrong
reason. Do not skip this step, and report which tests you proved and how.

Required coverage:

1. **The no-registration invariant.** With an empty/absent registry, `snapshot()` is
   byte-identical to today -- same rows, no extra file reads that change behavior. This
   is the single most important test; it is what makes the change safe.
2. Round-trip: register, load, appears in `snapshot()` as a `remote` provider row with
   the full selector as `unique_id`.
3. The selector survives `ProbeSelectionStore.resolve()` and reaches `open_session`
   unmangled (extend the existing pattern in `test_discovery_hook_workflow.py`).
4. Re-registering the same `host:port` updates rather than duplicating.
5. `unregister_remote_probe` removes it and the row disappears from the next snapshot.
6. A malformed/corrupt registry file does not crash discovery.
7. Unreachable endpoint still registers, and the response says `reachable: false`.
8. Port and host validation rejects empty host and out-of-range ports.
9. Remote rows appear **even when a native probe is present** (the anti-gating test).

## 6. Practical hardware test

There is a real ST-LINK on this machine (`0668FF514988525067213913`), and it is
natively discoverable -- which makes it *ideal* for this, because `pyocd server` can
serve it and the client half is exercised for real.

Write this as a **manually-run script, not part of the default suite** (it needs
hardware and a spare TCP port; the suite must stay hermetic). Put it somewhere obvious
such as `scripts/` or `tests/manual/`, and document how to run it.

It should:

1. Start `pyocd server -p <port> -u 0668FF514988525067213913` as a subprocess with
   `PYTHONIOENCODING=utf-8` in its environment (**required on Windows** -- without it
   the server dies with a `charmap` codec error while printing its probe table).
2. Poll the port with `socket.create_connection` until it accepts. Do not sleep a fixed
   amount.
3. Call `register_remote_probe("localhost", port)` and assert `reachable` is true.
4. Take a real `snapshot()` and assert the remote row is present with the right selector.
5. Open a session through the server's own path and read something real back off the
   target (the core PC works -- `0x80015de` on this board).
6. `unregister_remote_probe` and assert the row is gone.
7. **Always** terminate the server subprocess in a `finally`, with a `kill()` fallback.

Note for step 5: a remote probe reports no board identity, so pyOCD falls back to a
generic `cortex_m` target and warns that flash programming is unavailable. That is
expected and correct -- the real target comes from the board profile on the normal
setup path. Do not add special-case code for it.

## 7. Non-goals - do not build these

- No URL parsing, no `remote://` scheme handling. Host and port are two parameters.
- No auto-discovery of probe servers (no mDNS, no port scanning).
- No server-side composition of `provider:uid` for hook rows. This was considered and
  rejected: the server holds both fields, but composing would turn a row whose
  `provider` is a good guess rather than a fact into a hard failure, where today it
  still resolves by falling through to another provider class.
- No auto-starting of `pyocd server`. The user owns that process.
- No reachability polling, health checks, or reconnection logic on the snapshot path.
- No changes to `discovery_failures.py` or the no-probe overview.

## 8. Definition of done

- Every test above passes, and each new test was demonstrated able to fail.
- `python -m unittest discover -s tests` green.
- `ruff check src/ tests/` clean.
- `pyright src/` clean, and **no new errors in `tests/`** (there are exactly 19
  pre-existing; that number must not go up).
- The hardware script runs green against the attached ST-LINK.
- No behavior change whatsoever when no remote probe is registered.
