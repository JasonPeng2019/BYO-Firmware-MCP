# BYO Server architecture

## Product boundary

BYO Server is a local, checkout-operated MCP server for board setup, debug,
flash, serial, and recovery through pyOCD and pyserial. The only server
transport is stdio. It does not listen on a socket, embed an agent, or trust an
MCP client as a safety authority.

```text
MCP client over stdio
        |
        v
server.py composition root
        |
        +-- kernel: registry, managed dispatch, lifecycle, process ownership
        +-- guardrails: plans, permissions, validation gate
        +-- safety: reviewed map authority, regions, runtime containment
        +-- setup_flow: inventory, research, setup, validation
        +-- tools: schemas and board-facing handlers
        +-- services/adapters: board routing, pyOCD, serial, symbols
        |
        +-- FirmStore: durable evidence under .firm (never live authority)
```

The client chooses what to request. The server independently checks whether
the named operation is visible, planned, permitted, scoped to the live board,
safe for the current map, and still fresh. Discovery is guidance, not
authorization: hidden tools remain registered so a stale direct call reaches a
physical handler lock and receives the same prerequisite refusal.

## Layers and ownership

`server.py` is the composition root. It creates one process-local
`ServerRun`, `ConnectionManager`, `ToolRegistry`, `PlanEngine`,
`PermissionStore`, `GateManager`, safety policy, setup services, and board
adapters. Business rules live in their owning modules rather than in the
composition root.

Profiles in the selected project `.firm/boards/` root are the only normal-connection
source. The checkout ships no board-profile fallback. Fresh setup accepts an exact MCU
ordering code and local PDF without requiring a checked-in board record. It first replays
verified project support. If none exists, it issues a bounded research request
for one official CMSIS-Pack; the server quarantines and hashes the bytes, bounds
archive/XML parsing, proves the exact PDSC leaf, derives the pyOCD target, loads
only that pack, and performs a non-destructive live attach before promotion.
Agent strings and the project manifest are indices, not authority. Every later
load re-hashes the pack and datasheet and replays the exact binding. Validation
promotes only the exact live connection; no pseudo-connection stamp can satisfy
the readiness barrier.

The kernel provides the protocol and lifecycle boundary:

- `kernel/registry.py` filters dynamic tool discovery, sends
  `tools/list_changed`, rechecks handler locks, and routes every call through
  managed dispatch.
- `kernel/operations.py` assigns a finite timeout and operation identity,
  serializes one board while preserving cross-board concurrency, connects MCP
  cancellation to cooperative cancellation, and owns one idempotent cleanup
  path.
- `kernel/finalizers.py` accepts only the structured `uart_write` and
  `reset_and_run` finalizers on eligible serial tools. Finalizers are
  best-effort and run before mandatory cleanup.
- `kernel/processes.py` owns validated argv, finite subprocess bounds, process
  groups, and identity markers. `kernel/hygiene.py` performs bounded startup
  cleanup only when the live process identity still matches.

`ConnectionManager` is the only owner of live board handles. It enforces one
active connection per logical board and one logical board per stable
connection identity. Calls on one board serialize; different boards can run
concurrently. Disconnect clears only the named board's connection and
run-scoped authority.

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

The write gate is default closed. Only successful `board_validate` creates
live identity proof. The stamp records logical board, current connection, stable
probe identity, observed MCU evidence, validation run, and canonical map digest.
It is memory-only; disk artifacts, refresh, setup, plans, permissions, reports,
and tool visibility cannot create it.

Guarded dispatch applies the standard order before backend mutation:

1. require the registered handler to be unlocked;
2. verify any plan-bound artifact digest before scope, permission, preconditions,
   or budget consumption;
3. validate exact plan, board, run, session, parameters, and permission;
4. require live identity proof for guarded reads and a matching map digest for
   writes;
5. apply action-specific runtime containment;
6. decrement the plan/permission budget exactly once at execution start; and
7. call the bounded backend operation.

Raw and symbol memory checks cover the exact bytes accessed. UNKNOWN and
PROHIBITED spans fail closed. `safety/regions.py` uses authoritative non-empty
half-open ranges with prohibited precedence. `safety/linker.py` parses selected
ELF/HEX bytes for segments, entry, vector, executable evidence, and target/build
metadata; it never accepts caller-provided ranges. HEX bytes must agree with a
matching ELF companion. `safety/verify2.py` promotes only deterministically
reconciled device-support and official-document facts.

The sole persisted authority is each board's `memory_map.yaml`: schema v2 for
reviewed compatibility profiles and schema v3 for dynamically resolved support.
Semantic source digests cover the profile, replayed support bytes/binding,
captured datasheet evidence, deployment policy, and map-generator schema.
Ordinary build artifacts are not stable-map currentness inputs.
`board_safety_refresh` rederives the complete map from those server-owned
sources, can create the first map, and can update only the map association of an
existing same-connection identity proof. It cannot create live identity
authority.

The resulting action policy is:

- guarded address reads require a validated current connection;
- memory writes are fully contained in RAM;
- peripheral register writes exclude prohibited ranges;
- breakpoints require executable segments from the current plan-bound ELF;
- application and bootloader flash require explicit deployment authority plus
  target, segment, entry/vector, and erase-sector containment. A generic board may acquire or
  monotonically expand a server-derived application allocation under an approved artifact-bound
  plan and bounded sector-driver proof; existing bytes inside that envelope may be replaced without
  requiring a whole-device blank state; and
- target recovery uses a typed mechanism, complete disclosure, a fixed one-call
  plan, and fresh one-time permission, then clears live proof.

Symbol tools use either an explicit project `elf_artifact` or the ELF bound by a successful
application flash in the same Server Run. The binding is only a convenience: it is not persisted,
does not grant address authority, and implicit checkout firmware is never silently substituted
after restart. Explicit symbol-write ELFs are digest-bound by the accepted plan; every resolved
address still passes the stable memory-map containment check before target access.

Every refusal occurs before the corresponding backend mutation and names the
required remedy.

Normal connection is structurally separate from manual override. The visible
`connect(board_id)` schema and handler resolve only the named project profile,
disable legacy launch-environment probe/config fallbacks, and reject unknown
fields before backend dispatch. The hidden `connect_override` retains explicit
run-scoped probe, target, and external-config values behind its plan. Batch
children traverse the same strict FastMCP argument model, so batching cannot
reintroduce the removed public override channel.

## Setup and agent relay boundary

Setup deterministically inventories probes, serial ports, cache matches,
targets, builds, and exact verified pack bindings before requesting research.
Unknown facts are returned as strict research requests; blocked physical
conditions are not mislabeled as research. Candidate replies contain only an
official pack source record and cannot alter the exact user-supplied MCU part
number or choose the target, geometry, identity evidence, or partitions.

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
project `.firm` root, archive-bounded, exact-leaf checked, enumerated,
live-connected, and only then added through a serialized project-index update.
The exact validated payload is rebound before publication, and the checkout
pack registry is never a runtime write target. Successful attach
mode/frequency is a board fact discovered by a bounded generic fallback and is
reported and persisted rather than inherited from another board.

`get_setup_status` is the explicit pre-code barrier. It reports configuration,
live identity/map readiness, and UART attachment readiness separately. Native
build and artifact-collector guidance is advisory only. The normal deployment
flow is build, optional collection, populated flash plan, then flash; routine
build bytes do not enter stable-map currentness.

Safety authority is one strict `memory_map.yaml` per board. A schema-v3 generic
map stores resolved-support identity, semantic evidence digests, conservative
physical geometry, nullable partitions, and a closed deployment policy. Its
initial policy is `none`; the mere existence of physical flash never grants
deployment ownership. Separate pack RAM/ROM/flash ranges and optional SVD peripheral blocks are
retained without joining gaps. Exact or compatible live identity permits artifact-contained
application programming; bootloader/recovery authority remains separate. Status exposes both
identity capability and flash-planning readiness. A new or expanded generic allocation is persisted
before programming so a partial failure remains inside a durable owner. Schema-v2 reviewed application and bootloader partitions
exist only when an explicit reviewed partition policy authorizes them; the
historical full-flash ceiling is never reinterpreted as partition authority. Legacy source
manifest and safety report siblings are deleted during map load/commit and are
never read.

`board_safety_refresh` accepts only a board ID and rederives a complete candidate
from the profile and replayed server-owned evidence on every call. The
missing, malformed, and old-schema paths use the same derivation for compatibility maps. A
present but unreadable generic map is not replaced because it may contain one-way deployment
ownership that cannot be reconstructed safely. Refresh can
replace the map association of existing live identity proof, but cannot create
identity authority. An identity-anchor change closes the proof and requires
`board_validate`.

Validation connects through the selected probe, reads only replayed exact or
compatible identity evidence, associates the current map digest, and stamps
run-scoped gate state. When a pack exposes no safe identity proof it may prove
connection diagnostics, but cannot stamp the gate. It performs no UART capture
or firmware behavior assertion. Identity
proof is cleared by restart, disconnect, connection/probe change, identity
repair, and recovery, but not by reset, flash, UART work, or refresh. Silicon
mismatch guidance is neutral and an exact run-scoped allowance is required
before setup may create a new logical board/profile.

Flash and breakpoint plans bind selected artifact digests when populated plans
are accepted. Digest drift is rejected before permission, budget, containment,
or backend work. Flash containment then checks target, segments, entry, vector,
reviewed partition, and erase sectors; HEX also requires its matching ELF.
Breakpoint containment uses executable segments from the selected current ELF,
not blanket partition executability.

Setup and validation return structured control payloads for the agent plus an
`agent_prompt` written as ordinary prose. The agent must relay only that prose
and friendly choices, never structured payloads, continuation tokens, internal
field names, or machine identifiers unless a destructive approval explicitly
requires the exact live identity. See [agent-contract.md](agent-contract.md).

MCP setup and validation adapt inputs into the single
`setup_flow.validate.BoardValidator`; there is no parallel checkout-specific
validation implementation.

## Durable `.firm` artifacts

`FirmStore` is the single layout and low-level write owner:

```text
.firm/
  boards/       schema-v2 board profiles
  packs/        promoted support index and exact quarantined pack bytes
  evidence/     content-addressed captured datasheet bytes
  setup/        immutable setup attempts and append-only logs
  safety/       one schema-v2 or schema-v3 memory_map.yaml per board
  validation/   immutable validation and recovery attempts
  cache/        revocable host attachment hints
```

Writes are project-local, atomic, and checked for authority-bearing keys.
Profiles preserve the exact user-supplied MCU part number and Unicode display
name. The project pack manifest indexes exact immutable bytes and server-derived
bindings; every load replays those bytes rather than trusting manifest claims.
Profiles bind the resulting canonical support ID, not an agent path or target
proposal. Cache records contain only stable attachment hints.

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

Firmware builds remain native-project work. The server returns an exact parameterized invocation of
the provider-neutral `pyocd_debug_mcp.native_build` helper. The agent resolves the project's real
executable, argv, cwd, environment, and outputs; the helper executes that argv directly without a
shell, inherits network access by default, verifies ELF/HEX formats, and records whether the linker
map was explicit or uniquely discovered without claiming universal ELF/map
coherence. Any project-native output can be declared with a named path; understood ELF/HEX formats
receive structural checks and unknown formats are honestly reported as opaque nonempty files.
Outputs may be discovered under a caller-selected artifact root. Existing incremental and in-source
layouts and caller-adjustable timeouts are supported. Local
toolchains are preferred, acquisition is allowed when none is compatible, and best-effort offline
environment guards are explicit rather than an OS network-sandbox claim. The server does not infer
a provider, toolchain, SDK root, target, or output convention. The always-visible
`collect_build_artifacts` MCP tool then provides an optional build-system-neutral
handoff for explicit ELF, HEX, BIN, and linker-map outputs. It performs no build,
search, subprocess, download, or hardware access. Collection stages a canonical
`firmware.*` bundle and deterministic SHA-256 manifest outside `.firm`; the
manifest contains provenance but no allowed ranges, plans, permissions, or gate
state. Safety refresh never consumes build outputs. The flash plan binds the selected
artifact and runtime containment parses it immediately before execution.

All build systems use the same owned-process helper with exact agent-resolved argv, and their normal
output can enter through the same visible collector.

## Target and host de-biasing boundaries

The package ships no reviewed board identities, device evidence, geometry, or attach facts.
An operator may explicitly configure an external reviewed catalog, but the default empty state uses
generic onboarding, which derives exact target/core/physical memory/flash
algorithm facts from the verified PDSC leaf and records actual probe/attach
facts from the live setup transaction. Production setup code contains no
branches for a particular board name or device address. Missing identity,
peripheral, erase, deployment, or recovery facts remain missing; no MCU prefix
invents them. Capability-specific operations then refuse before backend access.

Serial association uses stable USB identity and generic metadata scoring first.
Optional vendor helpers may be selected from an explicitly configured external registry when
generic evidence stays ambiguous; their executables come from an explicit environment path or
`PATH`, never a compiled host installation path. Recovery exposes only `backend_mass_erase` or
`manual_only`, checks the connected backend capability before disclosure, and
retains the existing exact-map disclosure and fresh one-time approval boundary.

## Runtime contracts

The live MCP `tools/list` schemas, tool descriptions, and the plan definitions in
`guardrails/plan_defs.py` are the runtime contract. The generated
[plan-tool contract](plan-tool-contract.md) is the corresponding human-readable reference.

The server is supported operationally from the complete checkout. Wheels and
sdists prove metadata, entry points, and importability; they do not include the
board, pack, firmware, or evidence roots needed for board control.
