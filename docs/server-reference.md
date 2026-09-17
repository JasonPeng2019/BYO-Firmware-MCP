# BYO Firmware MCP Server — Comprehensive Reference

This document describes the implementation currently in this repository. It is
an architectural and operating reference, not a replacement for the live MCP
`tools/list` schemas. The live schemas and `guardrails/plan_defs.py` remain the
authoritative definitions of tool parameters, budgets, and permission modes.

## 1. What this server is

BYO Firmware MCP is a local, headless MCP server for working with embedded
boards through pyOCD and pyserial. It runs over **stdio only**: it does not host
an HTTP API, open a listening socket, or treat an MCP client as a safety
authority. Its job is to combine board setup, hardware discovery, debug,
bounded serial I/O, firmware deployment, recovery, and evidence collection
with a server-enforced safety model.

The server is intentionally board-neutral. A distribution contains no trusted
board profiles, firmware, CMSIS packs, reviewed datasheets, device geometry, or
pre-approved addresses. Those facts are established for the active firmware
project and stored under that project's `.firm/` directory.

The central principle is:

```text
Client request → server checks visibility → plan → permission → live session
               → map freshness → typed containment → backend operation
```

A tool appearing in an MCP client does not mean the call is authorized. A
client may also call a hidden name from stale schema data; the physical handler
still applies its lock and refuses it.

## 2. Major components

```text
MCP client (stdio)
        │
        ▼
server.py / RegistryFastMCP
        ├─ kernel: dynamic tool registry, dispatch, cancellation, timeouts,
        │          per-board locks, finalizers, owned-process cleanup
        ├─ guardrails: plans, budgets, permission store, session gate
        ├─ setup_flow: inventory, pack/evidence onboarding, setup, validation
        ├─ safety: maps, source replay, freshness, region containment
        ├─ services + adapters: pyOCD debug/SWD, symbols, serial I/O
        ├─ FirmStore: atomic project-local profiles, evidence, maps, reports
        └─ monitor: passive observations, ledger, reports, local filler delivery
```

`server.py` is the composition root. It wires the modules together, but the
rules belong to their owning packages rather than being scattered through MCP
handlers.

### Runtime ownership

* `ServerRun` is one process-local live run. It owns run identity and transient
  authority; restart creates a new run.
* `ConnectionManager` is the sole owner of live target handles. It enforces one
  logical board per live connection and one live connection per logical board.
* `ToolRegistry` controls discovery visibility and independent per-board locks.
* `PlanEngine`, `PermissionStore`, and `GateManager` keep plans, approvals, and
  validation authority in memory only.
* `FirmStore` owns durable project evidence and atomic writes. It never restores
  a connection, a plan, a permission, or an open gate.

## 3. Starting, handshake, and workspace boundary

Install and start with the locked environment:

```text
uv sync --locked
uv run --locked pyocd-debug-mcp
```

When the client starts the server outside the firmware project, it can set
`BYO_MCP_ARTIFACT_ROOT` to the project root. This selects where project-local
state can be found; it must not be pointed at the server checkout.

The first MCP call should be `initialization_handshake(workspace_path?)`.
It returns operating guidance, the currently advertised tool set, and the
current Server Run identity. An optional absolute existing `workspace_path` is
used only to label monitor records. It is not a safety-evidence write location
and cannot relocate `.firm` state. Invalid paths are ignored and the monitor
records the run as unbound.

The user should only be asked, conversationally, for familiar names of boards
they want to use now, or the literal single answer `no board`. The client then
calls `setup_overview`. The user should never be asked for a board ID,
connection token, continuation token, permission enum, pyOCD target, or JSON.

## 4. Durable data and what is deliberately not durable

The project data owner is `FirmStore`. Its normal layout is:

```text
.firm/
  boards/       schema-v2 portable logical board profiles
  packs/        verified immutable pack bytes and support index
  evidence/     content-addressed captured datasheet bytes
  setup/        immutable setup attempts and append-only logs
  safety/       one schema-v2 or schema-v3 memory_map.yaml per board
  validation/   immutable validation and recovery attempt records
  cache/        revocable host attachment hints
  discovery_hooks/  local agent-authored fallback discovery (gitignored)
```

Writes are atomic and authority-bearing fields are checked. Board profiles keep
the exact user-supplied MCU ordering code and Unicode display name. Profiles
refer to a server-derived canonical support binding, not an arbitrary client
path or a target string claimed by an agent. Pack bytes and datasheets are
re-hashed and replayed on later use.

The following are never persisted as usable authority: active connection and
assignment, current plan and remaining budget, permission, unlocked action,
validation stamp, or open session gate. Reports are evidence, never a shortcut
past a fresh validation after reconnect or restart.

## 5. Identity, boards, probes, and serial devices

A board profile is a logical role, not a permanent physical-device ownership
claim. Its `board_id` is stable and the YAML filename stem must equal it;
`display_name` is the user-facing familiar name and may change. A compatible
replacement may use an existing profile only after current validation.

The server inventories native pyOCD probes, active connections, UARTs, optional
vendor fallbacks, explicitly registered remote pyOCD-server endpoints, and
eligible discovery-hook rows. It returns friendly choices to the client while
keeping the machine identifiers opaque.

Stable probe identity is provider-qualified. When a provider gives no durable
UID, an immutable runtime token identifies that live connection only; it is
explicitly session-local and cannot be used as a durable cache key. The same
distinction applies to serial devices: stable USB serial/VID/PID data can be
used to find a moved COM/tty port later, while a raw port path is runtime-only.

`AttachmentCache` stores revocable host-local hints between a stable probe and a
stable UART. It is not portable board authority, is ignored for incomplete or
ambiguous identities, and never grants a target, map, plan, permission, or
gate.

### Remote probe endpoints

`register_remote_probe` and `unregister_remote_probe` manage explicit
`remote:<host>:<port>` pyOCD-server endpoints. Registration normalizes and
persists the endpoint, performs a bounded TCP reachability check, and can retain
an endpoint even if it is unavailable at that moment. It does not itself open a
debug session. The registry is durable inventory configuration, not an approval
or a claim that the remote board is safe.

### Discovery hooks

Native discovery is preferred. If it finds no probe or UART and the prescribed
locked-environment check also finds none—even though the user can see hardware
in a vendor tool—the client may call `get_discovery_hook_contract`.

That inspection tool returns the server-designated hook directory, manifest and
output schemas, limits, supported runners/platforms, and, when retrying an
actual discovery failure, an opaque short-lived retry ticket. The agent writes
the hook and manifest under `.firm/discovery_hooks`; the server never accepts a
caller-supplied script path, argv, or code through MCP. `refresh_discovery_hooks`
loads the designated manifest, hashes its files, runs eligible hooks once, and
returns friendly inventory rows plus the exact retry call.

Hooks run only for a kind whose native discovery is empty. Their output can name
hardware only. It cannot select a target, create a profile, open a connection,
restore validation, change a plan, grant a permission, or make an unsupported
provider usable.

## 6. Board onboarding and validation

`setup_overview` is the routing front door after the handshake. It takes the
user's familiar board names, inventories current hardware, resolves matching
profiles, and returns server-generated next calls and friendly choices.

* A complete matching profile routes to `board_validate`.
* An unknown board routes through `board_setup-plan` and `board_setup`.
* An incomplete/failed profile routes to the repair allowance and
  `board_fix_setup`.
* A stale or invalid safety map routes to `board_safety_refresh`.

Before using a setup/validation path, the client calls
`load_setup_tool(board_id, tool_name)`. This records a per-run disclosure of the
specific workflow and returns its exact guidance. It is not itself authority.

### Setup transaction

`board_setup-plan` is a permission-locked plan whose action parameters bind
one mode (`setup` or `repair`), selected connection, display name, exact MCU
part number, UART requirement/baud/identity, and local authoritative datasheet
path. The user supplies normal board facts; the server selects or validates the
technical facts.

The setup workflow does the following in phases:

1. Re-inventory probes, UARTs, profiles, build artifacts, targets, and known
   support; ask for a friendly physical choice only when necessary.
2. Replay any verified project support for the exact part. Otherwise issue a
   focused research continuation for a single official pack candidate.
3. Stage candidate pack bytes, hash them, validate archive/PDSC structure,
   derive/select the exact PDSC leaf and target, and make a non-destructive live
   attach before promoting support.
4. Capture and hash the authoritative local datasheet. If map facts cannot be
   derived from server-owned evidence, request narrowly scoped official-document
   research, not user-supplied addresses.
5. Build a safety map, commit profile/support/evidence only after deterministic
   checks succeed, and write immutable setup evidence.

`continue_setup` accepts only the server-issued continuation and exactly the
requested research/choice response. Failed candidates are recorded; a retry
cannot silently re-submit the same stale candidate. `board_fix_setup` resumes
the first unfinished phase but redoes current hardware preflight rather than
trusting old inventory.

### Validation

`board_validate` is the only ordinary path that establishes live identity
authority and opens a board's in-memory gate. It loads the profile and map,
re-inventories hardware, resolves the current selected probe, replays the exact
support binding, connects, and performs only safe configured identity reads.
It associates the current map digest with that exact live connection and stamps
the run-scoped session.

Validation may prove a specific exact identity or an explicitly compatible one.
If support lacks safe identity evidence, diagnostic connectivity can be
reported but the gate cannot open. Validation does not install packs, flash,
capture UART behavior, or claim firmware correctness.

Identity proof is cleared on restart, disconnect, connection/probe change,
identity repair, mismatch, and destructive recovery. It is not invalidated just
by reset, flash, UART work, ordinary artifact collection, or map refresh.

## 7. Safety maps and containment

Each board has exactly one active `memory_map.yaml`. The server supports a
reviewed schema-v2 map and a generic schema-v3 map. A map is not merely a
diagram: it is the source of typed address authority.

Reviewed maps retain separate regions for application and bootloader partitions
only where explicit reviewed partition policy authorizes them. The physical
capacity of a flash device is never reinterpreted as authority to program all
of it. Generic maps retain physical RAM/ROM/flash, flash algorithm/erase facts,
and optional SVD peripheral blocks without joining gaps. Generic application
allocation is explicit and durable before programming; a partial flash failure
therefore remains within a recorded owner.

The map classifies physical flash/RAM/ROM, CPU/system controls, peripherals,
security/provisioning/OTP/option-byte/lifecycle regions, plus any reviewed
firmware partitions. Prohibited subranges override broader writable categories.
Unknown or unmapped space is denied. Read-only behavior is also typed: reads
are limited to backend-accessed bytes and write-only peripherals cannot be
read as a convenience.

`board_safety_refresh(board_id)` rederives a complete candidate from the
profile and replayed server-owned evidence. It accepts neither caller-supplied
ranges nor build outputs. It can replace the map association for an existing
live identity proof when the identity anchor is unchanged, but cannot create
identity proof. A part/target/identity-anchor change closes proof and requires
`board_validate`.

Every guarded action checks current fingerprint freshness and then applies its
own containment rule immediately before target I/O:

* memory writes must fit permitted mapped RAM/type-specific regions;
* peripheral writes must fit a documented peripheral register and avoid all
  prohibited subranges;
* application/bootloader flashing validates artifact load segments, entry and
  vector behavior, target, partition, and affected erase sectors;
* breakpoints use executable segments from the current ELF, not a blanket
  assumption that all flash is executable.

## 8. Plans, permissions, locks, and action budgets

All guarded actions use their matching `*-plan` tool. A plan is not an action.
First call it with its complete all-`NULL` envelope. The response renders the
purpose, field semantics, preconditions, safety mode, budget, permission mode,
and exact example. Then submit one complete plan JSON envelope. It must contain
the exact action-parameter object and no flattened fields, prose wrappers,
missing members, or extra members.

On acceptance the plan engine snapshots the board, exact action parameters, and
required budget/permission. It unlocks the action for that board and provides a
compact exact preferred direct call. Dynamic MCP clients receive a
`tools/list_changed` notification. Static clients may use only the exact
server-returned one-child `action_batch` fallback; it re-enters the same
dispatch path and does not bypass any check.

Plan definitions currently cover:

| Plan | Unlocked action | Core purpose |
| --- | --- | --- |
| `board_setup-plan` | `board_setup` / repair route | create or repair profile and evidence |
| `connect_override-plan` | `connect_override` | exceptional reviewed identifiers |
| `connect_under_reset-plan` | `connect_under_reset` | attach while reset is asserted |
| `flash_application-plan` | `flash_application` | application-partition artifact |
| `flash_bootloader-plan` | `flash_bootloader` | bootloader artifact; permission locked |
| `read_memory_address-plan` | `read_memory_address` | bounded raw mapped read |
| `read_serial-plan` | `read_serial` | bounded UART capture |
| `register_write-plan` | `register_write` | masked peripheral write |
| `reset_and_halt-plan` | `reset_and_halt` | reset then halt |
| `serial_exchange-plan` | `serial_exchange` | one bounded UART request/response |
| `set_breakpoint-plan` | `set_breakpoint` | bounded breakpoint placement |
| `set_execution_state-plan` | `set_execution_state` | set target run/halt state |
| `target_unlock-plan` | `target_unlock` | destructive recovery |
| `write_cpu_register-plan` | `write_cpu_register` | core-register mutation |
| `write_memory-plan` | `write_memory` | typed contained memory write |
| `write_serial-plan` | `write_serial` | bounded UART output |

Some plans require an explicit user permission. The client gathers it through
ordinary conversation, but only passes it in that plan's exact structured
permission field. Conversation, a prior run, visible tool, report, or batch
child never counts as authorization.

At invocation, a guarded action is checked again for its registry lock, plan
scope and exact parameter binding, remaining budget, required permission, live
session/gate, safety-map freshness, and typed containment. The plan is therefore
not a pre-authorization that a batch or stale caller can repurpose.

## 9. Tool reference

The exact currently visible list changes by phase; this groups the server's MCP
surface by responsibility. Consult `tools/list` for the live schema.

### Always-visible workflow, inventory, and status tools

| Tool | What it does |
| --- | --- |
| `initialization_handshake` | returns run-specific operating guidance and optionally labels monitor records with a workspace |
| `setup_overview` | routes familiar board names to validation, setup, repair, or safety refresh |
| `load_setup_tool` | exposes detailed guidance for one setup/validation workflow in this run |
| `continue_setup` | supplies an exact server-requested setup continuation |
| `get_setup_status` | reports readiness and non-authoritative build guidance |
| `board_safety_refresh` | rederives/rechecks the durable map from replayed evidence |
| `board_validate` | non-destructively establishes live identity and opens one gate |
| `get_discovery_hook_contract` | returns the hook contract without executing it |
| `refresh_discovery_hooks` | loads server-designated hooks and refreshes fallback inventory |
| `register_remote_probe` / `unregister_remote_probe` | maintain explicit pyOCD-server endpoint inventory |
| `collect_build_artifacts` | stages explicit ELF/HEX/BIN/map outputs into a canonical hashed bundle; does not build, search, or authorize |
| `action_batch` | executes a non-recursive list for one board, stopping at first failure; each child is individually guarded |
| `server_health_check` | returns monitor/store/delivery health without changing board authority |
| `report_agent_issue` | submits a structured agent-observed issue to the passive monitor |
| `submit_routine_checkin` | records and submits a routine work/check-in summary when due or requested |

### Connection and inspection

`connect(board_id)` is profile-only: it resolves the named project profile and
profile-matched probe, with no target, UID, external config, or environment
override accepted. `disconnect(board_id)` releases that board's live resources
and clears its run-scoped authority. `get_board_info`, `get_state`,
`read_cpu_register`, `read_execution_state`, `find_symbol`, and
`read_memory_symbol` provide non-mutating inspection through a connected board
and appropriate symbol/map checks.

`get_board_info` reports resolved profile and connection information, while
`get_state` reports current target/session state; neither creates validation
authority. Symbol tools accept an explicit ELF when needed. A successful
application flash may create only a temporary convenience symbol binding for
the current uninterrupted Server Run.

The hidden `connect_override` action is only for an accepted
`connect_override-plan`; its manual values are run-scoped and never rewrite a
profile. `connect_under_reset` requires its plan and a probe with reset-line
support. A failure to support reset is reported; it does not silently fall back
to ordinary attach.

### Execution and debug

Visible ordinary operations include `halt`, `resume`, `step`, `reset_and_run`,
`remove_breakpoint`, and bounded `wait`. They operate on the named live board
and preserve their documented MCU state; cleanup does not reset a target merely
because a normal operation completed.

`reset_and_halt`, `set_execution_state`, and `set_breakpoint` are plan-locked.
Breakpoint setup resolves a symbol/address against the selected current ELF and
requires the target address to be within an executable mapped segment.

`read_execution_state` is read-only target-state inspection. It is deliberately
separate from `set_execution_state` and `write_cpu_register`, so read access
cannot be turned into an execution or register mutation without a plan.

### Memory and register access

Symbol-oriented reads are preferred when debug metadata identifies a value.
`read_memory_address` is hidden until its raw-read plan specifies exact address,
width (8/16/32), and optional bounded length. The server checks the exact bytes
the backend will read. Mapped RAM, flash, ROM, CPU/system, and peripheral space
may be readable according to map policy, including deliberately prohibited
security spans when authoritatively mapped; unknown space and write-only
register reads are refused.

`write_memory` is plan-locked and type-contained. `write_cpu_register` accepts
only registers supported by the target architecture. `register_write` performs
a masked read-modify-write only for a documented writable peripheral register;
write-only registers require a full 32-bit mask because a prior value cannot be
safely read. All prohibited security/provisioning/lifecycle/option-byte regions
remain unavailable.

### Serial tools

`read_serial`, `write_serial`, and `serial_exchange` are plan-locked. The
server resolves a selected stable UART identity to its current port just before
use. Caller port paths are runtime-only and never become durable attachment
identity. Captures and exchanges are bounded in time and size. Read results use
reversible JSON-escaped `captured_text` rather than assuming terminal encoding.

Plan schemas can specify a reset-on-open behavior and a tightly structured
finalizer. The only supported finalizers are `uart_write` and `reset_and_run`
on eligible serial operations. They are best-effort and execute before the
mandatory cleanup path; arbitrary code/finalizers are not accepted.

### Firmware deployment and recovery

`flash_application` and `flash_bootloader` are separate hidden actions. Their
plans bind an explicit local ELF or HEX artifact by digest. Artifact drift is
rejected before permission, budget, containment, or pyOCD work. Flash parsing
verifies format and concrete load segments; HEX requires the corresponding ELF
where the operation needs it. No caller-supplied base address can expand the
allowed partition.

Application flash requires a validated fresh write gate and containment in the
reviewed/generic application allocation. Bootloader flash has a separate
partition and stronger permission rule. Neither can write ROM bootloader,
unknown, or prohibited space. Flash becomes non-interruptible once its
transaction starts; cancellation waits for bounded safe completion and cleanup.

`target_unlock` is a separate destructive recovery flow. It requires a fresh
one-time approval and only discloses supported mechanisms (`backend_mass_erase`
or `manual_only`) after checking connected backend capability and exact map
facts. Recovery leaves the normal validation gate closed; the board must be
validated again before guarded work.

## 10. pyOCD, drivers, and process isolation

The adapter layer isolates pyOCD/SWD behavior behind backend-neutral services.
It uses finite timeouts, validates subprocess arguments, and owns process
groups/identity markers so cleanup terminates only processes it owns. Startup
hygiene is bounded and only cleans up when a live process identity matches.

The server expects the host's debug drivers and pyOCD providers to enumerate
hardware. When `pyocd list --probes` sees no device, that is normally a host
driver/permission/cable problem—not something a discovery hook can repair.

Multiple J-Link sessions receive special handling because pylink DLL instances
cannot safely be shared in every concurrent arrangement. The first live J-Link
may use the normal DLL; additional simultaneous sessions use isolated temporary
DLL instances. If a close cannot be verified, the reservation is retained and
later sessions remain isolated rather than risking reuse. This allocation is
based on live provider/session behavior, not board names or host-specific paths.

Calls for one logical board serialize. Different boards can execute in parallel.
MCP cancellation is connected to cooperative cancellation where safe. The common
cleanup owner releases UARTs, debug handles, reset state, locks, and owned
processes exactly once, including after stdio EOF or normal shutdown.

## 11. Native build and artifact collection

The server does not choose an IDE, SDK, compiler, target, output convention, or
vendor-specific build command. `get_setup_status` supplies provider-neutral
guidance for `pyocd_debug_mcp.native_build`: the client inspects the project,
resolves executable/argv/cwd/environment/outputs, and passes that exact argv
after `--`. The helper executes without a shell, with bounded timeout. `--offline`
is a best-effort environment guard for common dependency tools, not an OS-level
network sandbox.

The `pyocd-collect-artifacts` helper and `collect_build_artifacts` MCP tool
accept explicit outputs only. They do not scan the disk, build code, download
dependencies, or access hardware. They create a canonical `firmware.*` bundle
and SHA-256 manifest outside `.firm`, recording provenance but no gate, plan,
permission, or allowed-range authority. A later flash plan and runtime artifact
inspection decide whether a chosen artifact is safe to program.

## 12. Monitoring (“Sentry”) and evidence delivery

The monitor is passive: it observes managed dispatch and must never delay,
reorder, authorize, or change a tool result. It records bounded in-memory trails
for problem reports, cumulative counters, issue classification/deduplication,
periodic usage snapshots, routine check-in prompts, and an append-only per-run
hash-linked ledger. It also verifies prior ledger segments and reports its
health.

`server_health_check` is read-only monitor diagnostics. `report_agent_issue`
submits the monitor's explicit structured issue form, and
`submit_routine_checkin` supplies its routine activity form. None opens a
hardware gate, consumes a hardware plan, or changes a hardware tool result.

The ledger detects ordinary corruption, partial writes, and localized edits. It
is not tamper-proof against the local machine owner, who can rewrite records and
recompute a public hash chain. A genuine off-box witness is required for that
stronger property.

### Current delivery status: local filler, not OAuth

There is **no true OAuth/OpenID remote pipeline in this repository today**.
The default configured monitor transport is `SimulatedRemoteTransport`:

* ledger files are copied to a local `simulated_remote/<workspace>/ledger/`;
* report JSON and Sentry-shaped envelopes are written locally under
  `simulated_remote/<workspace>/reports/`;
* the Sentry SDK client is created with `dsn=None` and a custom local envelope
  writer;
* successful filler calls return `FILLER_SIMULATED`, not `SENT`;
* `FILLER_SIMULATED` is expressly not durable off-box delivery.

Set `BYO_MCP_MONITOR_ROOT` to an absolute directory when one launch needs an
isolated monitor store. A nonblank value is exclusive and takes precedence over
both per-user app data and `BYO_MCP_ARTIFACT_ROOT`: `server_data/` and
`simulated_remote/` are created beneath that directory only. If it cannot be
resolved or written, the monitor buffers in memory and server startup continues;
it does not fall back outside the selected root. When the variable is unset or
blank, the baseline order remains the per-user application-data directory named
`BYO`, then `.byo-monitor` under the configured artifact root, then buffering.

The transport seam is deliberately shaped so a future real authenticated
transport can replace the filler, but OAuth token acquisition/refresh,
OpenID identity, authenticated HTTP delivery, real DSN configuration, and
off-box ACK/replay are not implemented. Until that cutover, monitor delivery
must not be described as cloud archival or an external integrity witness.

## 13. Safety lifecycle at a glance

```text
handshake → setup_overview
  ├─ known complete profile → load board_validate → validate → gate opens
  ├─ unknown profile → load setup plan → plan → setup/continuations
  │                    → map commit → validate → gate opens
  └─ incomplete/stale state → returned repair/refresh route

guarded action → exact plan + (approval when required)
               → live connection + validation stamp + fresh map
               → action-specific containment → backend

disconnect/restart/probe change/recovery → clears live proof → validate again
```

No standalone “open gate” tool exists. A gate is opened only by successful live
validation for its assigned connection and current map. A plan, permission,
profile, report, or cache entry cannot reopen it.

## 14. Important operational limits

* This is a local stdio server, not a multi-user network control plane.
* A safe map is only as complete as the reviewed/replayed project evidence;
  unknown areas fail closed.
* Successful flashing is deployment evidence, not proof that firmware behaves
  correctly.
* Validation proves current connection/profile compatibility and configured
  identity evidence, not immutable lifetime provenance of a physical board.
* Host attachment cache is convenience metadata, not a security boundary.
* Discovery hooks are local fallback configuration, not drivers and not
  authority.
* The monitor currently writes locally; it is not an OAuth-backed Sentry/cloud
  service and supplies no off-box anti-tamper guarantee.

## 15. Related repository references

* `SERVER_GUIDE.md` — concise operator workflow.
* `docs/architecture.md` — implementation architecture and state ownership.
* `docs/client-contract.md` — client-facing response/relay contract.
* `docs/plan-tool-contract.md` — generated current plan/action fields.
* `mcp-issue-monitor-remaining-work.md` — release-readiness items, including
  the real OAuth/OpenID delivery cutover.

---

# Implementation Detail Appendix

The sections above explain the product boundary. This appendix documents the
literal state transitions, parameters, refusal boundaries, and per-feature
mechanics used by the current implementation.

## A. Authority model, layer by layer

The server has independent authority predicates. It deliberately does not
reduce them to one global “safe” switch, because a different failed predicate
has a different safe remedy.

| Layer | Owner | Establishes | Does not establish |
| --- | --- | --- | --- |
| Discovery | `ToolRegistry` | whether a tool appears in `tools/list` | permission, session, freshness, or containment |
| Tool lock | `ToolRegistry` | this board was unlocked by the matching plan | a usable gate or address authority |
| Plan | `PlanEngine` | exact action, canonical parameters, scope, and budget | live hardware identity |
| Permission | `PermissionStore` | current action-specific decision when required | broader/future permission |
| Connection | `ConnectionManager` | one owned live handle per board | profile compatibility |
| Validation gate | run state / gate manager | live identity and map association | artifact/register containment |
| Map freshness | safety policy | replayed sources still match the stamp | missing source evidence |
| Typed containment | safety policy + tool | exact target bytes/segments suit the requested type | user approval or plan |
| Backend | pyOCD/pyserial adapter | bounded actual I/O | any policy assertion |

The normal guarded dispatch sequence is therefore:

```text
request → registry lock → exact plan → budget → permission (if required)
        → live session/gate → map freshness → typed containment → backend I/O
```

Every predicate is rerun at execution time. A plan does not pre-authorize a
different parameter object, a later artifact, a sibling board, or an action in
a different Server Run.

### A.1 Scope and revocation

All active authority is scoped to the current process/run, `board_id`, exact
live connection identity, action name, canonical nested action parameters,
remaining plan budget, and (where defined) action-specific permission. It is
not transferable across profiles, probes, reconnects, server restarts, later
plans, or batch children.

Authority is revoked when the action completes/uses its budget, the connection
is disconnected or replaced, a validation/identity condition changes, a map
anchor changes, recovery runs, or the process ends. Durable reports and files
are never used as a substitute for re-establishing it.

### A.2 Managed dispatch and concurrency

`RegistryFastMCP` assigns every managed call an operation ID and finite timeout,
starts passive monitoring, performs the guard, and acquires the named board’s
lock. Same-board calls serialize. Different boards can proceed concurrently.
The action body receives backend resources only after this sequence. The common
completion path owns finalizers, closing I/O, releasing reset, cleanup of owned
processes, lock release, and monitor observation.

`action_batch` has no alternative authorization lane: each child re-enters this
same dispatcher. Hidden tools remain registered, so a stale direct call still
receives the physical locked-tool refusal rather than reaching a backend.

## B. Dynamic discovery and plan protocol

The server registers physical handlers at start but marks guarded ones hidden
and locked. Visibility is presentation only; lock enforcement is separate.
`ToolRegistry` increments a list revision and sends `tools/list_changed` when
an action enters or leaves discovery.

### B.1 All-NULL plan initialization

Every general `*-plan` is first called with every one of its fields set to JSON
`null`. That query returns rendered guidance from the same declaration used to
validate the runtime call: purpose, intended and prohibited uses, exact field
order, nested action schema, budgets, permissions, safety mode, warnings,
preconditions, exit state, and a complete example.

The populated call must be one complete plan JSON object. Prose wrappers,
flattened action fields, missing nullable members, unknown keys, extra keys, or
permission fields on a plan that has no permission mode are rejected atomically.
The server specifically hardens registrations that would otherwise let FastMCP
drop unrecognized arguments, including plan tools and normal `connect`.

### B.2 Accepted plan behavior

An accepted plan snapshots exact canonical action parameters, board scope,
budget, and permission state. It unlocks the named action for that board and
returns an exact preferred direct call. Dynamic clients use the updated
`tools/list`; static clients can use only the exact returned single-child
`action_batch` fallback. That fallback must not be edited, extended, replayed,
or treated as permission to call any other hidden action.

All general populated plans use these outer fields in order:

```text
board_id, hypothesis, strategy, hypothesis_made, strategy_evaluated,
expected_fail_return, expected_success_return, max_calls, max_calls_buffer,
action_parameters[, user_permission]
```

`fixed` and `flexible` budget modes are defined by the action declaration. The
runtime consumes/checks accepted usage at dispatch, not when the guide is read.

### B.3 Exact nested action matrix

| Plan → action | Exact nested `action_parameters` | Notes |
| --- | --- | --- |
| `connect_override-plan` → `connect_override` | `probe_uid`, `target_override`, `external_board_config` | all nullable; values never rewrite profile |
| `connect_under_reset-plan` → `connect_under_reset` | `probe_uid`, `target_override` | reset line must be genuinely available |
| `flash_application-plan` → `flash_application` | `artifact` | local digest-bound ELF or HEX |
| `flash_bootloader-plan` → `flash_bootloader` | `artifact` | separate partition and permission |
| `read_memory_address-plan` → `read_memory_address` | `address`, `width`, `length` | width exactly 8/16/32; length nullable positive |
| `read_serial-plan` → `read_serial` | `expected_text`, `read_seconds`, `baudrate`, `port`, `reset_on_open`, `on_exit` | bounded capture; structured finalizer only |
| `register_write-plan` → `register_write` | `address`, `mask`, `value` | exact documented register field operation |
| `reset_and_halt-plan` → `reset_and_halt` | none | target reset, not target unlock |
| `serial_exchange-plan` → `serial_exchange` | `steps`, `read_seconds`, `baudrate`, `port`, `ready_text`, `ready_seconds`, `ready_probe_text`, `ready_probe_line_ending`, `ready_probe_delay_seconds`, `clear_input` | one bounded UART session |
| `set_breakpoint-plan` → `set_breakpoint` | `address`, `elf_artifact` | executable ELF containment |
| `set_execution_state-plan` → `set_execution_state` | `name`, `value` | supported architecture state only |
| `target_unlock-plan` → `target_unlock` | `recovery_mechanism` | fresh one-time destructive approval |
| `write_cpu_register-plan` → `write_cpu_register` | `name`, `value` | target-supported core register |
| `write_memory-plan` → `write_memory` | `address`, `value`, `width`, `elf_artifact`, `symbol` | destination resolved and contained |
| `write_serial-plan` → `write_serial` | `text`, `baudrate`, `port`, `append_newline`, `timeout_seconds`, `on_exit` | bounded write/finalizer |

`board_setup-plan` is separate from that general family. Its exact nested
object is `mode`, `connection_id`, `display_name`, `mcu_part_number`,
`requires_uart`, `serial_baudrate`, `serial_id`, and `datasheet_path`. Mode is
only `setup` or `repair`; a required UART has a positive baud rate, otherwise
that field is null. The server reads and hashes the supplied local PDF itself.

## C. Setup state machine, phase by phase

### C.1 Routing and assignments

`setup_overview` is the first routing call after the user gives familiar board
names. It normalizes the name list, accepts `no board` only as a standalone
sentinel, reads current project profiles, obtains a new unified inventory, and
returns server-generated next calls. It does not mutate the board merely because
a name matches.

Routes are deterministic:

| Current condition | Returned route |
| --- | --- |
| completed matching logical profile | load `board_validate`, then validate |
| unknown board name | load `board_setup-plan`, initialize/submit plan, setup |
| incomplete/failed same profile | plan in repair mode, then `board_fix_setup` |
| stale/invalid map with recoverable replay evidence | `board_safety_refresh` |
| native probe/UART absent | host check, then optional hook contract route |

The server keeps current `connection_id → board_id` assignment only in memory.
It enforces one-to-one mapping across active selections. Connection IDs are
opaque values and must be replayed exactly; they are not editable probe serials.

### C.2 Setup phases and results

An accepted `board_setup-plan` permits one primary setup action, then at most
one paired repair action if the first action is incomplete/repairable. Setup is
not a monolithic client script; the workflow advances persistent evidence only
after each deterministic phase succeeds.

1. **Input/replay:** validate mode, display name, exact MCU ordering code,
   selected connection, UART requirements, and local datasheet path. Preserve
   the MCU text exactly; it is never changed to match an unexpected live chip.
2. **Fresh preflight:** enumerate probes, UARTs, active sessions, known profile
   support, target candidates, and local artifact evidence. Cached inventory is
   never substituted for live enumeration.
3. **Physical selection:** use a stable attachment-cache entry only if it is an
   exact, unique match. Otherwise return friendly probe/UART choices for the
   agent to relay. Raw COM/tty paths are never stored as identity.
4. **Support replay:** reuse only the profile’s verified canonical support
   binding. If that is unavailable, request one official pack candidate rather
   than accepting a naked target-name assertion.
5. **Pack admission:** stage bytes; compute SHA-256; verify readable archive and
   PDSC structure; select the exact device leaf; enumerate targets; require the
   part-consistent target; attempt a non-destructive live attach. No failed or
   unverified candidate is promoted.
6. **Connection/identity enrichment:** connect through the selected probe using
   built-in or staged support. Optional test reads/silicon masks are accepted
   only if requested, safely readable, and verified against the live target.
7. **Safety evidence:** replay Pack/CMSIS/SVD/PDSC facts and authoritative local
   datasheet bytes. When an official-document fact cannot be derived, issue a
   narrow research continuation; do not ask the user to invent addresses.
8. **Map:** derive physical geometry, blocks, prohibited spans, supported
   peripheral facts, partitions/allocation authority, fingerprints, and overlap
   checks. A missing source keeps the affected action closed.
9. **Commit:** atomically write the profile, exact support binding, evidence,
   map, and immutable setup report/log; close the setup allowance. Then route to
   validation, because completed setup is not an open live gate.

### C.3 Continuations, retries, and repair

`continue_setup` consumes a server-issued continuation ID plus only the exact
accepted response shape requested by the preceding status. It is not a generic
“run phase N” RPC. Continuations are run-scoped; research candidates are
validated, recorded, and may not be silently re-submitted as unchanged failed
data.

Common outcomes are `setup_needs_user_input`, `setup_research_required`,
`setup_blocked`, `setup_unresolved`, `setup_connection_failed`,
`setup_validation_failed`, `setup_safety_incomplete`, and `setup_completed`.
Each nonterminal response explains the next conversational or evidence step via
`agent_prompt`; the agent relays prose/friendly choices but not JSON internals.

`board_fix_setup` resumes the first unverified phase from the record but repeats
current preflight and connection checks. It never blindly trusts an old device
inventory, previous port path, or stale external pack assertion.

## D. Connection, validation, and gate lifecycle

### D.1 Normal and exceptional connection

Normal `connect(board_id)` is profile-only. It loads the project profile,
replays its support binding, resolves the profile-matched/assigned probe, opens
one managed target session, and records routing state. It refuses extra manual
target, probe UID, or board-config arguments rather than silently ignoring them.

`connect_override` is available only through `connect_override-plan`; it accepts
nullable `probe_uid`, `target_override`, and `external_board_config` solely for
this run. The values do not modify profile YAML. `connect_under_reset` is
separately planned: it asserts wired reset, attaches and halts, then releases
reset. A probe without reset support refuses instead of degrading to ordinary
attach and calling it success.

### D.2 Board validation algorithm

`board_validate(board_id, probe_id?)` performs no flash, erase, pack install,
or behavior assertion. Its successful path is:

1. Load the profile and map and reject missing, malformed, stale, or
   authority-incomplete safety state.
2. Refresh inventory; resolve the current selected probe, returning a friendly
   choice only where physical assignment remains ambiguous.
3. Replay exact built-in/verified-pack support from bytes and bindings rather
   than trusting a manifest’s descriptive target string.
4. Connect with a bounded temporary validation session.
5. Read only replayed safe identity evidence; apply exact/compatible identity
   rules and masks. If safe identity proof is absent, report diagnostics but do
   not stamp a gate.
6. Promote only the exact current connection to the logical profile, bind map
   digest plus connection identity to the run, and open that one board’s gate.
7. Persist immutable validation evidence. On mismatch/refusal, close provisional
   resources and leave the gate closed.

### D.3 Invalidating events

| Event | What is cleared | What does not restore it |
| --- | --- | --- |
| Server restart/new MCP run | all connections, plans, permissions, stamps, gates | `.firm` reports or map files |
| disconnect/closed handle | named assignment, session, validation stamp, gate | attachment cache |
| probe/session identity replacement | prior proof | prior validation result |
| mismatch/identity repair | provisional promotion and proof | matching target name alone |
| destructive recovery | validation proof and gate | recovery plan or approval |
| same-anchor safety refresh | proof can be re-stamped if still live | refresh cannot create identity proof |
| part/target/map-anchor change | map association/gate | profile field edit or report |

There is no arbitrary idle timeout. Validation is repeated after the listed
identity changes, not after normal build, flash, reset, UART work, collection,
or routine bookkeeping.

## E. Safety map source ownership and enforcement

### E.1 Source ownership

| Fact | Authority source | Consequence |
| --- | --- | --- |
| MCU ordering code | user/profile | preserved exact; unexpected silicon does not rewrite it |
| target/PDSC device facts | verified pack/built-in pyOCD + live attach | replayed before use |
| physical RAM/ROM/flash/erase facts | pack/PDSC/target | geometry, not deployment ownership |
| peripheral/SVD blocks | verified Pack/SVD | explicit blocks only; no gap filling |
| security/option/OTP/lifecycle spans | reviewed official evidence reconciled with server facts | explicit prohibited override |
| app/boot deployment ranges | reviewed partition policy or generic allocation | no inference from full flash capacity |
| symbol/segment locations | current ELF/HEX parsing | action-time containment input |
| probe/UART attachment | stable host cache | hint only; never authority |

The map stores fingerprints/provenance. A source report or manifest sibling is
evidence, not a second authority source used to restore safety state.

### E.2 Map invariants

Every range has ordered integer bounds and an explicit type. Prohibited spans
remain representable and override broader flash/peripheral ranges. A partition
cannot overlap prohibited or incompatible ownership. Flash operations use known
erase-sector geometry instead of caller-expanded ranges. Schema-v3 generic maps
retain distinct physical blocks and optional peripheral blocks without joining
gaps; an unreadable existing generic map is not silently replaced because its
one-way allocation history might be unrecoverable.

### E.3 Concrete containment rules

* **Raw reads:** calculate the actual byte interval pyOCD will access. Refuse
  unknown/unmapped spans, boundary crossings, and write-only peripherals. A
  specifically mapped sensitive span can be read for inspection without becoming
  writable.
* **Symbols:** require a regular current ELF, hash it, resolve one symbol, reject
  function symbols for scalar data access, too-small/unknown/alignment-invalid
  objects, re-hash before I/O, then contain the resolved bytes.
* **Memory writes:** parse integer strings/base prefixes strictly, reject boolean
  values, require 8/16/32-bit width, and authorize actual bytes rather than a
  caller’s requested category label.
* **Peripheral writes:** require a documented writable register. Normal fields
  use masked read-modify-write; write-only registers require full 32-bit mask.
  Security/provisioning/OTP/lifecycle/option-byte spaces refuse before I/O.
* **Breakpoints:** use executable segments from the selected ELF—not all flash—
  to decide eligible addresses.
* **Flash:** validate artifact digest, format, target compatibility, segments,
  entry/vector conditions as applicable, partition/allocation containment,
  prohibited exclusion, and affected erase sectors. No caller address expands a
  HEX/BIN image’s authority.

## F. Detailed behavior of the operational tools

### F.1 Session and execution tools

| Tool | Inputs | Exact effect | Authority consequence |
| --- | --- | --- | --- |
| `connect` | `board_id` | opens profile-derived managed target session | no gate merely from connection |
| `disconnect` | `board_id` | closes named target/UART resources only | clears named proof/gate |
| `get_board_info` | `board_id` | returns active profile/routing facts | read-only |
| `get_state` | `board_id` | queries observable core run state | read-only |
| `halt` | `board_id` | halts target core | target remains halted |
| `resume` | `board_id` | resumes core | target runs |
| `step` | `board_id` | single-steps and reports resulting PC | changes execution state |
| `reset_and_run` | `board_id` | resets and starts reset vector | does not unlock security |
| `reset_and_halt` | planned `board_id` | resets then halts at startup | planned execution mutation |
| `wait` | `board_id`, `ms` | bounded wait in operation lifecycle | no invented hardware mutation |
| `connect_override` | planned manual fields | exceptional run-scoped attach | never persists override |
| `connect_under_reset` | planned UID/target | reset assert, attach/halt, reset release | validate current session before gated work |

`get_board_info` and `get_state` are intentionally informative only. A profile
being readable through them does not prove a target is connected or validated.
`halt`, `resume`, `step`, and reset preserve the state documented by their own
tool; common cleanup does not “helpfully” reset a board after normal success.

### F.2 Registers and execution state

`read_cpu_register(board_id, name)` reads one target-supported named register.
The target adapter determines supported names rather than assuming every
architecture has a particular ARM register set. `read_execution_state` is a
read-only view of supported target execution state.

`write_cpu_register`, `set_execution_state`, and `register_write` are distinct
plan-locked mutations. This prevents read access from automatically becoming
register/control access and prevents a memory-mapped peripheral write from
being represented as a core-register change. Each parses and records values,
runs under the board lock, and passes the containment rules in Section E.

### F.3 Symbols and memory

`find_symbol(board_id, query, elf_artifact?)` finds matching ELF symbols and
returns name, address, size, and ELF type. Empty queries, unavailable files,
non-ELF files, parse errors, and artifact changes receive typed refusals.

`read_memory_symbol(board_id, symbol, width=32, elf_artifact?)` requires a
sized data symbol. It refuses executable/function symbols, invalid widths,
unaligned scalar access, unknown size, and a scalar wider than the data object.
It then checks the resolved concrete memory range before reading.

`read_memory_address(board_id, address, width=32, length?)` is the planned raw
inspection route. `width` is exactly 8, 16, or 32; with no `length` it reads one
scalar, and with `length` it reads a bounded block. It never avoids map policy.

`write_memory` follows its exact plan parameters (`address`, `value`, `width`,
`elf_artifact`, `symbol`) and resolves/contains the concrete final destination.
Symbol names are convenience for resolution, not an address authorization
mechanism. A same-run application flash can create only a temporary ELF
convenience binding; after restart the current local ELF is supplied explicitly.

### F.4 Breakpoints

`set_breakpoint` is plan-locked and binds the chosen ELF/address. It proves the
address belongs to a currently selected executable segment, then uses the target
adapter’s breakpoint mechanism. `remove_breakpoint(board_id, address)` removes a
previous breakpoint through the ordinary target operation path. Breakpoint
containment deliberately does not claim that every mapped application-flash byte
is executable.

### F.5 UART

The server resolves stable UART identity to a current port immediately before an
operation. A supplied `port` is runtime-only override/context; it is not written
back as a durable attachment identifier.

`read_serial` opens the resolved UART for a plan-bounded duration. Its plan can
bind expected text, baud rate, port, reset-on-open behavior, and permitted
structured exit finalizer. Captured output is returned as reversible
JSON-escaped text. Matching text is useful test evidence but is not live MCU
identity authority.

`write_serial` sends explicit text, optional newline, and bounded timeout.
`serial_exchange` validates its steps then opens the UART once for a state-
preserving bounded command/response run. It may wait for readiness text, send a
specified readiness probe using an explicit line-ending/delay, clear input, and
read bounded responses. It is not an arbitrary shell or serial scripting
escape hatch.

Only `uart_write` and `reset_and_run` are accepted finalizers on eligible serial
tools. They are structured, best-effort actions that run before mandatory
cleanup; no arbitrary callback/code finalizer is accepted.

### F.6 Flash and recovery

`flash_application` and `flash_bootloader` are different hidden actions with
different plan definitions and partitions. A plan binds a selected artifact by
digest; modification after planning refuses before permission, budget,
containment, or backend work. The parser validates ELF/HEX structure and load
segments. Where HEX coherence needs an ELF, the corresponding ELF is required.

Application flashing requires current fresh-write authority and full containment
in application allocation/partition. Bootloader flashing has an independent
partition and stronger permission boundary. Neither may program unknown,
prohibited, ROM bootloader, or the other partition. Once programming begins,
the action becomes non-interruptible: cancellation waits for bounded safe
transaction completion before managed cleanup.

`target_unlock` is deliberately not “flash with an extra flag.” Its plan takes a
recovery mechanism, needs fresh one-time destructive approval, verifies current
backend capability, and discloses only `backend_mass_erase` or `manual_only`
where the exact map supports that statement. Completion leaves the validation
gate closed; recovery approval cannot be reused as post-recovery authority.

### F.7 Batches and artifact collection

`action_batch(board_id, actions)` validates all children before execution:
nonempty exact shared board ID, JSON-only arguments, existing tool name, no
surrounding whitespace, and no nested `action_batch`. Monitor actions are not
batchable because they have no board scope and must not enter the board
serialization path. Children run in order through ordinary dispatch. Execution
stops on the first exception and reports `batch_completed` or `batch_failed`,
completed children, failing index/tool, and typed error.

`collect_build_artifacts` takes a destination plus only explicit ELF/HEX/BIN/map
paths that a native build actually produced. The destination must be new or
empty. It stages canonical roles, structurally checks understood formats,
computes SHA-256, and writes provenance. `expected_roles` makes an expected set
explicit. It never searches, builds, downloads, opens hardware, creates a map,
or opens a gate. A raw BIN has no trusted load address merely because it was
collected; HEX/BIN-only collection remains provenance only.

## G. Probes, providers, drivers, and process isolation

The adapter boundary separates core policy from pyOCD provider implementation.
Probe inventory normalizes native provider information into stable or explicitly
session-local identities. Remote probe-server registration is explicit; a
registered endpoint can be unreachable and still remains an inventory record.
The registration TCP check is bounded and does not open a target session.

Host driver/permission absence is intentionally distinct from a discovery
problem. When `pyocd list --probes` sees no probe, the likely remedies are
cable, debugger driver, OS permission, or supported provider installation.
Discovery hooks cannot manufacture that missing driver capability.

The J-Link path has special process behavior: pylink DLL instances cannot always
be safely shared for multiple simultaneous sessions. The first live J-Link uses
the ordinary DLL path; additional concurrent sessions receive isolated temporary
DLL instances. If close cannot be confirmed, the reservation is retained and
future sessions stay isolated rather than risking reuse. The allocation is based
on live provider/session state—not board name, target string, or USB location.

Subprocess helpers use validated argument vectors (no shell reconstruction),
finite timeouts, owned process groups, and ownership identity markers. Startup
hygiene performs bounded cleanup only for processes it can identify as this
server’s own, avoiding destructive cleanup of unrelated developer processes.

## H. Kernel lifecycle and cleanup

The operation manager attaches finite deadlines and cooperative cancellation to
actions. The single managed cleanup owner is responsible for stop-I/O, UART
close, debug/session close when required, owned process-group termination,
reset release, and board-lock release. It is idempotent so failures in one
cleanup branch do not cause duplicate resource ownership behavior.

Conceptually, completion is:

```text
action result or exception
  → permitted best-effort structured finalizer
  → mandatory resource close / reset release / owned-process cleanup
  → lock release and final result
```

Normal successful operations retain their documented MCU state. Only explicit
reset tools or accepted reset finalizers cause reset/run behavior. Stdio EOF and
normal shutdown use the same ownership path rather than leaving live sessions
to a client process exit.

## I. Monitor and Sentry implementation detail

### I.1 Passive observation and records

The monitor observes managed dispatch but never becomes a prerequisite for it.
It tracks tool name, board scope, argument fingerprint rather than raw sensitive
arguments, duration, classified outcome/error type, guard-state fingerprint,
and a bounded per-board trail. Monitoring exceptions are swallowed and counted
internally so logging cannot block or alter a hardware request.

It creates occasions rather than an unbounded permanent per-call transcript:

* boot;
* cumulative usage snapshots at the configured 100-call cadence;
* routine check-in prompts at 500 calls in narrative/personal builds;
* deduplicated issue reports; and
* closeout.

The append-only segment ledger is hash-linked. It detects ordinary corruption,
partial writes, and localized accidental edits; it does not prevent the local
machine owner from rewriting records and recomputing a public hash chain.

### I.2 Monitor-facing tools

`server_health_check` is read-only and returns run/uptime, counters, exercised
versus advertised coverage, ledger record/head state, store/workspace state,
transport/delivery anchor, narrative capability, and staleness-block state. It
sends and writes no report.

`report_agent_issue` is only for server behavior that is actually wrong or whose
named remedy is absent/wrong/non-convergent. Correct locked-tool, plan, gate,
containment, and `no board` refusals are not reportable defects merely because
they are refusals. The form includes signal class, codebase objective,
hypothesis/goal/plan, exact failure point, up to five recent actions, earlier
phase summary, session start, and required subcase for designated signals.

`submit_routine_checkin` is a separate normal activity record, not an error.
It is included only in narrative/personal builds and accepts codebase summary,
phase-level work summary, tool/purpose list, and observable effectiveness. In a
professional build, user-authored narrative reporting is disabled so project
code descriptions are not authored/stored/sent through that feature.

### I.3 Current transport is a local filler

The default monitor transport is `SimulatedRemoteTransport`, not OAuth, OpenID,
or a real Sentry remote. It writes to the resolved monitor store (the exclusive
`BYO_MCP_MONITOR_ROOT` when configured, otherwise the baseline per-user/app-root
chain):

```text
BYO/
  server_data/                 active local ledger/report work
  simulated_remote/<workspace>/
    ledger/                    locally copied acknowledged segments
    reports/                   local JSON reports and Sentry envelopes
```

The SDK client is constructed with `dsn=None`, no default integrations, PII
sending disabled, stack attachment disabled, `environment="filler"`, and a
custom local envelope writer. A local report JSON copy is written even when the
SDK path has a local problem. Result state is `FILLER_SIMULATED`, never `SENT`;
`is_durable_off_box` is true only for `SENT`.

The delivery worker is background-only so slow delivery cannot occupy the MCP
hardware request path. It queues boot, ledger/report/summary, and closeout work,
tracks acknowledgements/anchors, and applies staleness behavior. A filler ACK
can release the active `server_data` work while preserving the local simulated
copy, but it is not an external witness and has never left the machine.

There is currently no OAuth authorization/device flow, access or refresh token,
OpenID identity, authenticated HTTP uploader, real DSN configuration, off-box
ACK, or simulated-backlog replay service. Those remain required cutover work.
