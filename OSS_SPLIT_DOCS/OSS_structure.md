# OSS / Closed-Source Split — Current MCP Server Structure

This document defines the intended feature split for
`MCP_Server/BYO-Firmware-MCP`.

The broader company and product rationale is documented in
[firmware_hal_oss_strategy_consolidated.md](firmware_hal_oss_strategy_consolidated.md).
This document applies that strategy to the current MCP server. If the broader
strategy uses a heuristic or an illustrative example, the concrete feature
placements in this document control for this codebase.

The normative file-, function-, tool-, and test-level migration specification
is [OSS_split_implementation_map.md](OSS_split_implementation_map.md). That map
implements the strategy here and is the handoff document for performing the
actual product separation.

The goal is not to maximize the number of open features or the number of paid
features. The goal is to place each feature where it creates the most total
strategic value.

The OSS project must be genuinely useful by itself. An individual engineer
must be able to pair it with Claude, Codex, Cursor, a custom agent, or ordinary
scripts and successfully build, flash, debug, inspect, and iterate on real
firmware without the commercial product.

That independence is a minimum capability guarantee, not a promise of feature
parity. The commercial product should be materially better where its features
create more value for the profit model.

The company thesis remains:

> **Commoditize agent-to-hardware access. Own the system that makes autonomous
> hardware operation shared, persistent, coordinated, safe, and reliable.**

Shorter:

> **Open physical I/O. Own the physical infrastructure OS.**

## The governing judgment

There is no mechanical rule that decides every feature.

For each feature, consider all relevant OSS and commercial value, then place
the feature on the side where it creates the greater total strategic benefit.

The familiar single-target versus shared-system distinction is a useful signal,
not a rule. A single-user or locally deployed feature may remain closed when it
has much greater commercial value and customers can control it through public
inputs and outputs. A feature used by teams may still be OSS when source-level
extension is essential to physical compatibility or when opening it creates
exceptional substrate value.

### Factors that favor OSS

- The OSS runtime cannot complete a real individual firmware loop without it.
- It creates exceptional value for individual developers.
- It materially improves adoption, trust, standardization, or ecosystem pull.
- It helps make the OSS HAL the default substrate instead of another thin
  wrapper over pyOCD, OpenOCD, or vendor tools.
- Supporting real customer hardware frequently requires source-level edits.
- Hardware vendors and users are likely to extend or replace it.
- Its paid value or willingness-to-pay is limited.
- Keeping it closed would make the OSS project too weak to establish a moat
  against low-hanging competing implementations.

### Factors that favor closed source

- It creates strong willingness-to-pay or is central to the profit model.
- Its value grows sharply with more agents, users, boards, rigs, workflows, or
  elapsed time.
- Opening it would substitute for a substantial part of the commercial product.
- Customers can adapt it sufficiently through configuration, schemas, callbacks,
  plugins, or a backend API without changing its implementation.
- It is primarily use-only machinery rather than a physical compatibility point.
- It is much more valuable to teams and businesses than to an individual
  developer.
- It compounds proprietary knowledge, policy, coordination, history, or
  operational responsibility.
- The company can make it leagues better as an integrated paid system than it
  would be as a disconnected OSS feature.

No single factor is automatically decisive. A feature can provide good OSS
value and still remain closed when its commercial value is much greater. A
feature can provide good commercial value and still be OSS when it is required
for independence, creates exceptional ecosystem value, or is not a durable
profit center.

For this repository, the tables below are decisions, not examples. Future
changes should use the same judgment, but an implementer performing the current
split must follow the normative implementation map rather than reopening the
placement decisions.

## Public interfaces do not require open implementations

Customization is not the same as source access.

A customer may need to control what a feature does without needing to edit how
the feature is implemented. A closed engine may accept:

- customer-authored configuration;
- public schemas and policy documents;
- plugins at a deliberately exposed boundary;
- calls to a customer-controlled backend API;
- customer-provided allow/refuse decisions; or
- documented inputs and outputs that another system can generate or consume.

In those cases the contract can be public while the machinery remains closed.

This is especially important for safety, policy, persistence, scheduling,
approvals, audit, and workflow execution. Customers must be able to describe
their hardware and desired behavior. They do not need the source code for the
engine that validates, coordinates, remembers, or enforces it.

Source-level customization remains a strong reason to choose OSS when it is
genuinely needed for physical compatibility—for example, a new probe, bus,
instrument, or discovery provider—but it is one factor in the full judgment,
not a substitute for that judgment.

## Local deployment does not imply OSS

The problem being solved determines the layer, not where the software runs.

A whole-lab coordinator running entirely on one workstation can still be a
commercial feature. If it owns inventory, scheduling, leases, shared state,
multi-board workflows, or agent coordination, it is solving the physical
infrastructure OS problem even when there is no cloud service.

Likewise, an OSS transport may reach a remote probe without becoming a remote
lab control plane. Transport is a mechanism. Managed inventory, authority,
health, routing, sharing, and coordination are the product.

## Product shape

```text
ANY AGENT OR SCRIPT
        |
        +----> OSS HAL API / CLI / MCP
        |          |
        |          +----> explicit local hardware operations
        |          +----> one or more caller-coordinated targets
        |
        +----> CLOSED PRODUCT API / MCP / UI
                   |
                   +----> policy, knowledge, plans, approvals, audit
                   +----> inventory, scheduling, topology, workflows
                   +----> drives one or more OSS HAL instances
                                  |
                                  +----> physical hardware
```

The OSS core should be a stable hardware library. Its CLI and MCP server are
adapters over that library. The commercial product may expose MCP, HTTP, a UI,
or other interfaces; it does not need to be internally defined by MCP.

The dependency direction is one-way:

```text
CLOSED PRODUCT ---> OSS HAL ---> HARDWARE
```

The OSS HAL must never import the commercial product. The commercial product
must operate hardware through public HAL contracts rather than reaching around
them into pyOCD- or probe-specific internals.

## OSS — independently useful physical HAL

### Physical compatibility and extension features

| Feature | OSS scope | Why OSS |
|---|---|---|
| Probe and transport adapters | SWD/JTAG/UART backends today; CAN, power, instruments, and other transports over time | Physical compatibility often requires real code changes; the commercial layer must remain hardware-agnostic |
| Adapter and capability contracts | Stable interfaces, typed capabilities, operation results, deadlines, cancellation, and normalized errors | Required for third-party integrations and for the closed product to drive any compliant HAL |
| Discovery-provider hooks | Mechanism for adding new ways to enumerate probes, serial ports, instruments, and buses | Machine- and vendor-specific discovery sometimes requires customer-authored code |
| Device-support parsing | Load and validate CMSIS packs and equivalent hardware metadata | Necessary to support real targets without a proprietary device catalog |
| Minimal pack-byte store | Content-addressed storage of exact verified device-support bytes | Low standalone friction, high ecosystem value, and little independent profit value |

### Standalone hardware-development features

| Feature | OSS scope | Boundary |
|---|---|---|
| Discovery and enumeration | Find local probes, ports, targets, and explicitly supplied remote endpoints | No persistent shared inventory, ownership, health management, or automatic organizational assignment |
| Explicit selection and connection | Select a concrete endpoint, connect, disconnect, and inspect current attachment facts | No firmstore-backed automatic board memory or persistent asset identity |
| Local connection mechanics | Hold one or more live connections, serialize per target, and isolate provider failures | The caller coordinates targets; the HAL provides no pools, leases, schedules, or multi-target workflow semantics |
| Target capability and geometry inspection | Report target identity, cores, physical memory geometry, erase geometry, algorithms, and peripheral facts derived from current hardware or verified support data | Reports facts; does not decide organizational permission or deployment authority |
| Debug control | Halt, resume, step, reset, execution state, and breakpoints | Raw target operation with mechanical validation only |
| Registers | Read and write CPU and peripheral registers | No commercial policy decision about whether a particular register is appropriate for this asset or workflow |
| Memory | Read and write explicit addresses with width, alignment, backend, and request-bound checks | No proprietary allowed/prohibited-region evaluation |
| Flash primitive | Program explicit artifact bytes, honor backend/algorithm constraints, contain the requested transaction mechanically, and verify the write | Does not decide whether this artifact, partition, user, asset, or moment is allowed |
| Serial and bus primitives | UART read/write/exchange and future CAN or other bus operations | No shared test orchestration or organization-wide actuator policy |
| Symbols and artifact parsing | ELF symbols, segments, HEX/BIN handling, digests, and structural validation | No organizational artifact approval or provenance database |
| Native build invocation | Run exact caller-resolved build commands and collect declared artifacts | No managed toolchain fleet, reproducibility service, or organization-wide build provenance |
| Stateless onboarding | Discover an unknown target, validate supplied support data, and return portable board/profile facts | The OSS user explicitly saves and supplies configuration; automatic durable knowledge belongs to firmstore |
| Caller-managed board configuration | Public schema and explicit profile-file input | The format is open; automatic persistence, reconciliation, and lifecycle management are closed |
| Recovery primitive | Typed, bounded backend recovery or mass erase with explicit destructive invocation | No policy engine, approval service, or high-level recovery decision-making |
| Single-target batch | Sequence operations for one target and stop on the first failure | No cross-target transaction, synchronization, rollback, or aggregate postcondition |
| Operation preview and request validation | Describe the exact primitive request, validate its shape, and disclose mechanical effects | Not an authorization, plan budget, permission, dynamic unlock, or organizational safety decision |
| Execution discipline | Timeouts, cancellation, per-target serialization, owned subprocesses, bounded cleanup, and failure isolation | Generic correctness rather than organizational coordination |
| Remote endpoint transport | Connect to an explicitly supplied supported remote probe endpoint | No durable endpoint registry, credentials service, health routing, fleet discovery, or maintenance scheduler |
| Basic diagnostics | Typed errors, operation results, timing, and cleanup outcomes needed to understand a direct call | No durable audit ledger, report lifecycle, remote delivery, or cross-session organizational history |
| Programmatic API, CLI, and thin MCP server | A stable, usable surface for scripts and arbitrary agents | Functional and documented, but not the company's full optimized agent-execution experience |

### OSS independence floor

Without any commercial import, license, credential, backend, or service, an
engineer must be able to:

1. discover or explicitly name supported hardware;
2. supply or derive enough target metadata to connect;
3. invoke the project's real native build and collect its artifacts;
4. program and verify application firmware;
5. halt, resume, reset, step, and use breakpoints;
6. inspect registers, memory, execution state, and symbols;
7. exchange serial or other supported local I/O;
8. invoke a disclosed recovery primitive;
9. use the runtime through its library, CLI, or MCP surface; and
10. add a new physical adapter through the documented HAL contract.

The OSS experience may require explicit profile paths, endpoint addresses,
operation parameters, and destructive acknowledgements. It does not need the
commercial product's automatic memory, policy decisions, approvals, planning,
coordination, or polished guidance.

## Closed source — commercial physical infrastructure OS

### Retained or extracted from the current codebase

| Feature | Closed scope | Why closed |
|---|---|---|
| Firmstore / local board knowledge | Persist board profiles, exact pack bindings, evidence, safety maps, attachment hints, setup attempts, validation attempts, and managed lifecycle | Customers can supply facts through public inputs; the storage, reconciliation, replay, and knowledge experience are valuable use-only machinery |
| Safety-policy engine | Decide what may be read, written, flashed, erased, recovered, or actuated from customer rules, asset facts, current state, and product policy | Very high product and B2B value; customers can dictate behavior through public policy inputs or a backend API without editing the engine |
| Reviewed partition authority | Establish protected bootloader, calibration, recovery, application, and prohibited regions for a particular asset or revision | The valuable feature is trusted authority and enforcement, not the raw address-writing mechanism |
| Plan and gate engine | Bind exact actions and artifacts, manage preconditions, budgets, freshness, validation gates, and tool unlocks | High-value use-only machinery that makes autonomous execution safer and more reliable |
| Approvals and permissions | Human approval, one-time or session grants, roles, and organizational policy | A core paid safety and governance feature |
| Polished agent-execution experience | Plan tutorials, optimized MCP semantics, dynamic tool visibility, unlock payloads, observation shaping, recovery guidance, and workflow-aware responses | Improves OSS if opened, but creates greater differentiation and profit value inside the product |
| Monitor and audit system | Ledger, issue detection, reports, delivery, retention behavior, staleness enforcement, and later organizational audit | Compounds operational history and accountability; basic direct-call diagnostics remain OSS |
| Persistent hardware inventory and assignment | Remember boards, endpoints, attachment relationships, selection history, and managed identity | More valuable as persistent product knowledge than as individual raw access |
| Managed remote endpoints | Registry, credentials, health, routing, maintenance, failover, and remote process lifecycle | Explicit transport stays open; making remote hardware dependable and managed is commercial infrastructure |
| High-level recovery decisions | Decide when recovery is appropriate, what state must be preserved, and what postconditions or escalation are required | The raw recovery mechanism is open; responsibility for the outcome is paid |
| Organizational build and artifact control | Approved artifacts, commit-to-binary relationships, reproducible environment policy, deployment provenance, and retention | Strong B2B traceability and reliability value |

### Greenfield commercial control plane

| Feature | Closed scope |
|---|---|
| Resource ownership | Users, agents, CI jobs, roles, claims, reservations, and leases |
| Pools and scheduling | Shared board pools, queues, priorities, quotas, and contention handling |
| Persistent physical state | Hardware revision, current/previous firmware, bootloader, calibration, configuration, health, and last-known-good state |
| Physical topology | Boards, probes, buses, power, instruments, fixtures, rigs, and dependency relationships |
| Multi-board and multi-resource workflows | Coordinated execution across targets and instruments |
| Physical transactions | Atomic acquisition, preconditions, synchronized actions, postconditions, rollback, recovery, and final-state ownership |
| Multi-agent coordination | Prevent collisions and coordinate concurrent autonomous actors |
| Remote labs and maintenance | Distributed HAL lifecycle, lab-machine management, upgrades, observability, and support |
| CI/HIL orchestration | Repeatable organizational workflows over shared scarce physical resources |
| Fleet provenance and history | Durable answers to who changed what, with which artifact, under which physical configuration, and with what result |

These features remain commercial even when they run entirely on a customer's
local workstation or self-hosted network.

## Important feature boundaries

### Flash and safety

```text
OSS HAL
  receives an explicit artifact and target operation
  validates the request and backend mechanics
  programs the requested bytes
  verifies the physical write

CLOSED PRODUCT
  knows the board and organizational state
  obtains customer policy through configuration or an API
  decides what may be flashed and where
  binds the artifact and policy decision
  obtains approval when required
  calls the HAL only after authorization
  records the organizational result
```

The policy input and decision contract should be documented. The evaluator,
authority model, and enforcement orchestration remain closed.

The same split applies to memory writes, register access, recovery, power,
instruments, and future actuation: the HAL performs explicit physical
operations correctly; the product decides whether an operation is appropriate.

### Board configuration and firmstore

- **OSS:** a public board/profile schema, explicit caller-supplied files, current
  hardware facts, and portable onboarding output.
- **Closed:** automatically remember, reconcile, replay, update, validate, and
  use board knowledge over time.

The OSS user may save the onboarding output and pass it back explicitly. The
commercial product makes that state managed and invisible.

### Agent experience

- **OSS:** stable callable primitives, a usable thin MCP surface, documented
  schemas, typed errors, and enough guidance to complete real single-target work.
- **Closed:** the best planning, gating, dynamic exposure, observation shaping,
  recovery strategy, context efficiency, and workflow-aware experience.

The OSS interface must work. It does not need to match the paid experience.

### Multi-board operation

The OSS runtime may hold multiple explicitly addressed live connections because
connection multiplexing and per-target isolation are generic HAL mechanics.
The caller remains the coordinator.

No OSS operation may treat several boards or resources as one managed unit. The
OSS layer has no pool, lease, schedule, cross-target precondition, synchronized
workflow, rollback, or aggregate postcondition.

The commercial product may choose to run one HAL process per board for process
isolation even though the OSS runtime supports multiple addressed connections.
That is a deployment choice, not the licensing boundary.

### Remote operation

- **OSS:** connect to an explicit supported endpoint using an open transport.
- **Closed:** discover, remember, authenticate, monitor, route, allocate,
  maintain, and coordinate remote endpoints as a lab.

## Split by feature, not by module

The licensing boundary is defined by features and responsibilities, not by the
current Python file layout.

If a module contains both OSS and closed features, split or rewrite it. Examples
include:

- raw memory/flash operations versus policy and plan enforcement;
- device geometry facts versus safety authority;
- discovery execution versus persistent inventory and assignment;
- pack-byte storage versus managed board knowledge;
- direct-call diagnostics versus the monitor and audit system; and
- thin MCP tools versus the polished commercial agent contract.

Code entanglement is allowed to change the feature decision only when the total
cost and continuing maintenance burden of separation are genuinely greater than
the strategic loss from putting the combined feature on one side.

When making that exception:

1. estimate the one-time split cost;
2. estimate the continuing duplicated-code and compatibility cost;
3. estimate the OSS adoption gained or lost;
4. estimate the commercial differentiation gained or lost;
5. choose the side with the better total outcome; and
6. record that the placement is a cost-driven compromise rather than the ideal
   conceptual boundary.

“The code is already together” is not sufficient. The exception is justified
only when the pain is large enough to outweigh a knowingly suboptimal product
split.

## Current codebase surgery map

| Feature separation | Likely pain | Direction |
|---|---|---|
| Build invocation and artifact collection | Trivial/easy | OSS |
| Probe and serial enumeration | Trivial/easy | OSS |
| Remote endpoint transport | Trivial/easy | OSS |
| Errors, timeouts, symbols, and raw services | Trivial/easy | OSS |
| Debug, register, serial, and connection primitives | Easy | OSS |
| Single-target batch and execution discipline | Easy/moderate | OSS |
| Memory and flash primitives from plans/policy | Moderate | Extract primitive to OSS; retain decision machinery closed |
| Recovery primitive from approval and recovery policy | Moderate | Extract primitive to OSS; retain decision machinery closed |
| Pack parsing and minimal byte storage from firmstore | Moderate | OSS feature; closed firmstore consumes or wraps it |
| Stateless onboarding from persistent board knowledge | Moderate/high | Portable onboarding OSS; persistence and lifecycle closed |
| Discovery hooks from inventory/assignment policy | High | Hook and explicit discovery OSS; persistent policy closed |
| Device geometry from safety maps and reviewed authority | High | Facts and parsing OSS; policy map, authority, and enforcement closed |
| Thin MCP surface from polished plan/tutorial contract | High | Functional primitive MCP OSS; optimized execution experience closed |
| `server.py` composition root | High | Recompose both products over a public OSS library boundary |

Previous line-count estimates should be treated as provisional. The correct
feature boundary comes first; implementation size can be measured accurately
after the public HAL contracts and closed service boundaries are explicit.

## Public contracts required by the closed product

At minimum, define and version:

- adapter discovery and capability contracts;
- endpoint and session identity types;
- target/device capability and geometry types;
- primitive operation request/result types;
- deadline, cancellation, cleanup, and error semantics;
- artifact identities and flash-result evidence;
- explicit board/profile input schemas;
- customer safety-policy input and decision schemas;
- hooks or backend APIs through which customers can supply policy decisions;
- observation/evidence returned from direct physical operations; and
- compatibility tests that every HAL implementation must pass.

Publishing a contract does not require publishing the commercial engine that
uses it.

## Practical extraction order

1. Define the OSS HAL contracts and the standalone acceptance suite.
2. Extract adapters, physical discovery, raw services, device-support parsing,
   build/artifact utilities, and execution discipline into the OSS library.
3. Separate physical facts from commercial decisions: geometry from safety,
   primitive requests from plans, recovery mechanisms from recovery policy, and
   direct results from audit.
4. Build the OSS programmatic API, operational CLI, and thin MCP server.
5. Keep firmstore, safety policy, plans/gates, approvals, monitor/audit, and the
   polished agent experience in the closed product.
6. Make the closed product consume only the published HAL boundary.
7. Build the greenfield inventory, scheduling, topology, remote lab, and
   multi-resource transaction layers above it.

## Final decision summary

The OSS project is not the best possible version of the whole product. It is
the strongest strategically useful open physical-access substrate.

The closed product is not merely “the same tools with enterprise packaging.” It
owns the valuable use-only machinery that decides, remembers, coordinates,
constrains, optimizes, and assumes responsibility for physical systems.

For every future feature, ask where it produces more total value after weighing
standalone necessity, individual utility, ecosystem adoption, required
customization, commercial differentiation, willingness-to-pay, substitution
risk, and implementation cost.

Then place the feature there—even when the result makes one side somewhat less
complete.
