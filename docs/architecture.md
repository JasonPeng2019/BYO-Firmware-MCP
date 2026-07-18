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
        +-- safety: evidence, regions, fingerprints, containment
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

Schema-v2 profiles in the selected project `.firm/boards/` root are the primary
normal-connection source. The checkout `boards/` directory is a read-only
compatibility fallback. Fresh automatic setup is advertised only for catalog
entries with a pinned official datasheet, a distinct pinned device-support
document, and exact installed pyOCD target/SVD identities. It recomputes every
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

The write gate is default closed. Only a successful `board_validate` can stamp
it, using the logical board, live connection, hardware result, stable probe
identity, and current aggregate safety fingerprint. A stamp is in memory only.
Disk artifacts, safety refresh, setup, planning, permission, or tool discovery
cannot create one.

Guarded dispatch applies the standard order before starting a handler:

1. require the registered handler to be unlocked;
2. validate the exact plan, board, run, session, and permission;
3. resolve the named live connection and board lock;
4. require validation for guarded reads;
5. for writes, recompute source freshness and require a matching gate stamp;
6. apply the action-specific containment rule;
7. decrement the plan/permission budget exactly once at execution start; and
8. call the bounded backend operation.

Raw memory-read containment is part of step 6, so rejection occurs before a
planned call burns budget. The memory handler repeats the same check as a
defense at the backend boundary, and symbol reads check after resolving the
symbol but before target I/O. Checks cover only bytes actually read: scalar
width for scalar/symbol access and requested byte length for block access.
Mapped region kinds are readable; UNKNOWN and PROHIBITED are fail-closed.

`safety/regions.py` uses typed non-empty half-open ranges. Classification is
UNKNOWN unless authoritative regions fully contain the request, and any
prohibited overlap wins. `safety/linker.py` extracts build partitions,
loadable segments, entry point, vector table, and configuration. It never
accepts caller-provided allowed ranges. `safety/verify2.py` promotes only
deterministically reconciled device-support and official-document facts.

`safety/fingerprints.py` creates canonical per-source and aggregate SHA-256
fingerprints. Map setup and refresh re-evaluate conflicts and overlaps before
atomic promotion. Anchor changes require setup plus validation. Refreshable
artifact drift can update the fingerprint on an already-valid live stamp but
cannot open a closed gate.

The resulting action policy is:

- guarded address reads require a validated connection;
- memory writes are fully contained in RAM;
- peripheral register writes exclude prohibited ranges;
- breakpoints require build-derived executable space;
- application and bootloader flash require the exact target and fingerprinted
  artifact, with segment, entry/vector, partition, and erase-sector
  containment; and
- target recovery uses a typed vendor mechanism, a fixed one-call plan, exact
  live disclosure, and fresh one-time permission. Recovery leaves the gate
  closed until validation succeeds again.

Every refusal is before the corresponding backend mutation and names the
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
resulting ELF/map must still pass `board_safety_refresh` before application
flash.

Safety refresh compares canonical sub-fingerprints and returns a stable drift
classification plus exact public remedies. Application and bootloader build
inputs are symmetric, but authority is not: application builds must remain
inside the reviewed deployment ceiling, while bootloader builds may replace
only build-derived regions already contained by an authoritative bootloader
partition. A missing bootloader envelope is an honest terminal maintainer
blocker, never an invitation to supply ranges. Pack and official-evidence
sources remain coupled. Their migration path reloads current pinned assets and
runtime identity, reruns two-source verification, reproduces retained build
regions from content-addressed artifacts, and then atomically promotes the
coupled replacement. A failure writes a blocked report and leaves the old map
closed. Part/target, geometry, and schema changes require
full safety setup followed by validation; unclear profile scope requires full
safety setup. Safety responses do not expose internal continuation IDs because
no public safety tool consumes them; report records retain correlation IDs.

If a profile's board type lacks complete pinned reviewed evidence,
`board_safety_setup` returns `safety_setup_unsupported_board` with the reviewed
automatic board list and the maintainer evidence requirements. It does not
advertise a dead research continuation and never opens a gate.

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
  safety/       maps, source manifests, fingerprints, safety reports
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
handoff for explicit ELF, HEX, BIN, and linker-map outputs. It performs no build,
search, subprocess, download, or hardware access. Collection stages a canonical
`firmware.*` bundle and deterministic SHA-256 manifest outside `.firm`; the
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
