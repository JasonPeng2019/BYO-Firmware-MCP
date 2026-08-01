# Code implementation guide — discovery hooks in BYO-Firmware-MCP

Companion to `DEBUGGER_UART_DISCOVERY_HOOK_PLAN.md`. The plan says *what* and *why*;
this says *where in the code* and *in what order*, with the traps that the current
implementation actually contains.

All paths are relative to `BYO-Firmware-MCP/`. Line numbers are from the state of the
tree at the time of writing — treat them as anchors to search near, not as addresses.

---

## 0. Ground rules and verification

**Project shape.** `requires-python >= 3.10`. Ruff `line-length = 100`,
`target-version = "py310"`. Tests are stdlib `unittest`, not pytest. Every module
starts with `from __future__ import annotations`.

**Verification commands** (run from the checkout root):

```sh
PYTHONPATH=src .venv/Scripts/python.exe -m unittest discover -s tests
uv run --locked ruff check src tests
uv run --locked pyright src tests
uv run --locked python -c "import pyocd_debug_mcp.server"   # import-time regression guard
```

That last one matters more than usual here: `server.py` executes ~500 lines of
module-level wiring at import, and `serial_resolver.py` reads an environment
variable and parses a JSON file at import (see Trap 6).

**Step 0 ships on its own.** It repairs two pre-existing defects and is verifiable
against the suite as it stands today, with no hook code present. Keeping it as a
separate change means the corrected no-probe message can be reviewed and released
without waiting on the feature.

**Three invariants to hold through every step:**

1. Hook output is *configuration*, never evidence. It cannot restore a gate, plan,
   permission, assignment, or session. `firmstore/store.py:29`
   (`PERSISTED_AUTHORITY_KEYS`) and `ensure_no_persisted_authority` already enforce
   this for anything you persist — route new documents through `FirmStore` so you
   inherit it.
2. With no manifest present, every code path must behave byte-identically to today.
3. **Hooks execute per kind, only when that kind's native result is empty.** See the
   gating rule below — this is the decision the plan leaves open, and it is what keeps
   hooks off the hot path.

### The hook execution gating rule

The plan specifies merge semantics ("hook results supplement native results") and when
to *advise* an agent to write a hook (after native discovery comes back empty), but it
never states when hooks actually *run*. Fill that hole as follows:

```python
# in the unified inventory service
run_probe_hooks = hooks_loaded_for("probe") and not native_probe_rows
run_uart_hooks  = hooks_loaded_for("uart")  and not native_uart_rows
```

Independently per kind, evaluated fresh on each snapshot. Consequences, all intended:

- On a machine where native detection works, **no hook ever executes**, so no path
  gains subprocess latency. This is what makes invariant 2 cheap rather than
  aspirational.
- The mixed case still works: native probe visible + native UART empty runs the UART
  hook only. This is the case "supplement" exists for.
- Merge rules in step 4 are unchanged. They still have to handle a native row and a
  hook row describing the same device, because a device can appear natively on one
  refresh and only via hook on the next.
- A hook cannot mask or outrank a natively visible device, since it never runs while
  one is present.

**Why not "always run and merge":** `_resolve_serial_port_for_session` runs before
*every* UART action. Under always-run, each `read_serial` would spawn hook processes
whose cost its timeout budget does not cover — see step 9. Gating per kind removes the
problem at the source instead of inflating every serial deadline to accommodate it.

If you later decide always-run is required, step 9's budget work must extend to every
tool that touches inventory, not just the probe-inventory set.

---

## 1. What exists today

### 1.1 The eight discovery call sites

The plan lists these; here they are with anchors and what each actually does.

| # | Site | Anchor | What it does now |
| --- | ------ | -------- | ------------------ |
| 1 | `list_connected_probes_cli` | `probe_inventory.py:186` | Runs `configured_probe_cli_commands()` through `run_cmd`, parses the text table, returns `list[ProbeInfo]`. **Returns `[]` for every failure mode** — no exit code, no stderr. |
| 2 | `_validation_inventory` | `server.py:2433` | pyOCD CLI rows + `list_serial_ports()`, plus injection of already-open server-owned connections. Returns `ValidationInventory`. |
| 3 | `_resolve_probe_uid_for_connect` | `server.py:916` | Separate path: `resolve_probe_for_board_cli` (`probe_inventory.py:253`) → board-family scoring. Does **not** go through `_validation_inventory`. |
| 4 | `_assigned_probe_uid_for_connect` | `server.py:957` | `assigned.split(":", 1)[1]` then re-checks against a fresh `_validation_inventory()`. |
| 5 | `_setup_inventory` | `server.py:3390` | Wraps `_validation_inventory()` into `PreflightInventory`, filters probes to the exact `connection_id` (`server.py:3426-3431`). |
| 6 | `_setup_overview` | `server.py:4518-4555` | Wraps `_validation_inventory()` into `connections[]` / `serial_choices[]` rows. |
| 7 | `_get_setup_status` | `server.py:4182` | Another fresh `_validation_inventory()` for UART readiness + attachment-cache resolution. |
| 8 | `_resolve_serial_port_for_session` | `server.py:1488` | pyserial only (`list_serial_ports` + `resolve_serial_port`). Bound as `resolve_port` at `server.py:1977, 2013, 2286`; also called by the finalizer at `server.py:5702`. |

Plus the legacy vendor-helper registry: `serial_resolver.SERIAL_FALLBACKS`
(`serial_resolver.py:194-199`), consumed inside `resolve_serial_port`
(`serial_resolver.py:583-594`).

**Key structural insight for step 4:** sites 2, 4, 5, 6, 7 already funnel through
`_validation_inventory()`. Do **not** build a parallel service. *Promote*
`_validation_inventory` out of `server.py` into a real module and give it a richer
return type. That converts five of the eight sites in one move, and leaves only
sites 3 and 8 (the two genuinely independent lookups) plus site 1 to convert
by hand.

### 1.2 `connection_id` is not one value — four minting sites, three shapes

- `stable_connection_identity()` (`services/connections.py:20`) returns
  `f"probe:{probe_uid.casefold()}"` — **casefolded** — or `f"session:{runtime_token}"`
  when the provider exposes no UID.
- `_setup_overview` (`server.py:4522-4524`) builds `f"probe:{probe.usb_serial}"` —
  **not casefolded** — and falls back to the bare `probe.probe_id` when
  `usb_serial is None`.
- `_validation_inventory` (`server.py:2452`) uses `connection.connection_id`
  (i.e. `session:...`) as the `probe_id` for a UID-less open connection.

Six places then strip the prefix and treat the remainder as a pyOCD `unique_id`:

```text
server.py:968      _assigned_probe_uid_for_connect
server.py:4826     _setup_overview → board_validate.arguments.probe_id
server.py:5261     _setup_continue → _live_test_builtin_setup_target(probe_uid=…)
server.py:5301     _setup_continue → _setup_pack_pipeline(…, probe_uid, …)
tools/setup.py:291 load_setup_tool → validation_probe_id
tools/setup.py:555 board_validate → expected_probe_id
```

And two helpers compare the composite form:
`_same_setup_connection` (`server.py:4417`), `_setup_connection_key` (`server.py:4427`).

This is exactly the assumption step 5 has to delete. The casefold asymmetry is a
separate pre-existing defect — four minting sites, only some normalizing — currently
masked by those helpers casefolding both operands. Step 0b collapses it to one site
before step 5 gives the identifier real meaning.

### 1.3 Subprocess ownership — and the capture defect

`kernel/processes.py` gives you `popen_owned()` (`:460`) and `run_owned()` (`:519`).
Both are solid on process-group ownership: Windows Job Objects with
`CREATE_SUSPENDED` + kill-on-close, POSIX `start_new_session` + `killpg`, and
recoverable markers via `ProcessMarkerStore`.

But `run_owned` at `:548` does:

```python
output, errors = process.communicate(timeout=timeout)
```

`communicate()` buffers **without limit**. A hook that writes 4 GB to stdout takes
the server down before any size check can run. `_run_cmd` (`server.py:848`) then
decodes whatever came back. This is the plan's "checking size only after
`communicate()` is not enough" — confirmed. You cannot reuse `run_owned` for hooks.

**Caller audit — no current exposure.** Every caller was checked:

| Caller | Captures? | Output volume |
| --- | --- | --- |
| `server.py:831` (`_run_cmd`) | yes | pyOCD probe table, vendor CLI listings — small, 30 s ceiling |
| `swd_pyocd.py:150` (`_run_cmd`) | yes | same pyOCD listing path |
| `native_build.py:508` | **no** — `capture_output` omitted, so streams are inherited | unbounded, but never buffered |
| `swd_process.py:232` | uses `popen_owned` directly with its own streaming protocol | n/a |

The one caller with genuinely unbounded output does not capture it. So the missing
ceiling is latent, and hooks would be the first risky caller. Treat this as a
requirement on new code, not a repair — and do not "fix" `run_owned` itself, since
changing its buffering would touch the probe-listing and build paths for no benefit.

### 1.4 The operation timeout budget

`kernel/operations.py:451` `operation_timeout_seconds()` is the single resolver,
wired into `RegistryFastMCP` via `timeout_resolver` (`kernel/registry.py:223, 321`).
At `:533-540`:

```python
if tool_name in _PROBE_INVENTORY_TOOLS:
    inventory_timeout = (
        DEFAULT_OPERATION_TIMEOUT_SECONDS
        + len(configured_probe_cli_commands())
        * (DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS + MAX_OWNED_PROCESS_CLEANUP_SECONDS)
        + CANCELLATION_CLEANUP_GRACE_SECONDS
    )
    resolved_timeout = max(resolved_timeout, inventory_timeout)
```

`_PROBE_INVENTORY_TOOLS` (`operations.py:51`) is
`{setup_overview, connect, connect_override, get_setup_status, connect_under_reset,
board_validate, board_setup, board_fix_setup}`. Every one of these will now
additionally run hooks, so all of them need budget added.

---

## 2. Files to create and touch

**Create:**

```text
src/pyocd_debug_mcp/discovery_hooks.py        # manifest models, snapshot, capped execution, parsing
src/pyocd_debug_mcp/hardware_inventory.py     # the unified inventory service (promoted _validation_inventory)
src/pyocd_debug_mcp/tools/discovery.py        # the two MCP tool handlers
tests/test_discovery_hook_registry.py
tests/test_discovery_hook_process.py
tests/test_discovery_tool_contract.py         # schema drift + tool registration policy
tests/test_discovery_retry_store.py           # retry-ID bounds, expiry, wrong-kind refusal
tests/test_hook_gating_and_budget.py
tests/test_unified_inventory.py
tests/test_inventory_snapshot_concurrency.py  # one operation never mixes rows from two snapshots
tests/test_probe_selection_records.py
tests/test_discovery_hook_workflow.py
tests/test_discovery_hook_safety.py
tests/fake_discovery_hook.py                  # fixture script, mirrors tests/fake_provider_worker.py
```

**Touch:** `firmstore/store.py`, `probe_inventory.py`, `serial_resolver.py`,
`kernel/operations.py`, `setup_flow/preflight.py`, `setup_flow/validate.py`,
`tools/setup.py`, `server.py`, and the four docs.

---

## 3. Implementation sequence

### Step 0 — precursor fixes (land before step 1)

Two defects already present in this code. Neither is caused by hooks; the first blocks
correct placement of hook guidance, the second becomes load-bearing in step 5. Both are
independently verifiable against the current suite, so ship them as their own change.

#### 0a. `setup_overview` misreports zero probes

**The bug.** At `server.py:4589`, inside the
`if board_names is not None and validated_names and not no_board_sentinel:` block:

```python
        if len(validated_names) > len(connection_rows):
            _replace_setup_assignments({}, "setup overview requires assignment clarification")
            return {
                "status": "setup_assignment_clarification_required",
                ...
```

One requested board, zero probes: `1 > 0` is true, so a missing debugger is reported as
a board-naming ambiguity, and no route is built.

**The fix.** Insert an explicit zero test immediately before that comparison:

```python
        if not connection_rows:
            _replace_setup_assignments({}, "setup overview found no debug connection")
            return {
                "status": "setup_no_probe",
                "agent_prompt": (
                    "No debug probe is visible to the server. Tell the user to attach the "
                    "intended board's debugger and retry. Do not ask which board is which; "
                    "this is not a naming ambiguity. Do not expose this payload or internal IDs."
                ),
                "profiles": profile_rows,
                "connections": connection_rows,
                "serial_choices": serial_rows,
                "inventory_error": inventory_error,
                "routes": [],
            }
```

Clearing assignments first matches both sibling early returns (`server.py:4590`,
`server.py:4605`) — do not skip it.

In step 6 this same return grows the locked-environment diagnostic and the
`get_discovery_hook_contract(kind="probe")` call. Landing it now as a plain corrected
message keeps the two changes separately reviewable.

**Then delete the dead branch** at `server.py:4657-4658`:

```python
                if len(connection_rows) == 0:
                    required_user_facts.append("attach and identify one compatible debug probe")
                elif len(connection_rows) > 1:
```

becomes:

```python
                if len(connection_rows) > 1:
```

The `== 0` arm cannot be reached for any named board once 0a returns earlier. Removing
it prevents two competing no-probe messages drifting apart.

**Do not touch the UART equivalent** at `server.py:4666` (`if len(serial_rows) == 0:`).
An empty UART inventory does not short-circuit, so that arm is live and is where step 7
attaches the conditional UART guidance.

#### 0b. `connection_id` is minted four ways

Four construction sites, inconsistent normalization:

| Site | Anchor | Form |
| --- | --- | --- |
| `stable_connection_identity` | `services/connections.py:31` | `f"probe:{probe_uid.casefold()}"` |
| `_setup_overview` row builder | `server.py:4523` | `f"probe:{probe.usb_serial}"` — not casefolded |
| `_selected_setup_connection_matches` | `server.py:4450` | `f"probe:{probe_uid}"` — not casefolded |
| `_validation_inventory` (UID-less) | `server.py:2452` | reuses `connection.connection_id` |

Nothing misbehaves today: `_same_setup_connection` (`server.py:4420`),
`_setup_connection_key` (`server.py:4430`), and `_stable_identity_equal`
(`server.py:3286`) all casefold both operands before comparing. The defect is that the
identifier is not one value.

**The fix** — add to `services/connections.py` beside `stable_connection_identity`:

```python
def probe_connection_id(probe_uid: str) -> str:
    """Return the single canonical setup/connection identity for a probe UID."""

    return f"probe:{probe_uid.strip().casefold()}"
```

Have `stable_connection_identity` call it, and route `server.py:4523` and
`server.py:4450` through it. **Assert no behavior change**: the comparison helpers
already absorb the difference, so this is pure hygiene. Keep those helpers' defensive
casefolding in place — they also handle client-supplied values, not just
server-minted ones.

Step 5 then replaces the string surgery in these helpers with `ProbeSelection`
identity. Doing 0b first means that step starts from one input form rather than three.

### Step 1 — `discovery_hooks.py`

**Layout entry first.** `FirmLayout` (`firmstore/store.py:76`) is a frozen slots
dataclass with an explicit field list and a `for_project` classmethod. Add:

```python
discovery_hooks: Path        # in the field list, after `cache`
...
discovery_hooks=root / "discovery_hooks",   # in for_project
```

and add it to the `ensure_layout()` tuple (`:159-169`).

> **Boundary:** `FirmStore._owned_target` (`:171`) refuses writes outside `.firm`,
> which is fine — but FirmStore must never *write* hook files at all. Hooks are
> agent-authored. FirmStore only *names* the directory and creates it. Do not add a
> `write_hook()` method; that would make the server a hook author.

Also note `.gitignore` already excludes `/.firm/`, so agent-authored hooks are
untracked by default. That is correct — mention it in the operator docs rather than
changing it.

**Models.** Strict, frozen, slots, with an explicit allowed-field set so unknown keys
raise. Mirror the validation idiom in `serial_resolver._load_serial_fallbacks`
(`serial_resolver.py:142`) and `firmstore/cache.AttachmentRecord.from_document`
(`cache.py:124`) — both compute `unknown = sorted(set(raw) - allowed)` and raise.

```python
HOOK_SCHEMA_VERSION = 1
SUPPORTED_RUNNERS = frozenset({"server-python", "executable"})
SUPPORTED_PLATFORMS = frozenset({"windows", "macos", "linux"})
MAX_HOOK_TIMEOUT_SECONDS = 60.0
MAX_HOOK_STDOUT_BYTES = 256 * 1024
MAX_HOOK_STDERR_BYTES = 64 * 1024
MAX_HOOK_ROWS = 64
MAX_FIELD_CHARS = 512

class DiscoveryHookError(RuntimeError): ...

@dataclass(frozen=True, slots=True)
class DiscoveryHookSpec:
    hook_id: str
    kind: Literal["probe", "uart"]
    platforms: frozenset[str]
    runner: Literal["server-python", "executable"]
    entrypoint: Path          # already resolved and containment-checked
    argv: tuple[str, ...]
    timeout_seconds: float
    source: Literal["project", "operator"]
    file_sha256: str

@dataclass(frozen=True, slots=True)
class DiscoveryHookSnapshot:
    manifest_sha256: str
    hooks: tuple[DiscoveryHookSpec, ...]
    loaded_at: str
    def eligible(self, kind: str, platform: str) -> tuple[DiscoveryHookSpec, ...]: ...
```

**Platform naming.** Define one function and use it everywhere — the contract tool,
the manifest filter, and the docs must agree:

```python
def current_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"
```

**Path containment.** Resolve the entrypoint against the returned hook root and
verify, in this order: no NUL bytes; `Path(root, entrypoint).resolve()` is
`.is_relative_to(root.resolve())`; the resolved path `.is_file()`; and — for symlink
escape — that `os.path.realpath` of the file is *also* under the realpath of the
root. `is_relative_to` alone does not catch a symlink pointing outside.

For `runner == "executable"`, require an absolute path and skip root containment
(operator-installed by definition), but still require `is_file()` and NUL-freedom.

**Capped execution.** This is the delicate part. Use `popen_owned`, not `run_owned`,
and replicate `run_owned`'s cleanup discipline exactly — including the
`except BaseException` arm, which exists because cancellation must not outlive the
ownership marker:

```python
def _read_capped(stream, limit: int, sink: list[bytes]) -> None:
    """Read at most `limit` bytes, then keep draining and discarding."""
    remaining = limit
    while chunk := stream.read(65536):
        if remaining > 0:
            sink.append(chunk[:remaining])
            remaining -= len(chunk[:remaining])
    # draining past the cap prevents the child blocking on a full pipe

def _execute(spec, argv, *, marker_store=None) -> HookExecution:
    process, marker = popen_owned(
        argv,
        marker_store=marker_store,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_hook_env(),
        text=False,
    )
    out_sink: list[bytes] = []
    err_sink: list[bytes] = []
    readers = [
        threading.Thread(target=_read_capped, args=(process.stdout, MAX_HOOK_STDOUT_BYTES, out_sink), daemon=True),
        threading.Thread(target=_read_capped, args=(process.stderr, MAX_HOOK_STDERR_BYTES, err_sink), daemon=True),
    ]
    remove_marker = False
    try:
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=spec.timeout_seconds)
            outcome = "exited"
        except subprocess.TimeoutExpired:
            outcome = "timeout"
        except BaseException:
            remove_marker = terminate_process_group(process)
            raise
        remove_marker = terminate_process_group(process)
        for reader in readers:
            reader.join(timeout=MAX_OWNED_PROCESS_CLEANUP_SECONDS)
        if not remove_marker:
            outcome = "cleanup_failed"
        return HookExecution(spec, outcome, process.returncode, b"".join(out_sink), b"".join(err_sink))
    finally:
        # `communicate()` closes these for you; replacing it means closing them here.
        # Hooks run on setup, connect, validate, and status paths, so a leak of two
        # descriptors per execution exhausts the process over a long server run.
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if remove_marker:
            (marker_store or ProcessMarkerStore()).remove(marker)
```

Notes on that sketch:

- **Close both pipes in `finally`.** This is the one thing `communicate()` was doing
  for you that a hand-rolled reader must reproduce. Close *after* joining the readers,
  never before — closing a stream a reader thread is still blocked on raises inside
  that thread. If a reader fails to join within its grace, prefer leaking the
  descriptor over closing underneath it, and record `cleanup_failed`.
- `stdin=subprocess.DEVNULL` and no `shell` — `popen_owned` already raises on
  `shell=True` (`processes.py:467`). DEVNULL also satisfies the plan's "no stdin"
  requirement: a hook that tries to read input gets immediate EOF instead of hanging.
- Keep the four outcomes distinct: `exited` (with returncode), `timeout`,
  `cleanup_failed`, and a separate `parse_failed` produced by the parser. The plan
  requires these not be collapsed.
- Do not add a Windows `CREATE_NO_WINDOW` flag by hand — `popen_owned` sets
  `creationflags` via `process_group_options()` (`processes.py:65`) and blindly
  OR-ing in your own would fight it. If a no-window flag is wanted, add it inside
  `process_group_options`, with a test.
- `_hook_env()` should copy `os.environ` and set `PYTHONIOENCODING=utf-8`, same
  reasoning as `_run_cmd` (`server.py:825-829`) — Windows legacy code pages
  truncating child stdout mid-enumeration is a bug this repo has already been bitten
  by.
- For `runner == "server-python"`, argv is `[sys.executable, str(entrypoint), *argv]`.
  This matches how `configured_probe_cli_commands` already routes pyOCD through
  `sys.executable` (`probe_families.py:176`) so hooks run in the same locked env.

**Parsing.** Decode stdout as UTF-8 with `errors="strict"`. On `UnicodeDecodeError`
report `parse_failed` — do **not** decode with `replace` and then try to parse. Only
*diagnostics* (stderr, and the stdout excerpt shown in the failure payload) use
`errors="replace"`. Validate `schema_version`, cap row count and per-field length,
reject unknown keys.

**Snapshot ordering.** Sort hooks by `(source, kind, hook_id)` so execution order is
deterministic. Reject duplicate `hook_id` within a source, and record provenance so a
project hook and an operator hook with the same ID remain distinguishable.

**Registry loading.** Two sources: the project manifest at
`layout.discovery_hooks / "hooks.json"`, and the operator registry named by
`BYO_MCP_DISCOVERY_HOOK_REGISTRY`. Read the environment variable **inside the load
function**, not at import time (Trap 6). Precedence: merge only when the hardware
identity is identical; a conflict yields two separate selectable rows.

### Step 2 — the two MCP tools

Put handlers in `tools/discovery.py` following the `build_*_handlers(services)`
pattern used by every other tool module (`tools/setup.py:248`), then register them in
`server.py` next to the setup handlers (`server.py:5494-5502`):

```python
for _name, _handler in discovery_tool_handlers.items():
    mcp.add_tool(_handler, name=_name, description=_handler.__doc__, structured_output=False)
    forbid_unknown_tool_arguments(mcp, _name)
```

Do **not** call `tool_registry.configure(...)` for them. `mcp.add_tool` →
`ToolRegistry.register` (`registry.py:283`) defaults to `hidden=False, locked=False`,
which is exactly the "always-visible, non-authorizing" requirement. And do not call
`mcp.configure_layer2` — these are not hardware actions and must not get the
Layer-2 failure wrapper.

`forbid_unknown_tool_arguments` matters: FastMCP silently drops unknown fields by
default (see the comment at `server.py:2359`), and a client passing a bogus
`executable` field must fail closed rather than have it ignored.

**Retry contexts.** Run-scoped, memory-only, bounded. `ServerRun`
(`kernel/run_state.py:16`) is a slots dataclass with `plans/permissions/assignments/
gates` and a `clear_authority()` that wipes them. Retry contexts are **not**
authority, so:

- Do *not* add a field to `ServerRun` (it would be cleared by `clear_authority`, and
  it would sit alongside real authority state — wrong signal).
- Create a small `DiscoveryRetryStore` in `tools/discovery.py` or
  `discovery_hooks.py`, instantiated once in `server.py`, holding an
  `OrderedDict[str, RetryContext]` under an `RLock`, with `MAX_RETRY_CONTEXTS = 32`
  and `RETRY_TTL_SECONDS = 900`. Evict oldest on insert; check TTL on read. Key with
  `secrets.token_urlsafe(16)` (`secrets` is already imported in `server.py:20`).
- `RetryContext` holds: `run_id`, `kind`, `created_at` (monotonic), the exact
  original retry call (tool name + arguments), and the originating `board_id` when
  there is one. Clear on successful replay.

`get_discovery_hook_contract(kind, retry_id=None)` returns:

- `hook_root`: `str(_firm_store.layout.discovery_hooks)` — the server's value, never
  a guess.
- `manifest_schema` / `output_schema`: literal example documents from
  `discovery_hooks.py` constants, so tool output and validator cannot drift.
- `supported_runners`: `sorted(SUPPORTED_RUNNERS)`.
- `operating_system`: `current_platform()`.
- `pyocd_providers` (probe only): `sorted(str(k).casefold() for k in PROBE_CLASSES)`.
  `PROBE_CLASSES` is already imported from `pyocd.probe.aggregator` in
  `probe_inventory.py:9` and used by `probe_family_from_pyocd_probe` (`:57`) — that
  is the registered-provider source of truth the plan asks for. Do **not** derive it
  from `probe_families.json`.
- `example`: a runnable `server-python` manifest + a skeleton hook.
- `refresh_call`: `{"tool": "refresh_discovery_hooks", "arguments": {"retry_id": ...}}`.
- `platform_guidance`: the per-OS advice from the plan's "Cross-platform hook
  guidance" section, selected by `current_platform()`.

`retry_id=None` is permitted but the response must then carry
`"executable": false` and omit `refresh_call`. A wrong-kind or expired ID is refused
**without running anything**.

`refresh_discovery_hooks(retry_id)` takes no path, no argv, no code. It loads the
server-designated manifest, hashes manifest + every hook file, executes each eligible
hook once, and returns per-hook status plus friendly rows and the captured original
retry call. It opens no probe and no port, and consumes no plan or permission.

**Hash-drift enforcement.** Store `file_sha256` per hook in the snapshot at refresh
time. Before *executing* a hook during any later inventory call, re-hash and compare;
on mismatch refuse with a typed `hook/source-changed` and require another refresh.
The re-hash is a small read on a bounded file — acceptable per inventory call.

### Step 3 — typed native probe listing

`list_connected_probes_cli` (`probe_inventory.py:186`) loses everything on failure.
Add a result type and a new function; keep the old signature as a thin wrapper so
existing callers (including `swd_pyocd.py:332`) keep compiling.

```python
@dataclass(frozen=True, slots=True)
class NativeProbeListing:
    probes: tuple[ProbeInfo, ...]
    command: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    stdout_summary: str
    stderr_summary: str

def list_connected_probes_detailed(run_cmd: RunCommand) -> NativeProbeListing: ...

def list_connected_probes_cli(run_cmd: RunCommand) -> list[ProbeInfo]:
    return list(list_connected_probes_detailed(run_cmd).probes)
```

`_run_cmd` (`server.py:821`) already returns `124` for timeout and `127` for
`FileNotFoundError`, so `timed_out` is `exit_code == 124`. Summaries must be
truncated — a pyOCD traceback is not a payload.

### Step 4 — the unified inventory service

Create `hardware_inventory.py`. Move the body of `_validation_inventory`
(`server.py:2433-2481`) into it, parameterized by injected callables so it stays
testable without a live server:

```python
@dataclass(frozen=True, slots=True)
class ProbeRow:
    provider: str
    unique_id: str | None        # exact pyOCD selector; None for a UID-less live session
    row_id: str                  # opaque, stable within this snapshot
    description: str
    stable_identity: str | None
    provenance: tuple[str, ...]  # ("native",), ("hook:local-probe-fallback",), or both
    hook_source_sha256: str | None
    identity_scope: Literal["stable", "session"]

@dataclass(frozen=True, slots=True)
class UartRow:
    port_path: str
    description: str
    usb_serial: str | None
    vid: int | None
    pid: int | None
    provenance: tuple[str, ...]
    identity_scope: Literal["stable", "session"]

@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    snapshot_id: str
    probes: tuple[ProbeRow, ...]
    uarts: tuple[UartRow, ...]
    native_probe_diagnostics: NativeProbeListing
    native_uart_available: bool           # list_serial_ports() is not None
    hook_diagnostics: tuple[HookExecution, ...]

class HardwareInventoryService:
    def snapshot(self) -> InventorySnapshot: ...
    def validation_inventory(self) -> ValidationInventory: ...   # adapter for existing callers
```

**Merge rules, precisely as the code needs them:**

- Probes: dedupe **within the same provider** using the existing
  `_stable_identity_equal` policy (`server.py:3281` — exact casefold match, or
  decimal comparison with leading zeros stripped). Move that function into
  `hardware_inventory.py` and re-export from `server.py` so no behavior shifts. Never
  strip punctuation broadly; never merge across providers, even on identical UID text.
- Stable UARTs: dedupe by `(usb_serial, vid, pid)` — the same key
  `SerialEndpoint.stable_key()` already produces (`cache.py:94`).
- Session-local UARTs: dedupe within one snapshot only, by normalized port path +
  source. `normalize_port_name` (`serial_resolver.py:246`) already strips the
  Windows `\\.\` prefix and lowercases; reuse it, do not reimplement.
- Same stable device from both sources → one row, `provenance = ("native", "hook:…")`.
- Conflicting rows stay separate.
- Hook rows never delete native rows.

**Preserve the active-connection injection** at `server.py:2443-2467` verbatim. Its
comment explains why: pyOCD omits probes this process already has open, and
validation must still be able to select and stamp them. Losing this breaks
revalidation of a live board.

**Apply the §0 gating rule here**, inside `snapshot()`, not at the call sites. Collect
native probe and UART rows first, then run each kind's hooks only if that kind came
back empty. Keeping the decision in one place is what stops a future caller from
accidentally putting hooks on a hot path — and it means `hook_diagnostics` will be
empty on a healthy machine, which callers must tolerate rather than treat as an error.

**Then rewire, in this order:** `_validation_inventory` becomes a one-line delegation
to `service.validation_inventory()`. Sites 4, 5, 6, 7 keep calling
`_validation_inventory` and get hooks for free. Then convert site 8, then site 3.

**Legacy vendor helpers.** `SERIAL_FALLBACKS` and its two parsers
(`parse_nrfjprog_com_output` `:303`, `parse_stm32_programmer_list_output` `:325`) move
behind the unified layer as a third provenance source, `("vendor:nrfjprog",)` etc.
Keep the parsers exactly as they are — they have real-world format knowledge and
existing fixtures. Keep `PYOCD_SERIAL_FALLBACK_REGISTRY` working for one release,
document precedence, deprecate only after migration tests pass.

**Do not touch** `_single_matching_probe_visible_for_board_family`
(`adapters/swd_pyocd.py:318`). It is the safety condition for the J-Link UID-less
retry (`_should_retry_without_uid`, `:337`), and it must keep consulting native
discovery only. If hook rows fed that count, a hook could cause pyOCD to retry
without a UID and open a *different* physical probe. Leave it on
`ConnectHelper.get_all_connected_probes` + `list_connected_probes_cli`, and add a
test asserting a hook row does not change its answer.

### Step 5 — probe selection records

Add to `hardware_inventory.py`:

```python
@dataclass(frozen=True, slots=True)
class ProbeSelection:
    connection_id: str          # opaque token handed to agents
    provider: str
    unique_id: str | None       # exact pyOCD selector
    stable_identity: str | None
    provenance: tuple[str, ...]
    hook_source_sha256: str | None
    identity_scope: Literal["stable", "session"]

class ProbeSelectionStore:       # run-scoped, memory only
    def record(self, selection: ProbeSelection) -> None: ...
    def resolve(self, connection_id: str, snapshot: InventorySnapshot) -> ProbeSelection: ...
```

`resolve` re-derives against a **fresh** snapshot and raises a typed
`SelectionDisappeared` if the stable row is absent or `hook_source_sha256` changed.
Per the plan: clear the assignment and reroute through setup — never silently pick a
different probe.

`_setup_overview` records a selection for each row it emits, then hands out
`connection_id`. Every one of the six `removeprefix("probe:")` sites becomes
`selection_store.resolve(connection_id, snapshot).unique_id`:

- `server.py:968` → in `_assigned_probe_uid_for_connect`; the existing
  "assigned probe is no longer present" `RuntimeError` becomes the typed
  disappeared-selection response.
- `server.py:4826` → `board_validate` argument stays an opaque token; do not put a
  UID in it.
- `server.py:5261`, `server.py:5301` → `_setup_continue`'s live builtin test and pack
  pipeline both need the resolved `unique_id`.
- `tools/setup.py:291, 555` → these compare `probe_id` to the assignment for
  equality only. Simplest correct fix: compare the opaque `connection_id` values
  directly and stop deriving a UID here at all.

Keep `_same_setup_connection` / `_setup_connection_key` but reimplement them over
`ProbeSelection` identity rather than string surgery. Fix the casefold asymmetry from
§1.2 while you are in there — mint `connection_id` in exactly one place.

A `session`-scope selection (UID-less provider, `session:` token) must be usable for
the current run and refused for anything durable, matching how
`ValidationProbe.choice()` already warns the agent (`setup_flow/validate.py:63-70`).

### Step 6 — probe integration

Wire the snapshot into: `_setup_overview`, `_setup_inventory` (preflight),
`board_validate`, `_connect_impl`, `_assigned_probe_uid_for_connect`,
`_get_setup_status`, and disconnect invalidation.

**One snapshot per operation.** Take it once at the top of the operation and thread
it through; never call `service.snapshot()` twice inside one tool call. Run hooks
outside the per-board state lock where possible (`connection_manager.lock_for`,
`services/connections.py:61`), then bind the immutable snapshot inside the locked
region.

**The `setup_overview` insertion point is not where you'd guess.** With
`board_names=["Nucleo"]` and zero visible probes, control reaches `server.py:4589`:

```python
if len(validated_names) > len(connection_rows):
    ...
    return {"status": "setup_assignment_clarification_required", ...}
```

`1 > 0` is true, so it returns *assignment clarification* and never builds a route.
That is the branch the plan means when it says guidance must begin in
`setup_overview`. Split it:

```python
if not connection_rows:
    # typed fallback-available status, not an assignment problem
    return _no_native_probe_overview(profile_rows, serial_rows, inventory_error, hook_diagnostics)
if len(validated_names) > len(connection_rows):
    ...existing clarification...
```

Consequence: the `if len(connection_rows) == 0:` arm at `server.py:4657` (inside the
unknown-board route) is already unreachable for named boards and becomes fully dead.
Remove it rather than leaving two competing no-probe messages.

The zero-UART path is different — `serial_rows` being empty does **not** short-circuit
today, so `server.py:4666` (`if len(serial_rows) == 0:`) is the right place to attach
the *conditional* UART hook-contract call, alongside the existing
"if UART is used, attach and identify the board's UART connection" user fact.

**Preflight.** `PreflightEngine.evaluate` (`setup_flow/preflight.py:254`) is pure and
has no server access — keep it that way. The `setup/no-probe` prompt at `:274-285`
already names `uv run --locked python -m pyocd list --probes`; extend
`PreflightInventory` with a `hook_contract_call: Mapping[str, Any] | None = None`
field (default `None`, so existing construction sites are untouched) and have
`_setup_inventory` populate it. Then `preflight.py` only has to *render* it.
`tests/test_preflight_probe_guidance.py` asserts on that exact prompt string — keep
the existing sentence and append, don't replace.

### Step 7 — UART integration

`_resolve_serial_port_for_session` (`server.py:1488`) is the hot path: it runs
immediately before every UART action via `resolve_port` (`server.py:1977, 2013, 2286`)
and in the finalizer (`server.py:5702`). Rewrite it to:

1. take a fresh snapshot;
2. resolve the board's *selected stable identity* to its current `port_path`;
3. fall back to the existing `resolve_serial_port` scoring only when there is no
   recorded selection.

> **This is the path the §0 gating rule protects.** Because it runs before every serial
> operation, an unconditional hook execution here would put subprocess launches inside
> `read_serial`, `write_serial`, `serial_exchange`, and the `on_exit` finalizer. Under
> the gating rule a UART hook runs only when pyserial reports nothing, so a machine
> with a working native port pays nothing. Step 9 still reserves budget for the case
> where it does run — do not skip that on the grounds that hooks are usually inactive.

Never persist or blindly reuse a `COM3` / `/dev/tty*` string. The plan is explicit
and the code already agrees in spirit — `AttachmentCache` keys on
`(usb_serial, vid, pid)` and only *reports* `port_path` in `CacheResolution`
(`cache.py:179-183`).

**Stable vs session-local:**

- Stable hook endpoints (`serial_number` + `vid` + `pid` all present) → the existing
  `AttachmentCache`. `SerialEndpoint.has_stable_identity` (`cache.py:77`) is already
  the exact predicate; no change needed.
- Session-local endpoints (any stable field missing) → a new run-scoped
  board→UART selection map. Cleared on disconnect, restart, hook refresh, or
  disappearance. Never written to `AttachmentCache` — `_validated_identity`
  (`cache.py:217`) already raises for these, so the boundary is enforced if you
  route through it.
- More than one row could satisfy a selection → fail and route back to friendly
  setup selection. `CacheResolution(False, "multiple_matches")` (`cache.py:338`)
  already models this; reuse the reason code.

Also update `_get_setup_status`'s UART block (`server.py:4176-4230`) to use the
snapshot, and `SerialCandidate.external_adapter` / `provably_mapped`
(`server.py:3440-3449`) so hook-discovered adapters get the same
external-adapter confirmation flow.

### Step 8 — typed failure responses

Add a code family and thread it through both `PreflightBlock`
(`setup_flow/preflight.py:128`) and the `setup_overview` status strings:

| Code | Meaning | Remedy the payload must carry |
| --- | --- | --- |
| `discovery/no-native-probe` | native empty, hooks available | locked-env diagnostic, then `get_discovery_hook_contract(kind="probe")` |
| `discovery/no-native-uart` | native empty **and** UART required | `get_discovery_hook_contract(kind="uart")` |
| `discovery/hook-failed` | nonzero exit / cleanup failure | friendly hook ID, failure class, exact repair+retry call |
| `discovery/hook-timeout` | per-hook deadline | same, plus the configured timeout |
| `discovery/hook-output-invalid` | parse/schema/UTF-8 failure | bounded stdout excerpt, schema pointer |
| `discovery/hook-source-changed` | hash drift since refresh | call `refresh_discovery_hooks` again |
| `discovery/unsupported-provider` | provider not in `PROBE_CLASSES` | discovery worked; installed pyOCD cannot drive it. **A hook cannot fix this.** |
| `discovery/selection-disappeared` | recorded row gone | reroute through `setup_overview`; do not substitute |
| `probe/open-failed` | hook found it, pyOCD could not open | driver, competing process, firmware, physical-target checks. **Never loop back to discovery.** |
| `uart/open-failed` | resolved path would not open | action failure, not a discovery failure |

Two rules the code must make structurally impossible rather than merely documented:

- `probe/open-failed` and `uart/open-failed` responses must not contain a
  `hook_contract_call` field. Assert this in a test.
- Nothing on this table stamps a gate. `_stamp_validation_session`
  (`server.py:3146`) stays reachable only from a real live-identity read.

Ambiguity is *not* a hook case: when native inventory returns multiple rows, keep the
existing friendly-selection flow (`preflight.py:290-305`) untouched.

### Step 9 — timeouts, descriptions, docs

**Timeouts.** The plan requires "operation timeout calculations updated for the
configured number of eligible hooks." Getting the *scope* of that right matters more
than the arithmetic: **every tool that can execute a hook needs the budget, not just
the probe-inventory set.**

In `kernel/operations.py`, add a module-level provider rather than a constant, because
the eligible-hook count is only known after a refresh — and make it per kind, matching
the gating rule in §0:

```python
_eligible_hook_counts: Callable[[], Mapping[str, int]] = lambda: {"probe": 0, "uart": 0}

def set_eligible_hook_count_provider(provider: Callable[[], Mapping[str, int]]) -> None:
    global _eligible_hook_counts
    _eligible_hook_counts = provider

def _hook_budget(*kinds: str) -> float:
    counts = _eligible_hook_counts()
    total = sum(counts.get(kind, 0) for kind in kinds)
    return total * (MAX_HOOK_TIMEOUT_SECONDS + MAX_OWNED_PROCESS_CLEANUP_SECONDS)
```

`server.py` calls the setter once during wiring, pointing at the snapshot store.

**Then apply it to all three groups:**

| Tool group | Hook kinds reachable | Where |
| --- | --- | --- |
| `_PROBE_INVENTORY_TOOLS` (existing set) | `probe`, `uart` | extend the block at `operations.py:533-540` with `_hook_budget("probe", "uart")` |
| **`_UART_ACTION_TOOLS`** — new: `read_serial`, `write_serial`, `serial_exchange` | `uart` | add `_hook_budget("uart")` to each of their existing return paths |
| `refresh_discovery_hooks` | `probe`, `uart` | new `_DISCOVERY_HOOK_TOOLS` frozenset |

`get_discovery_hook_contract` executes nothing and keeps the default budget.

**The UART group is the easy one to miss and the one that breaks the product.**
`_resolve_serial_port_for_session` runs before every UART action (step 7), so those
tools can execute hooks — but they are not in `_PROBE_INVENTORY_TOOLS`, and their
budgets are computed from their own arguments. `read_serial` with `read_seconds=3`
resolves to `max(planned, 3 + ARGUMENT_TIMEOUT_GRACE_SECONDS)` = 8 s, while the plan's
example manifest allows a 10-second hook. Without this, one hook exceeds the whole
operation budget and the read is cancelled before it starts. The early-return paths at
`operations.py:541-549` (`read_serial`) and `:550-560` (`serial_exchange`) each need
the addend — adding it only after those branches misses them entirely.

**The `on_exit` UART finalizer needs it too.** `include_finalizer` (`operations.py:484`)
adds `finalizer_timeout + ARGUMENT_TIMEOUT_GRACE_SECONDS`; `_finalizer_uart_write`
(`server.py:5698`) calls `_resolve_serial_port_for_session`, so add
`_hook_budget("uart")` inside `include_finalizer` when a UART finalizer is present.

Note that with §0's gating rule these addends are **zero on a healthy machine** — the
counts only go positive once a manifest is loaded, and hooks only run when native
discovery is empty. The budget must still be reserved, because whether native
discovery will come back empty is not known when the deadline is computed.

Use a provider callable, not an import of server state: `operations.py` is imported
by `registry.py` which is imported by `server.py`, so any reverse import is a cycle.

**Descriptions.** The live MCP descriptions are the handler docstrings — passed as
`description=_handler.__doc__` at `server.py:5498`. Update the docstrings of
`setup_overview` (`tools/setup.py:303`), `load_setup_tool` (`:251`),
`board_setup_plan` (`:331`), and `board_validate` (`:528`).

**Docs.** `docs/client-contract.md` → `## Setup, research, and validation` (line 98)
and `## Connection routing` (line 67). `docs/architecture.md` → `## Setup and client
relay boundary` (line 193) and `## Runtime contracts` (line 372). Plus `SERVER_GUIDE.md`
and `README.md`.

### Step 10 — verify

Run the four commands from §0. Real hardware is not required for acceptance; if
available, record the optional smoke test separately.

---

## 4. Traps

Traps 2, 3, and 4 are **pre-existing defects**, fixed in step 0. The rest are rules
constraining new code — nothing to repair, but easy to get wrong.

1. **`run_owned` cannot be used for hooks.** `communicate()` at `processes.py:548`
   buffers unbounded. Use `popen_owned` + capped reader threads, and keep draining
   past the cap or the child blocks on a full pipe. Not currently a live bug — see
   §1.3 for the caller audit — so this is a new-code rule, not a fix.
2. **`setup_overview` returns before it can give no-probe guidance.** The
   `len(validated_names) > len(connection_rows)` branch at `server.py:4589` fires
   first. Live user-facing bug. Fixed in step 0a; guidance attaches in step 6.
3. **`server.py:4657` is dead code** once trap 2 is fixed. Deleted in step 0a rather
   than left to drift against the corrected message.
4. **`connection_id` is minted four ways with inconsistent casefolding** —
   `connections.py:31` casefolds; `server.py:4523` and `server.py:4450` do not.
   Latent only: the comparison helpers casefold defensively. Fixed in step 0b, which
   step 5 depends on.
5. **`_validation_inventory` injects live connections for a reason.**
   `server.py:2443-2467`. Preserve it or you break revalidation of an open board.
6. **`serial_resolver.py` reads its registry at import time** (`:194-199`) and raises
   `RuntimeError` from module scope on a bad file. Do not copy that pattern; the plan
   explicitly requires manifests load only via `refresh_discovery_hooks`. Loading at
   import would also make a malformed agent-written manifest unrecoverable without a
   server restart — the exact failure the tool exists to avoid.
7. **Do not feed hook rows to the J-Link UID-less retry.**
   `adapters/swd_pyocd.py:318, 337`. Test that a hook row cannot change its verdict.
8. **`PROBE_CLASSES`, not `probe_families.json`, is the provider source of truth.**
   The JSON is friendly labels + legacy CLI text matching
   (`probe_families.py:139-148`) and must not gate hook support.
9. **Timeout budget must not be an import-time constant.** Hook count is
   run-scoped; use the provider callable.
10. **The UART action tools need hook budget and are not in
    `_PROBE_INVENTORY_TOOLS`.** `read_serial`, `write_serial`, `serial_exchange`, and
    the `on_exit` UART finalizer all reach hooks through
    `_resolve_serial_port_for_session`. Their budgets come from their own arguments —
    `read_seconds=3` yields 8 s against a 10 s hook allowance. Missing this makes UART
    unusable whenever a hook actually runs. See step 9.
11. **Hook execution must be gated per kind, in one place.** The plan never states
    when hooks run; §0 fills the hole. Put the decision inside the inventory
    service's `snapshot()`, never at a call site, or the hot-path protection erodes
    the first time someone adds a caller.
12. **A hand-rolled reader must close the pipes.** `communicate()` did it for you.
    Close after joining readers, in `finally`. Two descriptors per execution on paths
    that run during setup, connect, validate, and status.
13. **`forbid_unknown_tool_arguments` on both new tools.** FastMCP drops unknown
    fields silently otherwise — the same reason it is applied to `connect`
    (`server.py:2361`).
14. **Session-local UART rows must never reach `AttachmentCache`.**
    `_validated_identity` (`cache.py:217`) raises, which is correct — make sure the
    code path treats that as a routing decision, not an error to swallow.
15. **`.firm/` is gitignored**, so agent-authored hooks are untracked by default.
    Document it; do not change it.

---

## 5. Test plan

`unittest` throughout. Model the fake hook on `tests/fake_provider_worker.py` — a
standalone script invoked as a real child process, so the process-ownership,
timeout, and capping paths are genuinely exercised rather than mocked.

`tests/test_setup_overview_no_probe.py` — **step 0, ships first**
: One requested name + zero connections returns the typed no-probe status, not
assignment clarification, and clears provisional assignments like its siblings. Two
requested names + one connection still returns clarification, proving the comparison
was narrowed rather than removed. Zero names + zero connections still returns
`setup_no_board` / `setup_names_required` unchanged. The shared `probe_connection_id`
helper reproduces exactly what all four former sites produced, including mixed case
and decimal leading zeros. `tests/test_server_assignment_connect.py` and
`tests/test_validation_honesty.py` must stay green untouched — they exercise the
comparison helpers whose inputs step 0b changes.

`tests/test_discovery_hook_registry.py`
: Project + operator manifests. Platform filtering. Path containment, symlink escape,
NUL bytes, traversal. Duplicate IDs, unknown fields, invalid runner, invalid platform,
invalid/oversized timeout, non-file entrypoint. Hook-file change after refresh is
detected *before* execution.

`tests/test_discovery_hook_process.py`
: Timeout kills the group. Oversized stdout is truncated and the process still reaped
(assert peak memory does not track output size — write 50 MB from the fake hook).
Malformed UTF-8 and malformed JSON → `parse_failed`, distinct from nonzero exit.
Cleanup-failure path keeps the marker. Cancellation mid-run cleans descendants.
Windows path-with-spaces; macOS/Linux executable-permission cases.
**Descriptors do not leak:** run the same hook a few hundred times and assert the
process's open-handle count is flat (`psutil.Process().num_fds()` on POSIX,
`num_handles()` on Windows — `psutil` is already a dependency). This is the only
cheap way to catch a regression of the `communicate()` replacement.
**A hook that reads stdin gets EOF, not a hang** — fake hook calls
`sys.stdin.read()`, must still exit within its timeout.
**Order is deterministic** across repeated snapshots with several hooks configured.

`tests/test_discovery_tool_contract.py`
: **Schema drift:** feed the exact `manifest_schema` example returned by
`get_discovery_hook_contract` through the real manifest validator from step 1 and
assert it accepts it. Do the same for `output_schema` against the real hook-output
parser. The server hands the agent an example and then judges what the agent writes
back with a separate code path — if the two disagree, the agent gets a rejection loop
it cannot debug, and nothing else in this suite would catch it.
: **Tool registration policy:** `get_discovery_hook_contract` and
`refresh_discovery_hooks` are both listed by `list_tools()` with no board connected
(not hidden), callable with no prior unlock (not locked), absent from
`mcp._layer2_tools` (not wrapped as a hardware action, so a hook failure is not
reported through the Layer-2 failure envelope), and reject an unrecognized keyword
argument (`forbid_unknown_tool_arguments` is actually wired, not just intended). Any
one of these being wrong makes the fallback unreachable from a client, not merely
buggy.

`tests/test_discovery_retry_store.py`
: A `retry_id` from `get_discovery_hook_contract(kind="probe")` is refused by
`refresh_discovery_hooks` if presented for a UART contract instead — and refused
**without executing any hook**, verified by asserting zero subprocess launches on that
call. An expired context (mock the clock past `RETRY_TTL_SECONDS`) is refused the same
way. Inserting past `MAX_RETRY_CONTEXTS` evicts the oldest, not a random one. A
successful `refresh_discovery_hooks` clears its own context; replaying the same
`retry_id` afterward is refused. `retry_id=null` is accepted for contract inspection
and the response omits `refresh_call`.

`tests/test_hook_gating_and_budget.py` — **guards the two hot-path defects**
: **Gating:** with a probe hook and a UART hook both configured, a snapshot taken while
native detection returns rows for both kinds executes *neither* hook (assert zero
subprocess launches, not merely empty results). Native probes present + native UART
empty runs the UART hook only. Both native lists empty runs both. Assert per kind
independently, since a single combined flag would pass the first case and fail the
mixed one.
: **Budget:** `operation_timeout_seconds("read_serial", {"read_seconds": 3})` exceeds
the configured per-hook allowance once a UART hook is loaded, and equals today's value
when none is. Same for `write_serial`, `serial_exchange`, and a `read_serial` carrying
an `on_exit` UART finalizer. Also assert `get_discovery_hook_contract` keeps the
default budget, and that the provider defaults to zero counts before any refresh so
the resolver is safe to call during startup.

`tests/test_unified_inventory.py`
: Native-only / hook-only / merged / deduplicated / conflicting / disappeared /
changed rows. Decimal J-Link UID equality without over-normalizing other providers.
Two providers with identical UID text stay distinct. Active opened probes remain
visible to validation. Registered plug-in provider (fake entry injected into
`PROBE_CLASSES`) is accepted; unregistered provider is diagnostic-only.

`tests/test_inventory_snapshot_concurrency.py`
: Two boards run `setup_overview` concurrently against different fake hook fixtures
that return different rows on each call (increment a counter per invocation). Assert
every row inside one returned snapshot carries the same `snapshot_id` — one operation
never sees probes from one scan paired with UART rows from a later or earlier one.
Also drive the two operations through a shared `HardwareInventoryService` instance
with real threads (not just sequential calls) to catch a snapshot built from
interleaved partial state, not only a snapshot swapped whole between two calls.

`tests/test_probe_selection_records.py`
: `connection_id` → `(provider, unique_id)` resolution. Hash change or missing row
clears the assignment instead of selecting another probe. Session-scope selections
are run-only. The J-Link retry verdict is unaffected by hook rows.

`tests/test_discovery_hook_workflow.py`
: The end-to-end simulations the plan enumerates — native pyOCD empty + probe hook
returns a valid UID; native pyserial empty + UART hook returns an endpoint; pyOCD
open succeeds with the hook UID; pyOCD open *fails* after successful discovery
(assert terminal remedy, assert no gate stamp, assert no `hook_contract_call`); UART
open success and failure; hook timeout / malformed output / disappearance / identity
change; reconnect and port-path change. Plus: `setup_overview` with zero native
probes returns hook guidance *before* building a route; zero UART rows yields the
conditional guidance; the full contract → write hook → refresh → rerun overview →
select → setup → validate → disconnect → reconnect loop; existing-profile validation
against a hook-discovered assigned probe; multiple boards keep one-to-one
assignments; UART-required vs UART-disabled route differently; stable UARTs survive a
port-path change; hook UART paths are used by read, write, exchange, and finalizer.

`tests/test_discovery_hook_safety.py`
: Hook output cannot inject a target, pack, connection policy, board ID, flash range,
permission, or hardware action. **No hook configuration → byte-identical behavior and
schemas** (the most important regression test in the set). Existing
`PYOCD_SERIAL_FALLBACK_REGISTRY` fixtures retain behavior. Native inventory remains
preferred. Plans, permissions, validation stamps, assignment invalidation, and
memory/flash containment behave identically for native- and hook-discovered hardware.

CI should run the process/fixture suites on a Windows/macOS/Linux matrix; the rest is
platform-independent.

---

## 6. Acceptance mapping

| Plan acceptance criterion | Where it is satisfied |
| --- | --- |
| A missing probe is reported as a missing probe, not a naming ambiguity | Step 0a |
| The probe `connection_id` has one construction site | Step 0b, then step 5 |
| Hooks never run on a machine where native discovery works | §0 gating rule, enforced in step 4's `snapshot()` |
| UART operations are not cancelled by their own deadline when a hook runs | Step 9 `_UART_ACTION_TOOLS` + `include_finalizer` |
| Post-failure response tells the agent exactly how to get the contract, install/refresh, and retry | Step 8 codes + step 6 `setup_overview` split |
| Any installed pyOCD provider openable by the returned UID completes the same paths | Steps 4-6, `PROBE_CLASSES` as truth |
| Hook UART completes the same guarded serial paths, re-resolved before use | Step 7 rewrite of `_resolve_serial_port_for_session` |
| Discovery failure / backend-open failure / ambiguity are distinct and actionable | Step 8 table; ambiguity stays on the existing flow |
| Hook code and output never grant hardware authority | Step 1 boundary + `ensure_no_persisted_authority` + safety tests |
| Windows/macOS/Linux covered by automated tests and agent guidance | Step 1 `current_platform`, contract `platform_guidance`, CI matrix |
| Full existing suite stays green | Step 9 wrapper-preserving signatures; §0 commands |
| The contract example the server hands out is one it will accept back | `test_discovery_tool_contract.py` schema-drift case |
| The two new tools are actually reachable by a client (visible, unlocked, unwrapped, argument-strict) | `test_discovery_tool_contract.py` registration-policy case |
| A wrong-kind or expired retry ticket is refused without touching a hook | `test_discovery_retry_store.py` |
| Concurrent setup for two boards never mixes probe/UART rows from different scans | `test_inventory_snapshot_concurrency.py` |
