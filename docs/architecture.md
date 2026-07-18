# BYO Server architecture

## Product boundary

BYO Server is a paired local product. Server A is the user-facing stdio turnkey brain and owns one
fresh provider-neutral middleman process per agentic call. Server B is the guarded board manager;
the one-command entry point verifies and reuses an explicitly managed singleton loopback
streamable-HTTP manager, or starts a session-owned one and terminates it at Client A EOF. A
cross-process OS lease prevents a second Server B from owning the probes.
Client A registers Server A's stdio command and may also register the supervised loopback Server B
HTTP endpoint for direct setup, guardrail, and hardware tools. Both registrations reach the same
Server B manager that the middleman uses; one `pyocd-turnkey` process owns their lifetime.
Direct Server B stdio remains available only when it can acquire the same lease. Neither Client A
nor a middleman is hardware safety authority.

```text
Client A over stdio
        |
        v
turnkey/launcher.py -> Server A (turnkey/server.py)
        |                         |
        | fresh middleman         | verified singleton endpoint
        v                         v
provider wrapper ----------> Server B streamable HTTP
                                  |
                                  v
server.py guarded hardware composition
        |
        +-- kernel: registry, managed dispatch, lifecycle, process ownership
        +-- guardrails: plans, permissions, validation gate
        +-- safety: reviewed maps, regions, and per-action containment
        +-- setup_flow: inventory, research, setup, validation
        +-- tools: schemas and board-facing handlers
        +-- services/adapters: board routing, selectable target backend, serial, symbols
        |
        +-- FirmStore: durable profiles/evidence under .firm (never run-scoped gate authority)
```

`.firm/setup/` and `.firm/validation/` are local immutable runtime reports and are ignored by Git;
acceptance evidence selected for review is copied to `docs/evidence/`. Safety authority consists
only of each board's `memory_map.yaml`; obsolete source-manifest/report siblings are removed and
ignored so normal execution cannot dirty tracked history with them.

The client chooses what to request. The server independently checks whether
the named operation is visible, planned, permitted, scoped to the live board,
safe for the current stable map. Discovery is guidance, not
authorization: hidden tools remain registered so a stale direct call reaches a
physical handler lock and receives the same prerequisite refusal.

## Layers and ownership

`server.py` is the composition root. It creates one process-local
`ServerRun`, `ConnectionManager`, `ToolRegistry`, `PlanEngine`,
`PermissionStore`, `GateManager`, safety policy, setup services, and board
adapters. Business rules live in their owning modules rather than in the
composition root.

Schema-v2 profiles in the selected project `.firm/boards/` root are the primary
normal-connection source. The checkout `boards/` directory is a read-only
compatibility fallback. Fresh automatic setup is advertised only for catalog entries with a
pinned official datasheet, a distinct pinned device-support document, and an exact installed
backend identity. The bundled catalog is pyOCD-backed; other MCU/toolchain providers register
equivalent reviewed records and an evidence loader through
`pyocd_debug_mcp.reviewed_board_support` without changing Server B. It recomputes every
hash, parses both authorities through the strict evidence schema, and persists
only regions accepted by deterministic two-source reconciliation. An empty or
drifted pin fails closed. Live silicon reads and the user-supplied datasheet
bytes are required before committing a profile or safety
baseline. Validation promotes the exact live connection; no pseudo-connection
stamp can satisfy the readiness barrier.

The kernel provides the protocol and lifecycle boundary:

- `kernel/registry.py` filters dynamic tool discovery, sends
  `tools/list_changed`, rechecks handler locks, and routes every call through
  managed dispatch.
- `kernel/operations.py` assigns a finite timeout and operation identity,
  enters the shared board-affecting execution queue, connects MCP
  cancellation to cooperative cancellation, and owns one idempotent cleanup
  path.
- `kernel/finalizers.py` accepts only the structured `uart_write` and
  `reset_and_run` finalizers on eligible serial tools. Finalizers are
  best-effort and run before mandatory cleanup.
- `kernel/processes.py` owns validated argv, finite subprocess bounds, process
  groups, and identity markers. `kernel/hygiene.py` performs bounded startup
  cleanup only when the live process identity still matches.

`ConnectionManager` is the only owner of live board handles. It enforces one active connection per
logical board and one logical board per stable connection identity. Every board-affecting operation
shares one process-wide queue; metadata calls remain concurrent. Disconnect clears only the named
board's connection and run-scoped authority.

The one process is protected by `kernel/singleton.py` so separate Client A launches cannot create
independent hardware queues. The built-in `pyocd` target backend is selected through the same
production registry used by external `pyocd_debug_mcp.target_backends` entry points. Selection is
per profile, not one global process choice. Board runtime logic consumes a backend-neutral target
identity; `pyocd_target` remains a read-compatibility field for existing profiles only. Artifact
containment is similarly selected through `pyocd_debug_mcp.artifact_evidence`, with
content-recognized ELF/Intel HEX as the built-in provider and provider-declared dependency bytes
bound into each plan. Symbol lookup and executable breakpoint resolution use the separate
`pyocd_debug_mcp.symbol_providers` entry point. The bundled provider recognizes ELF by content;
other toolchains can supply their native symbol/index format without adding vendor branches to
Server B.

## Plans and permissions

`guardrails/plan_defs.py` is the declarative source for each plan tool's
purpose, fields, exact action schema, budget, permission mode, safety mode,
timeout, and all-NULL guidance. Clients must initialize a plan tool by sending
the universal envelope with every field NULL, then submit only a complete JSON
envelope whose `action_parameters` member is one nested object binding exactly
to the eventual call. Flattened action fields, prose/wrapper payloads, missing
or extra fields, and permission fields on non-permission populated plans are
rejected atomically. Each all-NULL response renders the mechanism, purpose,
use/not-use cases, fields, validation, budget, permission, preconditions,
warnings, soft guardrails, exit state, and a complete example from
the same definition. `docs/plan-tool-contract.md` is a deterministic human-readable
rendering of every live plan/action field, budget, and permission mode. Archived
design prose is historical evidence rather than a second runtime authority.
After a populated plan is accepted, the response switches to a compact structured unlock payload:
the unlocked action, exact preferred call, unchanged static-client fallback, bounded usage guidance,
and reminders. It never repeats the initialization tutorial.

The pinned FastMCP SDK normally ignores unknown function arguments while
building its Pydantic call model. Plan-tool registration deliberately rebuilds
only those generated argument models with `extra="forbid"`, publishing
`additionalProperties: false`, so unknown inputs reach neither normalization
nor plan activation. The engine independently repeats exact-envelope and
nested-action validation; SDK visibility never substitutes for the handler
lock or policy checks.

`PlanEngine` scopes a plan to the current run, tool, board, session, canonical
parameters, and call budget. It atomically decrements once at execution start.
Pre-start refusals do not consume a call; failure, timeout, or cancellation
after start does. Replacement, exhaustion, invalidation, disconnect, and run
closure relock the action.

`PermissionStore` provides structured `one-time` and `full-session` grants.
One-time permission is consumed at execution start. Full-session permission
removes repeated prompting only where the plan definition allows it; it never
authorizes mass erase. Plans and permissions live only in `ServerRun` and are
empty after restart.

## Validation gate and safety

The write gate is default closed. Only a successful `board_validate` can create a run-scoped live
identity proof. Validation is deliberately lean: it selects the stable probe, makes a bounded
non-mutating connection, reads the reviewed silicon identity, checks the schema-v2 profile and
single memory map, and binds that map's canonical digest. It does not test UART or firmware
behavior, collect builds, rebuild safety, or rewrite the profile.

Call validation only when (1) the run has no live proof, including startup and initial setup; (2)
the physical/logical connection or probe identity changed; or (3) identity repair or destructive
recovery may have changed the hardware. Builds, flashes, resets, UART work, bookkeeping changes,
and same-connection safety refresh are not validation triggers.

Guarded dispatch validates the exact plan, permission, run/board/session binding, live connection,
map stamp, action containment, lock, and timeout before consuming budget and entering the backend.
Reads deny UNKNOWN and PROHIBITED spans. Writes additionally require the current canonical map
digest. Disconnect, connection changes, restart, identity repair, and destructive recovery clear
the live proof; refresh may update only the map digest on a still-valid same-connection proof.
Neither is persisted.

`safety/regions.py` supplies typed half-open ranges and prohibited-overrides-all classification.
`safety/linker.py` parses the selected ELF/HEX at execution. `memory_map.yaml` contains stable
reviewed identity, geometry, application/optional bootloader partitions, semantic source digests,
and region provenance. It is the only persisted file under `.firm/safety/<board_id>/`.

`board_safety_refresh` is the maximum safety recovery. It deterministically rederives the complete
map from current server-owned reviewed sources and atomically replaces only `memory_map.yaml`.
Missing, corrupt, old-schema, inconsistent, geometry-changed, partition-policy-changed, or reviewed
evidence-changed maps all use refresh and never route through setup. Refresh preserves a valid live
identity proof but never creates one. The obsolete `board_safety_setup` public surface is removed.

Firmware binaries are not persistent map authority. Every application/bootloader flash parses the
actual selected ELF/HEX and checks live target identity, all load ranges, entry/vector, stable
partition containment, prohibited overlap, and every required erase sector before backend mutation.
The populated flash plan hashes the artifact; execution rehashes it before budget or permission
consumption and rejects changed bytes. A normal build therefore follows `build -> collect -> flash
plan -> flash-time containment`; it does not require refresh. HEX requires its matching ELF.

Breakpoints likewise bind the current ELF in the plan and permit an address only inside that ELF's
executable loadable sections. Stable application partitions are not treated as wholly executable.
Memory writes remain RAM-only, peripheral writes exclude prohibited ranges, and recovery remains a
typed one-time operation that clears the gate.

Normal connection is structurally separate from manual override. The visible `connect(board_id)`
uses only the profile; guarded `connect_override` owns run-scoped manual probe/target/config values.
Batch children traverse the identical strict dispatch path.

## Setup and agent relay boundary

Setup deterministically inventories probes, serial ports, cache matches,
targets, and builds before requesting research. Unknown facts are returned as
strict research requests; blocked physical conditions are not mislabeled as
research. Candidate replies must contain exactly the requested fields and
cannot alter the exact user-supplied MCU part number.

`setup_overview` is the entry adapter between ordinary familiar board names and
internal profile/connection routing. The normalized `no board` sentinel is
handled before route construction. Each route composes the exact loader,
validation, or plan-initialization call and pre-fills server-known action
fields, including stable attachment identities. Volatile port paths remain
diagnostic and are resolved again at execution. `load_setup_tool` returns one
bounded, tool-specific guide instead of the entire setup manual. Validation
choice results carry an executable retry recipe that retains already-resolved
selectors. `continue_setup` is the reverse adapter
for one friendly choice or strict research response. It is scoped to the live
board continuation, grants no authority, and feeds the accepted selection or
target into the paired repair attempt. Pack candidates are staged under the
project `.firm` root, checked, enumerated, live-connected, and only then added
to the authoritative project manifest.

`get_setup_status` is the explicit pre-code barrier. It always returns a
provider-neutral native-build and visible artifact-collector handoff. Reviewed
Zephyr profiles may additionally return an optional, labeled and parameterized
Zephyr terminal fallback. This guidance is never safety authority; the
resulting ELF/HEX must pass its flash plan and direct execution-time containment checks before
application flash. Ordinary builds do not require `board_safety_refresh`.

Safety refresh always rebuilds one complete candidate from reviewed sources. It has no
build-artifact scoped mutation mode, no source manifest, and no safety report. Missing reviewed
evidence is an honest maintainer blocker, never a caller-supplied-range prompt. A live MCU mismatch
is different: validation reports expected and observed identities, asks the user what they want,
and never rewrites or automatically re-runs setup. Keeping different hardware requires a new logical
profile so the established profile remains intact.

`scripts/run_fresh_workspace_e2e.py` is a narrow real-stdio adapter for a clean
artifact root. Its input surface is fixed to board/probe/UART/datasheet
identity plus a one-attempt setup authorization. It emits an immutable-style
acceptance transcript at a fixed path and contains no downstream execution
hook. Therefore a setup refusal, timeout, incomplete continuation, failed
validation, or false readiness result is terminal; a separate orchestrator may
start a coding agent only after reading a `pass` record whose
`ready_for_code` value is true.

Setup and validation return structured control payloads for the agent plus an
`agent_prompt` written as ordinary prose. The agent must relay only that prose
and friendly choices, never structured payloads, continuation tokens, internal
field names, or machine identifiers unless a destructive approval explicitly
requires the exact live identity. See [agent-contract.md](agent-contract.md).

The CLI `stage0_check.py` and MCP validation both adapt inputs into the same
`setup_flow.validate.BoardValidator`; they do not maintain parallel validation
implementations.

## Durable `.firm` artifacts

`FirmStore` is the single layout and low-level write owner:

```text
.firm/
  boards/       schema-v2 board profiles
  packs/        authoritative pack manifest and downloaded files
  setup/        immutable setup attempts and append-only logs
  safety/       one memory_map.yaml per board
  validation/   immutable validation and recovery attempts
  cache/        revocable host attachment hints
```

Writes are project-local, atomic, and checked for authority-bearing keys.
Profiles preserve the exact user-supplied MCU part number and Unicode display
name. Pack identity belongs to `packs/manifest.yaml`, not profiles. Cache
records contain only stable attachment hints.

The following are deliberately never persisted: live connections and
assignments, active plans and remaining budgets, permissions, unlocked tools,
validation stamps, and open-gate state. Durable reports are evidence, never a
way to restore authority after restart.

## Batch and lifecycle behavior

`action_batch` validates the entire bounded child list for one shared board and
rejects recursion before starting. It does not pre-authorize or pre-consume
children. Each child traverses the identical direct-call dispatch path and
observes any plan, permission, gate, or freshness change caused by earlier
children. Execution stops at the first failure.

Accepted plans also render an exact one-child batch as a compatibility route
for MCP clients that do not refresh callable bindings after
`notifications/tools/list_changed`. The server builds it from the immutable
accepted snapshot (`board_id` plus canonical action parameters), never from
model prose. Direct execution remains preferred. The compatibility child does
not carry permission state and does not bypass hidden-handler locks or any
dispatch check. Paired setup repair is returned separately and is valid only
after the primary setup response establishes that route.

Managed cleanup owns stop-I/O, UART close, debug/session close when required,
owned process-group termination, reset release, lock release, and the final
board state. Flash becomes non-interruptible after its transaction starts, so
cancellation waits for bounded safe completion before resources are released.
Ordinary successful work preserves the action's documented MCU state; cleanup
does not silently reset it. Reset-and-run is explicit through a reset tool or
eligible structured finalizer. Ordinary stateful work is cooperatively
interruptible. Stdio EOF and normal shutdown use the same cleanup ownership.

## Build artifact intake

Firmware builds remain native-project work: the agent or developer uses the
validated local IDE/CLI and existing SDK. The always-visible
`collect_build_artifacts` MCP tool then provides an optional build-system-neutral
handoff for explicit native outputs. Common ELF/HEX/BIN/MAP fields are convenient,
while a bounded `native_artifacts` role-to-path mapping carries provider-defined formats
without changing the collector. It performs no build, search, subprocess, download, or hardware
access. Collection stages a canonical bundle and deterministic SHA-256 manifest outside `.firm`; the
manifest contains provenance but no allowed ranges, plans, permissions, or gate
state. Current safety refresh consumes the returned ELF/HEX/MAP paths explicitly
and remains the sole route to build-derived containment evidence.

The Zephyr helper is a separate terminal convenience, not an MCP hardware action.
It reuses the same collector after selecting the declared sysbuild default domain,
which keeps the application ELF and linker map coherent and preserves the native
incremental build tree. Other build systems need no provider adapter: their normal
build output can enter through the same visible collector.

## Target and host de-biasing boundaries

Reviewed board identities, parts, geometry, attach facts, and evidence hashes
live in the packaged `setup_flow/reviewed_boards.json` data resource. Production
setup code validates that schema and contains no branches for a particular board
name or memory address. Missing read/attach/recovery facts remain missing; no MCU
prefix invents them. Validation refuses missing safe-read evidence before a
backend connection and names setup repair as the remedy.

Serial association uses stable USB identity and generic metadata scoring first.
Optional vendor helpers are selected from the packaged
`serial_fallbacks.json` registry only when generic evidence stays ambiguous;
their executables come from an explicit environment path or `PATH`, never a
compiled host installation path. Recovery exposes only `backend_mass_erase` or
`manual_only`, checks the connected backend capability before disclosure, and
retains the existing exact-map disclosure and fresh one-time approval boundary.

## Contracts, performance, and historical evidence

The active MCP contract is `tests/contracts/product-server-tools.json`. It
imports the frozen M9 schema/implementation baseline by digest and records M10
hardening evidence. Extraction-era snapshots and
`docs/extraction-manifest.json` remain immutable historical provenance; they
are not the active product contract. The transition is documented in
[contract-history.md](contract-history.md).

`scripts/measure_m10_performance.py` measures gate/freshness, eight-device
enumeration, and NULL-plan/handshake latency with host context. Tests validate
the measurement and report target misses as warnings so host speed does not
become a CI correctness gate. A dated local result is retained under
`docs/evidence/`.

The server is supported operationally from the complete checkout. Wheels and
sdists prove metadata, entry points, and importability; they do not include the
board, pack, firmware, or evidence roots needed for board control.

## Verified

The software suite exercises the layers and invariants described here,
including stdio discovery, handler locks, `InMemorySessionStore`, per-board
dispatch, exact plans, permission consumption, safety freshness, cleanup,
contract ownership, and M10 hardening assertions. This is checkout-only
software evidence. The optional R11 harness retains its legacy Codex adapter
and can instead use an explicit, operator-owned argv adapter for any CLI or
thin wrapper that satisfies the documented prompt/result contract. The MCP
server itself remains provider-neutral over stdio; the harness does not pretend
that vendor-specific CLI flags or MCP registration formats are standardized.

## Pending verification

Bench evidence remains separately labeled in `docs/verification.md`. Exact
official-board coverage (`nrf52833dk` and `nucleo_l476rg`), alternate
`nrf52840dk` evidence, external-client behavior, cross-host process-tree
cleanup, and destructive authorization are not inferred from software tests.
