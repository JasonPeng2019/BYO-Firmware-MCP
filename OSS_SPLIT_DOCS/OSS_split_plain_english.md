# The OSS Split in Plain English

This project will become two products.

1. **Open product: `pyocd-debug-mcp` / `pyocd_debug_mcp`**

   This is a complete local hardware toolkit. A developer can use it with an
   MCP client, Python, a CLI, or scripts to work with explicitly selected
   hardware.

2. **Commercial product: `firmcli-sentry` / `firmcli_sentry`**

   This is the system that remembers hardware, applies safety policy, gets
   approvals, coordinates people and agents, and runs managed workflows.

The commercial product may use the open product. The open product must never
import, require, discover, or change behavior because of the commercial
product.

In this guide, **HAL** means the open hardware-access layer: the code that
talks to probes and boards. A **worker** is a separate process used to keep a
provider failure from taking down the main process.

```text
firmcli_sentry  -->  pyocd_debug_mcp  -->  probes, boards, UART, hardware
```

## The simple rule

Put direct physical operations in OSS. Put decisions, persistent knowledge,
and shared-operation management in the commercial product.

For example, OSS can flash a specific firmware file to a specific board. The
commercial product decides whether that file is approved for that board, who
may run it, whether a plan is current, and records the result.

## What goes into the open product

The open product must work without an account, license, private package,
backend, or `.firm` directory. It may keep temporary process state and use
files the caller explicitly selects, but it must not create or manage durable
organizational state.

### Hardware access and development work

- Find local probes, serial ports, and explicitly supplied remote endpoints.
- Connect to and disconnect from explicitly configured boards.
- Inspect target identity, cores, memory layout, erase geometry, and device
  support data.
- Halt, resume, step, reset, and manage breakpoints.
- Read and write registers and memory using mechanical checks such as width,
  alignment, address shape, and backend capability.
- Flash an explicitly supplied firmware artifact and perform physical
  verification.
- Read, write, and exchange UART data.
- Run a caller-specified native build command and collect caller-selected
  artifacts.
- Inspect ELF/HEX/BIN files, symbols, CMSIS packs, PDSC/SVD data, and pack
  bytes.
- Run an explicit recovery operation only after the caller confirms it is
  destructive.
- Run a simple, one-board batch of direct operations.

### Extensibility and reliability mechanics

- SWD, JTAG, UART, and future transport adapters.
- Provider and discovery-hook contracts so users can add support for new
  probes, buses, instruments, or discovery methods.
- Explicit remote-endpoint connection support.
- Timeouts, cancellation, per-target locking, process isolation, and cleanup.
- Public typed requests, results, errors, board configuration, identity, and
  physical-geometry contracts.

### Open MCP tools

The open MCP server is static: it always exposes the same direct tools. These
include discovery, onboarding, connect/disconnect, target inspection, debug
control, register and memory operations, flash, UART, recovery, simple batch,
native build, and artifact collection. The exact names and schemas are in
`OSS_split_implementation_map.md`, section 4.

## What stays in the commercial product

The commercial product owns everything that decides, remembers, coordinates,
or provides managed organizational assurance.

### Safety, permissions, and managed workflows

- Safety maps and protected partitions.
- Policy evaluation for flash, memory writes, register writes, recovery, and
  future physical actions.
- Plans, gates, freshness rules, budgets, dynamic tool access, and approvals.
- User roles, permissions, and one-time or session grants.
- Managed setup, setup repair, validation, and reviewed device evidence.
- High-level recovery decisions and post-recovery workflow.

### Persistent knowledge and audit

- FirmStore: saved board profiles, pack bindings, evidence, reports, safety
  maps, setup attempts, validation results, and other managed records.
- Approved-artifact management and deployment provenance.
- Issue monitoring, Sentry delivery, check-ins, audit logs, retention, and
  reliability history.
- Persistent board inventory, board-to-probe assignment, remote endpoint
  registry, health, and lifecycle information.

### Shared hardware operations

- Board pools, reservations, leases, queues, scheduling, quotas, and
  contention handling.
- Multi-user and multi-agent coordination.
- Multi-board, multi-rig, and multi-instrument workflows.
- Remote-lab management, fleet maintenance, CI/HIL orchestration, and
  organization-wide physical state and history.

## What the enterprise side already has today

The enterprise behavior is not an empty placeholder. A substantial part of it
is already implemented and tested in the current server. The important
qualification is that it is still mixed into the `pyocd_debug_mcp` package;
the separate `firmcli_sentry` package described above has not been created
yet.

| Area | What is already implemented | Current location |
|---|---|---|
| Durable FirmStore | `.firm` layout, board profiles, setup and validation paths, safety files, pack files/manifests, datasheet evidence, reports, caches, atomic writes, and append-only JSON/YAML helpers | `src/pyocd_debug_mcp/firmstore/` |
| Plans and guardrails | Plan definitions, parameter validation, plan rendering, permission modes, gates, validation stamps, identity checks, mismatch handling, and guarded dispatch support | `src/pyocd_debug_mcp/guardrails/` and `kernel/registry.py` |
| Flash/recovery policy | Artifact identity and hashing, guarded flash-request resolution, recovery-request authorization, refusal reasons, and safety-related preconditions | `guardrails/flash_gate.py`, `guardrails/recover_gate.py` |
| Safety evidence and regions | Hardware-evidence parsing, source reconciliation, memory-region construction, linker/map support, enforcement, refresh, and verification helpers | `src/pyocd_debug_mcp/safety/` |
| Managed setup and validation | Board catalog, target research, CMSIS-pack/device support, datasheet evidence, preflight, setup attempts, reviewed evidence, validation, and target/profile handling | `src/pyocd_debug_mcp/setup_flow/` |
| Monitoring and audit | Per-run counters, 100-call snapshots, 500-call check-ins, hash-linked ledger segments, issue classification, redaction, narrative reports, Sentry-shaped transport, ACK/delete behavior, staleness blocking, and closeout | `src/pyocd_debug_mcp/monitor/` |
| Inventory and endpoint management | Probe/UART discovery and merging, selection records, active connections, vendor rows, remote endpoint normalization/reachability, and persistent remote-probe operations | `hardware_inventory.py`, `probe_inventory.py`, `remote_probes.py`, and `tools/remote_probes.py` |
| Dynamic commercial server behavior | Tool registration, tool visibility/unlock state, guarded dispatch, operation-resource binding, finalizer hooks, monitor hooks, and stdio server composition | `src/pyocd_debug_mcp/kernel/registry.py` and `server.py` |

As a rough size indicator, these candidate enterprise areas contain about
20,800 lines of Python today. That number is not a completion percentage: some
code is shared, some code still needs extraction, and some planned enterprise
features (especially multi-user coordination and fleet services) are not yet
present.

The current implementation has dedicated tests for monitoring, ledger and
delivery, guardrails and trust-model behavior, setup/validation, inventory,
remote endpoints, and the change loop. The preserved clean verification run
recorded **477 tests passed and 3 skipped**, with Ruff and Pyright clean. The
evidence is in `sentry-evidence/results/`.

## What is not yet an enterprise product

These pieces still need to be built or separated before we can say the closed
side is a standalone product:

- A real `firmcli_sentry` package and installable commercial distribution.
- A private `hal_client` that calls the open HAL through public contracts.
- A production isolated HAL worker for each claimed board.
- Removal of private imports and private behavior from the open package.
- A completed static open server that works without the commercial code.
- Complete multi-user ownership, board leases, pools, scheduling, fleet health,
  multi-board transactions, remote-lab management, and CI/HIL orchestration.

In short: the enterprise side is **substantially implemented as a set of
server capabilities, but not yet separated into the closed product boundary**.
The next work is packaging and boundary extraction, followed by the greenfield
shared-resource features listed above.

## Features that must be split in two

Some current features contain both a direct hardware mechanism and commercial
decision-making. Do not duplicate the whole feature in both products. Extract
the direct mechanism into OSS and move the rest to the commercial package.

| Current area | OSS part | Commercial part |
|---|---|---|
| Flashing | Validate an explicit artifact, program it, and verify the physical write | Decide artifact role, allowed regions, approval, assignment, and policy |
| Memory/register writes | Perform a mechanically valid explicit write | Enforce prohibited ranges, plans, and organizational rules |
| Recovery | Validate the explicit recovery request and destructive confirmation | Decide whether recovery is allowed and what workflow follows |
| Board configuration | Open `BoardConfig` schema and caller-supplied config files | Automatic storage, reconciliation, lifecycle, and managed profiles |
| Pack support | Download/validate/store bytes in an explicitly chosen root | FirmStore admission, managed retention, and evidence records |
| Discovery | Local discovery, explicit hooks, and explicit endpoints | Durable registry, assignments, ownership, and managed remote endpoints |
| Sessions and operations | Connections, cancellation, locks, timeouts, and cleanup | Plans, gates, dynamic visibility, finalizers, and authority state |
| Setup/onboarding | Stateless target inspection and portable configuration output | Managed setup, continuation, repair, validation, and saved knowledge |
| Serial I/O | Direct UART read/write/exchange | Workflow/event meaning, policy, and durable evidence |
| Monitoring | Basic direct-call errors and operation results | Monitor, ledger, classification, reports, delivery, audit, and check-ins |

## How to do the split

Do the work in this order.

1. Create the open contracts first: board configuration, identity, geometry,
   requests, results, errors, policy-decision schemas, and provider interfaces.
2. Extract the open runtime and direct hardware services behind those contracts.
   Keep them usable from Python before adding MCP wrappers.
3. Prove the open product can independently complete a real loop: discover,
   configure, connect, build, collect an artifact, flash, debug, use UART,
   recover, and disconnect.
4. Build a static open MCP server that only translates tool input into open
   service calls. It must have no plans, dynamic tools, FirmStore, monitor, or
   commercial finalizers.
5. Create the private `firmcli_sentry` package. Move FirmStore, policy,
   guardrails, managed inventory, setup/validation, and monitoring into it.
6. Make the commercial product call the open HAL through public contracts. In
   production, it supervises an isolated open HAL worker for each claimed
   board; it does not import pyOCD adapter internals directly.
7. Remove old mixed-package paths and split tests so each product tests only
   its own implementation and dependencies.

## The implementation shape

The split is a package split, not just a licensing decision. The open package
should end up with these small, understandable areas:

```text
pyocd_debug_mcp/
  contracts/      public requests, results, errors, board config, geometry
  adapters/       SWD/JTAG/UART providers and provider workers
  runtime/        sessions, operations, cancellation, timeouts, cleanup
  discovery/      probes, UARTs, hooks, explicit remote endpoints
  artifacts/      native builds, ELF/HEX/BIN inspection, artifact collection
  packs/          pack bytes, validation, PDSC/SVD parsing, index repair
  onboarding/     stateless target inspection and config generation
  services/       connect, target control, memory, registers, flash, UART
  mcp_tools/      fixed MCP wrappers over the services
  server.py       static MCP server composition
```

The private package should own the managed product areas:

```text
firmcli_sentry/
  hal_client/     calls the public HAL and supervises its worker process
  firmstore/      durable profiles, artifacts, reports, and evidence
  policy/         safety maps, permissions, and allow/refuse decisions
  guardrails/     plans, gates, approvals, budgets, and authority
  inventory/      persistent boards, assignments, endpoints, and health
  setup/          managed setup, repair, validation, and reviewed evidence
  monitor/        monitoring, issue reports, delivery, and audit history
  mcp_tools/      commercial workflow and guarded tools
  server.py       dynamic commercial server composition
```

These names describe ownership. During migration, move code into the nearest
area instead of leaving a private feature hidden inside a shared open module.

The important current-source moves are:

- `src/pyocd_debug_mcp/firmstore/` becomes private `firmcli_sentry/firmstore/`.
- `src/pyocd_debug_mcp/monitor/` becomes private `firmcli_sentry/monitor/`.
- `src/pyocd_debug_mcp/guardrails/` becomes private policy/guardrail code,
  except for the direct request checks extracted into open contracts and
  services.
- Commercial parts of `src/pyocd_debug_mcp/safety/`, setup flow, inventory,
  remote-probe storage, and the dynamic registry move to the private package.
- Adapter code, direct target operations, discovery mechanics, artifact
  handling, pack parsing, and process cleanup move into the open package areas
  shown above.
- The old mixed `src/pyocd_debug_mcp/server.py` is replaced by a static open
  server. The existing guarded composition is retained privately.

Do not solve the split by copying a whole old module into both packages. Move
each function and type to one owner, then make the other side call it through a
public contract.

## Meaning of the migration labels

The detailed map uses four labels:

- **OSS:** leave it in `pyocd_debug_mcp`, after removing private imports.
- **CLOSED:** move it to `firmcli_sentry`; it must not be in the open wheel.
- **SPLIT:** extract the direct hardware portion to OSS and move the policy or
  persistence portion to private code.
- **REPLACE:** do not carry over the current wrapper; write a new open wrapper
  over the public open services and keep the guarded wrapper private.

## How to split a mixed module

Several existing modules contain both kinds of code. Split their contents by
responsibility:

| Existing area | Keep in OSS | Move to private package |
|---|---|---|
| `guardrails/flash_gate.py` | File checks, artifact parsing, and physical compatibility checks | Artifact role, allowed ranges, assignment, approval, and refusal policy |
| `guardrails/recover_gate.py` | Recovery request shape and destructive confirmation check | Permission, approval, assignment, and recovery policy |
| `pack_provision.py` | Download, hash, validate, and store pack bytes under an explicit caller path | FirmStore defaults, admission records, and managed retention |
| `hardware_inventory.py` | Current probe/UART facts, merging, deduplication, and run-scoped selection | Durable assignments, validation views, ownership, and lifecycle |
| `remote_probes.py` | Endpoint type, host/port normalization, and reachability check | Save/load/upsert/remove and the persistent endpoint registry |
| `kernel/operations.py` | Cancellation, operation lifetime, per-board locks, timeouts, and cleanup | Tool policy, finalizers, plans, gates, and authority state |
| `services` and setup flow | Direct hardware mechanics and stateless target inspection | Managed setup, validation truth, continuation, and saved evidence |
| `monitor/` | Nothing in the open server | The entire monitor, ledger, reports, delivery, and check-in system |

The rule is: an OSS function may answer “can this direct hardware request be
performed mechanically?” It must not answer “is this allowed for this user,
board, artifact, plan, or organization?”

## The open server must be static

The open `server.py` should register a fixed list of direct tools. It should
not import the current dynamic registry or guarded server composition.

The open tool wrappers should do only three things:

1. Parse and validate the public request.
2. Call one open service.
3. Return a typed result or a direct hardware error.

The open server must not create plan tools, hide or reveal tools based on
state, inspect FirmStore, start the monitor, issue approvals, or run private
finalizers.

The commercial `server.py` keeps those behaviors. Its guarded tool calls first
ask the private policy/guardrail code for an allow decision, then call the HAL
through `hal_client`. The commercial code should not import
`pyocd_debug_mcp.adapters.*` directly.

## Public boundary between the products

Before moving implementation code, define and freeze these public objects in
`pyocd_debug_mcp.contracts`:

- `BoardConfig`: explicit target, probe, UART, reset, clock, pack, and geometry
  settings; no owner, assignment, approval, or FirmStore ID.
- `PhysicalIdentity`: normalized facts used to ensure the connected target is
  the target the caller named.
- `AddressRange`, `EraseSector`, and `PhysicalGeometry`: physical facts only.
- Request/result types for connect, target control, memory, registers,
  breakpoints, flash, UART, discovery, build, artifacts, and recovery.
- Typed errors for bad requests, unsupported hardware, transport failures, and
  timeouts.
- `PolicyEvaluationRequest` and `PolicyDecision`: public data shapes that let
  the private product provide or consume a decision without exposing its
  evaluator.

Every public service must be callable from Python without MCP. MCP is only the
thin translation layer above those services.

## State and storage rules

Open code may keep:

- live connections and process-local operation state;
- cancellation tokens, locks, retries, and cleanup handles;
- caller-supplied board configuration and endpoint files;
- caller-selected build/artifact directories; and
- a caller-selected pack-byte cache.

Open code must not create `.firm` or persist profiles, assignments, approved
artifacts, safety maps, validation truth, plans, approvals, monitor state,
reports, or ownership. If state must survive a process or be shared with
another user or agent, it belongs in `firmcli_sentry`.

## Minimum migration tests

Add boundary tests while moving code, not after the move:

1. Install only the open package in a clean environment.
2. Confirm it exposes exactly the static open tool list.
3. Confirm its import graph contains no `firmcli_sentry`, FirmStore,
   guardrails, monitor, or `sentry_sdk` dependency.
4. Run a fake-provider loop: discover, onboard explicit config, connect, build,
   collect, flash, verify, debug, use UART, recover, and disconnect.
5. Confirm the loop creates no `.firm` directory and writes only to caller-
   selected paths.
6. Run the private compatibility tests and confirm every guarded write, flash,
   or recovery operation receives a private allow decision before the HAL call.

For the detailed file destinations and current test-by-test split, use
`OSS_split_implementation_map.md` sections 6 and 11. This guide gives the
reason and shape of the change; that map gives the exact inventory.

## Checks that prove the split is real

- The open wheel contains no FirmStore, guardrails, monitor, plans, approvals,
  policy safety maps, or `sentry-sdk`.
- Open code has no import of `firmcli_sentry` or private namespaces.
- The open product installs and starts in an empty environment without the
  private package.
- The open MCP server exposes only its fixed direct-tool surface.
- Open operations do not create `.firm` or durable organizational records.
- The commercial package uses public HAL contracts and does not reach directly
  into open adapter internals.
- The commercial compatibility suite continues to prove that each guarded
  physical action has a private allow decision before it reaches the HAL.

## Where to find the detailed instructions

- `OSS_structure.md` explains why each category is open or commercial.
- `OSS_split_implementation_map.md` is the binding implementation plan. It
  lists the exact file moves, tool disposition, test split, package layout,
  and acceptance checks.

When this guide and the implementation map differ, follow the implementation
map.
