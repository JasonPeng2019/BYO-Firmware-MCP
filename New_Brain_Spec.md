# Server B Design

## Normative Authority

This document is the canonical product behavior specification for Server B. It incorporates the
accepted single-file safety-authority decision in `decisions/ADR-0001-single-file-safety-authority.md`,
the behavior specified by `docs/safety-layer-v2-spec.md`, and deliberate runtime contract upgrades
recorded in `tests/contracts/product-server-tools.json`.

When historical implementation notes or archived action lists disagree with the behavior below, the
following order controls:

1. Accepted ADRs for one-way safety-authority decisions.
2. This specification for product behavior and agent-facing workflow.
3. The active product tool contract for exact MCP names and schemas.
4. Historical extraction and gap-audit documents as evidence only.

Normative tool names use lowercase `snake_case`; the `*-plan` suffix is retained exactly. Historical Title Case or hyphenated spellings in archived evidence are non-authoritative aliases.

## Server B Main

### Capabilities

#### Server B Layer 1

* Guards specific hardware-level tools
* Returns text prompt guides to the client agent on how to properly use hardware tools before unlocking them
* Responsible for prompt-injection to client Agent

#### Server B Layer 2

* Hardware actions that affect the board directly (flash, register/memory writes, resets, etc.)
* Write-capable and disruptive Layer 2 actions are guarded by corresponding Layer 1 plans. A small ordinary/read-only subset remains always visible. Every Layer 2 handler independently enforces its applicable L2 safety checks; visibility is never authorization.

### Structure

Server B is a local stdio MCP server, accessed by one computer with no authentication.

Server B (Guarded Hardware Server): This is a guardrail’d MCP server. It exposes direct actions to the board, but each action is guarded by a set of guardrail actions that require the Middleman or Client Agent to execute the guardrail actions first, before Server B exposes the tools in Server B. Thus any agent trying to directly access the board must go through:
- **Layer 0 — Security Setup:** Confirm immutable or locked bytes that must never be changed and establish hardware security.
- **Layer 1 — Guardrail Actions:** Require a reason, hypothesis, expected result, safety proof, and cleanup instructions before direct hardware actions are exposed.

**Server B Layer 2 - Hardware Actions:** Board-facing actions return results with safe-exit guidance. Write-capable and disruptive actions require Layer 1 plans; ordinary profile, read-only, resume/halt/step, breakpoint-removal, wait, and reset-and-run actions remain visible but still enforce handler-level scope and safety checks.

### Server B L2 Safety

#### Server Safety

The main danger with the server and models, is, given regular SWD/JTAG and UART Read/Write abilities (no extra download programs - giving these to the agent would be accepting the risk that comes with giving the agent this capability), is flash and register write ability, which, if flashing the wrong image or writing the wrong registers, could permanently cause MCU lockout. Other capabilities may cause application crash, bootloader crash, etc., but will not render the MCU un-recoverable permanently, so we will let the agent recover the board in these cases.

We have determined a few  types of safety level actions when SWD/JTAG is enabled:

1. flash_application
2. flash_bootloader
3. register_write (for peripheral/config registers)
4. memory access:
   1. Find_symbol
   2. Write (takes Symbol or Address)
   3. Read (takes Symbol or Address)
5. write_cpu_register (normal)
6. set_execution_state
7. reset device:
   1. reset_and_run
   2. reset_and_halt
   3. connect_under_reset

These need different safety levels for these different level of actions.

##### Stable Safety Authority

The server keeps write-capable actions closed until it has a complete, reviewed safety map for the
selected logical board and a live identity proof for the current connection.

The only persisted safety-authority file is:

```text
.firm/safety/<board_id>/memory_map.yaml
```

It contains stable board/MCU identity, reviewed source digests, physical flash/RAM geometry, erase
geometry, deployment partitions, typed regions, and provenance. Gates, plans, permissions, live
identity, and map stamps are never restored from disk. `source_manifest.json`, `safety_report.json`,
persisted aggregate fingerprints, and persisted gates are not authority and must not be read or
written by the current design.

The safety map classifies at least:

1. Prohibited option-byte, UICR, OTP, provisioning, lifecycle, protection, and debug-authentication
   regions.
2. CPU and system-control registers.
3. Peripheral register windows.
4. Physical flash and erase geometry.
5. ROM bootloader/system memory.
6. A reviewed stable application deployment partition.
7. An optional separately reviewed user-bootloader deployment partition.
8. Physical RAM and writable RAM regions.

Callers and agents never provide allowed ranges, erase geometry, or authority-bearing evidence.
Unknown memory is denied, and prohibited classifications override broader flash, RAM, or peripheral
classifications.

A normal firmware build does not change persistent safety authority. Firmware artifacts are
per-operation inputs. Every flash independently parses the actual ELF and optional matching HEX and
checks target identity, segments, entry point, vector table, partition containment, erase-sector
containment, prohibited overlap, and unknown-memory overlap before backend mutation.

##### flash_application

`flash_application` may write only the stable reviewed application deployment partition.

At populated-plan acceptance the server hashes the selected artifact and binds the digest to the
plan. Immediately before execution it hashes the artifact again. Changed bytes cause a pre-execution
refusal, invalidate/relock the plan, consume no call budget or permission, and invoke no backend
erase or write.

The ELF is authoritative for entry point, vector table, executable segments, and build metadata. A
selected HEX must have a matching ELF companion collected from the same build. Raw BIN and HEX-only
inputs may be retained as provenance but are not eligible for guarded flash.

The complete required erase footprint and every loadable range must fit inside the application
partition. The image must not touch bootloader, prohibited, ROM, or unknown space. The build can
prove that it fits the stable partition but can never widen that partition.

##### flash_bootloader

`flash_bootloader` follows the same execution-time artifact inspection and digest-binding rules but
may write only a separately reviewed user-bootloader partition. If no authoritative bootloader
partition exists, the action remains unavailable.

Bootloader flashing requires its L1 plan and user permission. It rejects application, prohibited,
ROM-bootloader, and unknown ranges and never accepts caller-provided allowed ranges. Ordinary
firmware work should use `flash_application`.

##### register_write

The server does not need intelligence or knowledge of every board.

The model can:

1. Read the reference manual/datasheet.
2. Read the SVD.
3. Confirm that the register name, address, and field agree.
4. Submit the exact address, mask, and value.

The server validates the complete access against the stable map before target I/O. Flash security, option bytes, OTP, debug protection, provisioning, and lifecycle registers are unavailable to ordinary register writes; no plan can authorize them. `target_unlock` is limited to typed documented recovery and is not a general register-write escape hatch.

##### Memory Access

Memory access is split into distinct tools rather than one dispatcher: `find_symbol`, `read_memory_symbol`, `read_memory_address`, and `write_memory`. Symbol access is the default path. A planned raw-address read may inspect any completely mapped, non-prohibited region; raw-address writes remain an explicit RAM-only fallback.

Behavior:

1. Symbol tools (`find_symbol`, `read_memory_symbol`, and symbol-mode `write_memory`) resolve the symbol from the ELF/DWARF and access that variable.
2. `read_memory_address` is itself the explicit planned raw-read path. It requires no redundant fallback flag or reason. The server permits the complete requested access only when it fits within mapped, non-prohibited memory. Eligible mapped regions may include flash, RAM, peripherals, ROM, and system memory; unknown and prohibited ranges are denied. Peripheral reads must account for documented clear-on-read or other read side effects.

3. Raw-address `write_memory` rejects a request without `allow_address_fallback` with:

   > “Try a symbol first. Provide a symbol name or explicitly request address fallback.”

4. Raw-address writes require:
   * `allow_address_fallback: true`
   * a brief reason symbols are unsuitable
   * stable-map RAM containment for the complete backend access width

The tool descriptions should tell the model:

```text
Prefer symbol access whenever source code or debug symbols identify the
intended variable. Use raw addresses only for dynamically allocated,
pointer-derived, stack, optimized-out, or otherwise unsymbolized memory.
```

Symbol search uses `find_symbol`, for example a query such as "motor speed".

So the surface supports:

```text
find_symbol
read/write by symbol
read/write by address as explicit fallback
```

##### Write CPU Register & Set Execution State

CPU Registers:

* write_cpu_register is for ordinary core registers such as R0–R12 and floating-point registers.
* set_execution_state is for registers that change control flow or CPU mode, such as PC, SP/MSP/PSP, LR, xPSR, CONTROL, PRIMASK, BASEPRI, and FAULTMASK.

The second category is more disruptive because it can jump execution, corrupt the stack, mask interrupts, or fault the CPU. It is still usually recoverable with a reset.
A simple policy is:
write_cpu_register
  allowed: R0-R12, FPU registers

set_execution_state
  allowed: PC, SP, MSP, PSP, LR,
           xPSR, CONTROL, PRIMASK,
           BASEPRI, FAULTMASK

**set_execution_state should require special permission from the user:**

* Just like bootloader flash, through a separate tool that unlocks it and takes a parameter: the user either requested to give it access just one time, or access for the rest of the live server run.
* Just like bootloader flash, should default to returning a failure + a request for unlock with user permission so that the model asks the user first.

You do not need per-board mappings for standard Cortex-M core registers. pyOCD can report which registers the connected core supports, and the server can reject unknown names.

##### Reset Device

A device reset should be implemented if cleanup fails, or the device gets locked in some state and the agent can’t figure out how to get the program back to old defaults.

Use three simple reset actions:

**reset_and_run**

* Reset the MCU.
* Start executing from the reset vector.
* Keep the server session active.

Use this when the device is temporarily stuck, halted, or in a bad runtime state.

**reset_and_halt**

* Reset the MCU.
* Halt immediately at startup.
* Keep or release the debug session depending on the tool.

Use this when the firmware crashes immediately and the model needs to inspect startup behavior. pyOCD directly supports reset with an optional halt.

**connect_under_reset**

* Assert the physical reset line.
* Connect through SWD while reset is active.
* Halt the core.
* Release reset.

Use this when firmware quickly enters sleep, reconfigures clocks or pins, crashes, or otherwise makes normal debugger attachment difficult. This requires the probe’s reset line to be connected and supported.

#### Managed Operations, Cancellation, and Cleanup

Every blocking hardware action runs inside a request-bound managed operation with:

1. a finite timeout;
2. one logical board/probe lock while preserving cross-board concurrency;
3. an operation/request identity and cooperative cancellation token;
4. explicit ownership of UART handles, debug sessions, reset lines, and helper processes; and
5. one idempotent server-owned cleanup path.

In-process pyOCD and serial calls may run in managed worker threads so persistent handles retain
correct ownership. External commands alone run as owned subprocesses with validated argv, a separate
process group, a marker tying the process to this Server Run, and bounded terminate/force-kill
cleanup. Startup hygiene terminates only a still-matching owned helper; it never kills by name alone.

Each request maps to its managed worker, owned subprocesses, UART/debug resources, and board lock.
When MCP cancellation, timeout, client EOF, or server shutdown occurs, the server marks the operation
cancelled, requests cooperative cancellation, terminates owned process groups, closes resources when
safe, releases reset/control lines, and releases the board lock in `finally` logic.

Flash becomes non-interruptible after the backend transaction begins. Cancellation then waits for
bounded safe completion before closing the session, avoiding a deliberately half-written image.
Pre-transaction validation remains interruptible.

Mandatory cleanup always stops owned I/O, closes operation-owned UART/debug resources, terminates
owned processes, releases reset lines, and releases locks. It does not depend on model-supplied
cleanup and continues after an individual cleanup error.

Successful ordinary work preserves the action's documented MCU state. Cleanup does not silently
reset the board or erase volatile state. Reset-and-run is explicit through `reset_and_run` or an
eligible structured `on_exit` finalizer. Intentional halt actions preserve halt; abnormal started
failure, timeout, cancellation, EOF, and shutdown still release all server-owned resources.

Optional device finalizers are a small closed union such as bounded `uart_write` or `reset_and_run`.
They are supplied in the original request, short, best-effort, and run before mandatory cleanup.
Arbitrary model-provided shell commands are forbidden, and finalizer failure never prevents cleanup.

Only one active operation may own a physical probe/board. Assignments and locks are board-local so
independent physical boards may proceed concurrently.

## Setup

### Automated Board Setup, Safety, and Validation

Creating a board profile should not require firmware developers to know pyOCD target names, CMSIS-Pack identifiers, probe-matching rules, silicon-ID registers, or protected memory ranges.

The MCP server collects only facts a firmware developer is likely to know, resolves everything possible from local hardware and workspace data, and validates the result against live hardware before committing it.

When outside research is required, the MCP tool does not perform it. It returns a focused prompt explaining what is missing, what was observed, what the agent must research, what evidence to return, and how the server will validate it. The agent performs the research and retries the tool.

Setup and repair authorization uses the L1 \*-plan tools defined in the plan design. Plan mechanics — the all-NULL details call, plan fields, call budgets, permission values, plan replacement, and server enforcement — are specified there and not repeated here. This document covers only setup-specific behavior.

#### Goal and responsibility boundary

The system should:

1. Collect board facts through normal user-agent conversation.

2. Inventory the hardware and workspace.

3. Deterministically resolve target, pack, and CMSIS data where possible.

4. Ask the agent to research unresolved documentation facts.

5. Validate agent-supplied facts against local Pack/CMSIS data and live hardware.

6. Generate a workspace safety map.

7. Keep guarded hardware actions closed until hardware and configuration freshness are established.

8. Re-close them when the connection ends or map inputs change.

9. Commit portable board facts only after deterministic validation.

Responsibilities are explicit:

- **User:** supplies ordinary board facts, resolves physical ambiguity, and grants permissions through normal conversation.

- **Agent:** talks to the user naturally, performs research, and calls MCP tools using structured JSON.

- **MCP server:** inventories, executes, validates, persists, fingerprints, and enforces tool visibility and memory-region boundaries.

The user’s exact MCU part number is authoritative and is never silently replaced.

#### User, Agent, and Server Interaction

The user never interacts with the MCP server directly and never needs to read, write, or approve JSON.

JSON is strictly an agent-to-server implementation detail. The agent translates between structured tool calls and ordinary user conversation.

When a tool needs user input, its response must include an agent instruction that says to ask conversationally and not expose JSON, continuation IDs, internal field names, or raw server state.

```json
{
  "status": "setup_needs_user_input",
  "continuation_id": "continue_...",
  "agent_prompt": "Ask which connected ST-Link belongs to the board. Present friendly descriptions in normal prose. Do not show this JSON, the continuation ID, or internal field names.",
  "choices": [
 {"choice_id": "probe_1", "display_name": "ST-Link ending 4857"},
 {"choice_id": "probe_2", "display_name": "ST-Link ending 19A2"}
  ]
}
```

The agent might ask:

I found two ST-Link probes. Which one is connected to this board: the one ending in 4857 or the one ending in 19A2?

The user answers normally; the agent converts that answer into the next tool call.

Research requests follow the same rule. The server returns a prompt for the agent, not a questionnaire for the user. The agent should involve the user only when an authoritative user fact or real choice is required.

General agent prose is never authorization for a guarded action. The agent gathers permission in ordinary language and passes it through the action’s plan tool.

#### Setup and Validation Tools

The public setup and readiness surface is:

- `setup_overview`
- `load_setup_tool`
- `board_setup-plan`
- hidden `board_setup` and `board_fix_setup` after an accepted setup plan
- `continue_setup`
- `board_safety_refresh`
- `board_validate`
- `get_setup_status`
- `target_unlock-plan`

`setup_overview` is the server-owned route from familiar names to profiles, friendly physical
connections, server-generated IDs, and exact next-call templates. The agent never invents or asks
the user for internal IDs.

`display_name` is the user's familiar name for one physical board. Neither the user nor the agent
provides, selects, or invents `board_type`. Setup resolves reviewed MCU/device support internally
from the user's exact MCU part number and the server-computed digest of the supplied official
datasheet. A custom PCB may reuse reviewed support for its MCU/device without a user-created hardware
definition. Missing or ambiguous reviewed MCU/device support produces a typed support/evidence result;
being a custom PCB by itself does not make the board non-writable.

`continue_setup` accepts only the exact server-requested friendly choice or official-source research
reply for one live continuation. It grants no plan, permission, safety authority, or gate state.

`board_safety_refresh` is the only public safety-map creation, maintenance, and recovery tool. It
accepts only `board_id`, deterministically rebuilds one complete candidate map from server-owned
reviewed sources, and atomically promotes it only after full validation. Initial setup may invoke the
same internal rebuild. There is no separate public `board_safety_setup` path in the current design.

`board_validate` proves live MCU identity and associates the current canonical map digest with that
live proof. `get_setup_status` reports durable configuration readiness, live-session readiness, and
UART readiness separately without opening a connection or gate.

`target_unlock-plan` and its underlying action remain separate. Setup and validation only report a
locked target; destructive recovery requires exact live disclosure and fresh one-time permission.

There is no separate research provider or arbitrary terminal-command layer. The server may return
directly usable, host-appropriate advisory commands for builds, dependencies, and optional build
helpers, but never executes them automatically. Advisory guidance grants no plan, permission, safety
authority, gate state, or flash authority.

#### Session Startup, Board Assignment, and Tool Choice

At the start of every Server Run, before board-specific work, the agent asks the user conversationally which boards are connected. The user gives each board its unique familiar name, or says **“no board.”** The user never supplies `board_type`, board_id, connection IDs, permission enums, or JSON.

The server enumerates every connection separately and returns friendly probe, port, and read-only board details. The agent maps the user-supplied names to profiles and connections. Exact attachment-cache matches may resolve silently; ambiguous assignments are presented conversationally. Names and active connections must match one-to-one before validation or setup proceeds.

##### Logical board profiles

Each Board YAML stores:

- board_id: the stable, unique, machine-facing profile ID and YAML filename stem.

- display_name: the unique, user-facing name used in conversation.

board_id is created with the profile, usually from its initial name, and remains stable if display_name changes. The server rejects .firm/boards/\<board_id\>.yaml when its filename and internal board_id disagree.

A profile represents a logical role/configuration, not immutable physical hardware. A compatible replacement board may use an existing profile when its part, target, hardware checks, and safety configuration validate. For the current session it then assumes that profile’s name and ID.

Session assignments are in-memory only:

```text
connection_1 → board_id: left_controller
 connection_2 → board_id: right_controller
```

Each active connection has exactly one board_id, and one board_id cannot address two active connections. Assignments and gates clear on disconnect or at the end of the Server Run.

##### Explicit per-board startup flow

The following flow runs independently for every user-named connection:

1. Resolve the supplied name against display_name in .firm/boards/\*.yaml.

2. If exactly one matching profile exists, assign the selected connection provisionally, call load_setup_tool, and call board_validate only.

3. If validation succeeds, bind that connection to the profile’s board_id and name for the rest of the active session and open only that board’s gate.

4. If validation reports a hardware/profile mismatch, ask the user to correct the connection-to-name assignment. Do not rewrite or silently reassign the profile.

5. If no matching profile exists, begin the board_setup-plan flow below.

6. If the profile exists but setup state is incomplete or failed, begin the same flow in repair mode.

A matching board name therefore means **validate, not setup**. A missing name/profile means **plan, then setup**. Setup and validation of one board never apply to another connection.

##### board_setup-plan scope

board_setup-plan follows the permission-locked plan flow. Setup-specific behavior:

- A setup plan is scoped to one intended connection and one logical profile. For a new profile it uses the requested display_name and a proposed/generated board_id; for repair it uses the existing board_id and recorded setup state.

- The plan’s underlying-tool parameters identify the connection, the profile, and setup versus repair mode.

- One valid setup plan permits one board_setup call and one board_fix_setup call. The plan’s redirect names board_setup for a new profile or board_fix_setup when recorded state already requires repair.

- When setup completes, validation follows and both actions are relocked behind a new plan.

- If board_setup fails or is incomplete, the same plan’s single board_fix_setup call remains available for the first repair attempt without asking the user again — even under one-time permission.

- Any further setup or repair attempt requires a replacement setup plan. With one-time permission the agent must ask the user again first; with full-session permission it continues without re-prompting, within deterministic retry limits.

- Setup/fix authorization also closes on disconnect of the scoped connection, user revocation, or completion or cancellation of the workflow.

##### Setup, repair, and validation sequence

When no matching profile name exists:

1. Call load_setup_tool, then complete the board_setup-plan flow, gathering one-time or full-session permission through normal conversation.

2. Call the exposed board_setup once.

3. If setup completes, call board_validate.

4. If setup fails or is incomplete, call the already-authorized board_fix_setup once.

5. If repair completes, call board_validate.

6. If repair cannot complete, submit a replacement setup plan — asking the user again only under one-time permission. Stop on a deterministic blocked/unresolved result or retry-budget exhaustion.

When a matching profile name exists, skip setup and repair and call board_validate directly.

##### Mid-session gate closure

The run-scoped gate keeps two distinct concepts:

- `LiveIdentityStamp`: board, connection, probe, observed MCU identity, and validation run.
- `SafetyMapStamp`: canonical digest of the current parsed stable map.

Disconnect, connection replacement, server restart, identity repair, and destructive recovery clear
the live identity proof. A stable-map refresh can update only the map stamp when the same live proof
still applies; it can never create live identity authority.

Ordinary rebuilds, artifact-path changes, flash, reset/halt, UART activity, and successful
non-destructive actions do not invalidate live identity. A map digest mismatch closes the write gate
until `board_safety_refresh` rebinds the current map, while loss of live proof requires
`board_validate`.

There is no standalone open-gate action. A successful validation establishes live proof and map
association; refresh may update the association only for an already valid same-board connection.

#### Scope and Persistence

Server-owned project state lives under `.firm/`:

```text
.firm/
   boards/<board_id>.yaml
   packs/manifest.yaml
   packs/files/
   setup/<setup_id>/report.json
   setup/<setup_id>/events.jsonl
   safety/<board_id>/memory_map.yaml
   validation/<validation_id>/report.json
   validation/<validation_id>/events.jsonl
   cache/attachments.yaml
```

Exact report filenames may evolve under the active product contract, but the ownership boundary may
not: profiles, pack pins, safety authority, setup/validation evidence, and attachment hints belong to
FirmStore; live gates and authorization do not.

Native build outputs and provenance-only firmware bundles deliberately remain outside `.firm`.
`collect_build_artifacts` copies explicit ELF, HEX, BIN, and linker-map outputs into a caller-chosen
new or empty directory and writes a deterministic portable `build-manifest.json`. The collector
performs no build, search, download, or hardware access, and its manifest contains no allowed
ranges, plans, permissions, gates, or safety authority. Artifact paths are passed explicitly to the
applicable flash plan; collection alone never authorizes deployment.

The project's existing native IDE or CLI workflow and a compatible SDK/toolchain already available
to that project are the primary build path. For example, an NCS project should prefer its installed
NCS environment. When no suitable native workflow is available, the product may offer an appropriate
Zephyr or vendor-specific helper as an optional fallback, never as a mandatory build path. It does
not silently install, replace, upgrade, or reconfigure toolchains. Any returned build or dependency
command is advisory and carries no authorization.

Board YAML stores portable logical profile facts. Current connection assignments, validation stamps,
map stamps, plans, and permissions remain in memory and reset with the Server Run. The host-local
attachment cache is excluded from source control and is only an assignment hint.

#### Source Authority and Double Verification

Map facts retain explicit source ownership. The server must not treat user assertions, linker data, Pack/CMSIS data, and datasheet research as interchangeable.

##### User-owned data

The user supplies only:

- Existing familiar profile name or a new familiar board name.

- Exact MCU part number.

- Local official datasheet PDF during initial setup or evidence repair. The server validates the
  document and computes its SHA-256; the user does not supply the digest.

- UART baud rate when UART is used.

- Physical probe/UART selection when ambiguous.


The user does not supply `board_type`, pyOCD targets, evidence hashes, register ranges, flash
geometry, or protected addresses. The accepted MCU/datasheet association is persisted, so later
connections normally require only the familiar board name.

##### Stable deployment partitions and per-build artifacts

Application and optional user-bootloader deployment partitions are stable reviewed policy recorded
in `memory_map.yaml`. They come from server-owned reviewed board/device evidence and explicitly state
whether a full-flash application partition is safe or whether a resident bootloader partition must
be preserved. Missing partition authority keeps the corresponding flash action unavailable.

Linker scripts, linker maps, ELF files, and HEX files describe one build. They are not persistent
safety authority and do not widen deployment policy. At each flash operation the server parses the
actual selected build and proves its loadable segments, entry point, vector table, target, and erase
footprint fit the stable partition.

For executable debugging, the current ELF supplies executable sections. A stable application
partition is not assumed executable in its entirety.

##### Independently reviewed hardware evidence

Persistent hardware classifications require two distinct, pinned, hashed, server-owned evidence
roles:

1. reviewed device-support evidence derived from the applicable Pack/CMSIS/SVD/target support; and
2. reviewed official vendor datasheet/reference-manual evidence.

The server parses both through strict schemas and deterministically reconciles exact part/target
identity, physical flash/RAM, erase geometry, prohibited/security regions, CPU/system ranges,
peripheral windows, ROM/system memory, aliases, banks, and deployment policy. Empty allowlists or a
missing role mean unavailable/unreviewed, not "accept any source."

The agent may research an official target, pack, or document candidate only when the server requests
it. Research can help maintainers acquire evidence but does not promote arbitrary address facts into
safety authority. A board whose MCU/device lacks independent reviewed evidence remains non-writable;
a custom PCB using an already reviewed MCU/device does not require a user-created board definition.
The user supplies neither hardware ranges nor evidence hashes.

Prohibited classifications override broader peripheral, flash, or writable classifications. Unknown
memory is denied by default.

#### Field Ownership

| Field or record | Primary source | Agent research? | Persisted location |
| :--- | :--- | ---: | :--- |
| board_id | Generated or selected logical profile ID; stable and filename-matching | No | Board YAML |
| display_name | Unique user-facing board name | No | Board YAML |
| mcu_part_number | Exact user input or persisted profile | No | Board YAML |
| mcu_family | Deterministic part derivation | No | Board YAML |
| probe_family, probe_type | Selected probe inventory/mapping | No | Board YAML |
| pyocd_target | Exact reviewed MCU/device-support mapping; a staged pack may supply but never redefine it | Research may locate official support, but cannot redefine identity | Board YAML |
| Official datasheet digest | Server hash of the user-supplied official PDF, accepted against reviewed MCU/device evidence | No | Board YAML and reviewed setup evidence |
| serial_baudrate | User input | No | Board YAML |
| Probe/serial hints | Built-in rules | No | Board YAML |
| test_read_address | Family default or validated candidate | Unsupported families only | Optional Board YAML |
| silicon_id_\* | Pack/CMSIS, official docs, live read | Sometimes | Optional Board YAML |
| expected_uart_substring | Optional user behavior expectation used by setup status/serial workflows, not identity validation | No | Optional Board YAML |
| External pack pin | Staged and verified pack | If support unavailable | Pack manifest |
| Probe/UART association | Stable local hardware identities | User confirms ambiguity | Host cache |
| Stable application/bootloader deployment partitions | Reviewed server-owned policy | No | Safety map |
| ROM bootloader, physical memory, registers | Independently reviewed device-support plus official-document evidence | Research candidates only; maintainer review required | Safety map |

probe_type remains required compatibility/display data, for example stlink → ST-Link.

The agent never writes Board YAML, manifests, maps, reports, or cache records directly.

#### Inputs

| Situation | User provides through normal conversation |
| :--- | :--- |
| Live Run startup | Unique names of all connected boards, or “no board” |
| Known board | Existing familiar profile name; UART baud rate only when UART is used or changed |
| Custom board | Familiar board name, exact MCU part number, local official datasheet PDF, and UART baud rate when UART is used |
| Multiple probes/UARTs | Selection from friendly descriptions |
| Unprovable external UART mapping | Confirmation that it is attached to the board |
| L1-protected operation | Permission required by that action’s plan tool |

#### Host-Local Attachment Cache

Portable board identity and physical bench attachment are different facts. Board YAML can state that a board uses an STM32L476RGT6 and ST-Link; it cannot prove which external UART adapter is wired to it.

When the user resolves ambiguity, setup records stable USB identities rather than COM/tty paths:

```yaml
board_id: my_board
probe:
  family: stlink
  usb_serial: "066EFF534857..."
uart:
  usb_serial: "A50285BI"
  vid: "10C4"
  pid: "EA60"
confirmed_attachment: true
confirmed_at: "2026-07-11T00:00:00Z"
```

Later calls may reuse the association when board ID, probe serial, and UART serial match exactly, then resolve the current port path. A change from COM7 to COM11 does not prompt again.

The cache is an assignment hint, not permanent ownership. The user may assign a compatible replacement board to the same profile in a later session. The cache is ignored if a serial is missing, hardware changes, multiple records match, the probe differs, or the user revokes it.

#### Deterministic Preflight

Before research, setup inventories hardware and workspace state: user input, connected probes and serial ports, attachment-cache matches, reviewed catalog eligibility, built-in and manifest pyOCD targets, and exact auto-detected target evidence.

| Condition | Deterministic result | Research? |
| :--- | :--- | ---: |
| No probe | setup/no-probe | No |
| Multiple probes | Agent asks user conversationally | No |
| No required UART | setup/no-uart | No |
| Multiple UARTs | Exact cache match or conversational selection | No |
| External adapter cannot be mapped | Conversational confirmation and cache | No |
| pyOCD returns one exact target | Use it | Optional enrichment only |
| pyOCD returns no exact target | Target-research prompt | Yes |

Selected hardware identities belong in reports/cache, never portable Board YAML.

#### Core Board YAML

Setup creates a draft profile in memory and commits it only after deterministic target support,
reviewed safety eligibility, and live connection checks succeed:

```yaml
schema_version: 2
board_id: my_board
display_name: "My Board"
mcu_part_number: STM32L476RGT6
mcu_family: stm32l476
probe_family: stlink
probe_type: ST-Link
pyocd_target: stm32l476rgtx
serial_baudrate: 115200
```

The profile is stored at `.firm/boards/my_board.yaml`; its filename stem and internal `board_id`
must match. It stores no live probe/port assignment and no external-pack identifier. The safety map
is a separate server-owned authority keyed by `board_id`; profile timestamps, display-name changes,
UART settings, report identifiers, and artifact paths do not stale that map.

#### Agent Research Handoff

A research response must contain enough context that the agent does not need hidden server state:

```json
{
  "status": "setup_research_required",
  "continuation_id": "continue_...",
  "agent_prompt": "Research the exact pyOCD target for STM32L476RGT6 using official pyOCD, vendor, or CMSIS-Pack sources. Do not ask the user for a target and do not show this JSON. Return one candidate with evidence in the next tool call.",
  "observed": {},
  "constraints": [],
  "rejected_candidates": [],
  "accepted_response": {},
  "validation_plan": []
}
```

The prompt states: the unresolved fact and authoritative board/MCU facts; relevant observed tool output; prior rejected candidates and failures; acceptable sources; exact response fields; fields the agent must not change; the server’s validation plan; and whether the user must be asked anything (default: no).

Research never authorizes a write, erase, unlock, security-state change, or MCU identity change.

#### Target Resolution and Pack Support

If pyOCD cannot resolve one exact target, the agent may return one official evidence-backed candidate:

```json
{
  "pyocd_target": "stm32l476rgtx",
  "evidence": [
 {"source": "official source", "claim": "This target covers STM32L476RG devices."}
  ],
  "reasoning_summary": "The target matches the supplied device variant."
}
```

The server requires one syntactically valid, part-consistent target, verifies that built-in pyOCD or a pack exposes it, and requires a successful live connection before commit.

For automatic writable setup, that target must also equal the exact reviewed MCU/device-support mapping for the supplied part and accepted datasheet. Research may locate support for the reviewed target but cannot infer a new part-to-target mapping by prefix, wildcard, package-name normalization, or agent assertion. A target that connects but lacks reviewed safety evidence remains non-writable.

| Support state | Action |
| :--- | :--- |
| Built into pyOCD | Continue |
| Supplied by current manifest | Continue |
| Unavailable | Return pack-research prompt |

The agent may supply one complete official pack candidate. The server stages it, computes SHA-256, compares an official checksum when available, enumerates its targets, requires the requested target, connects using the staged pack, and promotes it only after validation.

A failed candidate is recorded with observed target-listing output; the next prompt requires a materially different candidate.

#### Exact Live Identity Support

Every automatically writable board profile requires reviewed live identity evidence. This may be an
exact part register or a documented masked family identifier when package information is not
electronically observable; the limitation is explicit in reviewed catalog data.

A generic readable test address is not an identity substitute. Optional safe-read diagnostics may be
recorded, but missing identity evidence makes validation stamp-ineligible and keeps guarded writes
closed. Failed candidates remain out of the profile and are recorded only as evidence.

#### Stable Safety Map and Refresh

`board_safety_refresh(board_id)` is the sole public safety maintenance operation. It accepts no build
artifacts or caller-supplied ranges.

Every refresh:

1. Loads the exact schema-v2 profile.
2. Loads independently reviewed device-support and official-document evidence.
3. Reconciles identity, physical memory, prohibited regions, register windows, ROM, erase geometry,
   and deployment-partition policy.
4. Builds one complete deterministic candidate map.
5. Applies prohibited precedence, overlap checks, schema checks, and exact-part checks.
6. Atomically replaces only `memory_map.yaml` after the whole candidate passes.
7. Deletes or ignores legacy `source_manifest.json` and `safety_report.json` authority.
8. Reports semantic change groups for explanation without maintaining a second partial-mutation
   algorithm.
9. Updates the map stamp only if the same board/connection still has valid live identity proof.

Refresh is required for a missing, malformed, old-schema, or inconsistent map and when stable safety
facts may have changed: MCU/target evidence, reviewed pack/SVD/target or official-document evidence,
physical geometry, erase policy, deployment partitions, or map-generator schema.

Refresh is not required for a normal application rebuild, artifact path or timestamp change, image
size change that remains inside policy, flash, reset, halt, UART use, or report bookkeeping.

Outcomes include:

```text
safety_refresh_completed
safety_refresh_blocked
safety_conflict
safety_evidence_unavailable
```

A failed rebuild never promotes a partial map. Missing independent reviewed evidence keeps guarded
writes closed and names the unavailable evidence; setup and agent-provided address ranges are not
fallback authority.

#### Write-Gate Lifecycle

Each board gate is run-, board-, and connection-scoped and begins closed.

| State or event | Required action |
| :--- | :--- |
| Unknown profile name | `board_setup-plan`, then the paired setup/fix workflow |
| Existing profile, no current map or invalid/old map | `board_safety_refresh` |
| New Server Run or no live identity proof | `board_validate` |
| Disconnect, probe replacement, or connection identity change | Reassign, reconnect, then `board_validate` |
| Destructive recovery or explicit MCU identity repair | `board_validate` after recovery/repair |
| Stable map changed while live identity remains valid | `board_safety_refresh`; it may update only the map stamp |
| Ordinary new firmware build | Proceed to artifact collection when useful, then the applicable flash plan |
| Artifact does not fit stable partition | Fix or select the build; do not refresh or widen policy |
| Live MCU differs from established profile | Tell the user expected versus observed identity and ask what to do |
| User elects to adopt different silicon | Create a new logical profile through an explicitly authorized setup route; never rewrite the established profile in place |

A compatible replacement with the same reviewed identity may reuse an existing logical profile after
validation. An MCU mismatch never silently changes `mcu_part_number` or safety authority.

#### Action Surface

Normal connection is profile-only: `connect(board_id)` resolves the named persisted profile and does
not accept probe, target, board-config, or launch-environment overrides. Exceptional manual values
are available only through plan-guarded `connect_override`; they are run-scoped and never rewrite a
profile or conceal a validation mismatch.

Always-visible non-authorizing workflow tools include `setup_overview`, `continue_setup`,
`get_setup_status`, and `collect_build_artifacts`. Their outputs may route or describe work but never
supply safety authority, permission, or an open gate.

The exact visible/hidden tool schema is governed by the active product contract and the L1 plan
section below.

#### Type-Specific Request Validation

The map is stable policy, while per-operation inputs prove the actual requested access.

##### `flash_application` and `flash_bootloader`

Hash-bind the artifact at plan acceptance and recheck it before execution. Require a coherent ELF
(and matching HEX when selected), exact live target identity, valid entry/vector metadata, complete
segment containment, and complete erase-sector containment inside the correct stable partition.
Reject the other partition, prohibited, ROM, and unknown memory before backend mutation.

##### `write_memory`

Resolve the address or symbol. Symbol access is preferred. Raw addresses require the explicit
fallback flag and reason and are RAM-only. The complete backend access width must fit mapped writable
RAM and remain outside prohibited/unknown regions.

##### `register_write`

Require the complete word to be inside a mapped peripheral window and outside prohibited
security/provisioning subranges. Apply the plan-bound mask/value as a read-modify-write; a caller
cannot classify an address merely by naming it a register.

##### `write_cpu_register` and `set_execution_state`

Require a runtime-supported register in the correct category. Ordinary data/FPU registers use
`write_cpu_register`; control-flow, stack, masking, and CPU-mode registers use
`set_execution_state`, which additionally requires user permission.

##### `set_breakpoint`

Bind the current ELF artifact digest to the plan and require the requested symbol/address to fall
inside an executable ELF section. Do not treat the entire stable application partition as executable.

##### `action_batch`

All children use the same `board_id`; nested and multi-board batches are rejected. Each child is
validated against the exact direct tool schema before execution and traverses the ordinary dispatch
path.

A guarded child requires its own active immutable plan and consumes that plan exactly as a direct
call would. Batch cannot bypass permission, validation, map/identity stamps, artifact digest,
containment, locks, timeout, event recording, budget, finalizer, or cleanup.

For a static client, the agent may submit only the unchanged one-child fallback returned by an
accepted plan. The general batch surface does not authorize guessing hidden tool names.

#### Connection, Commit, and Checkpoints

| Result | Action |
| :--- | :--- |
| Connection fails | Record the exact error; do not commit unresolved identity or promote a staged pack |
| Core setup and reviewed safety eligibility succeed | Atomically commit the profile and current single-file map |
| Map missing, malformed, inconsistent, or old-schema | Run `board_safety_refresh` |
| Validation succeeds | Bind live identity and current map digest for only that board/connection |
| Validation reports MCU mismatch | Preserve the profile, report expected/observed identity, and ask the user what to do |
| Ordinary build completes | Optionally collect outputs, then create the applicable flash plan; do not refresh merely because bytes changed |
| Artifact changes after plan acceptance | Refuse before execution, invalidate/relock the plan, and require a replacement plan |

Every setup and validation attempt writes immutable structured evidence under `.firm/setup/` or
`.firm/validation/`. Reports are evidence, not live authority. Repair resumes the first unverified
same-identity setup phase after current hardware preflight; it never turns a safety failure or a
different MCU into an in-place profile rewrite.

#### Research and Retry Rules

The agent may submit only fields explicitly requested by a strict continuation schema. Target and
pack candidates must be exact, official, part-consistent, materially different from recorded failed
candidates, and deterministically validated. Research never authorizes execution, supplies allowed
ranges, changes the authoritative MCU, or promotes unreviewed evidence.

Locked targets, missing drivers, vanished probes, unsupported reviewed safety evidence, and physical
ambiguity are reported as their actual blocked/user-choice states rather than mislabeled as open-ended
research.

#### Board Validation

`board_validate` is a bounded, non-destructive live-identity and map-association gate for one assigned
connection. It does not rebuild the map, inspect build artifacts, capture UART, assert firmware
behavior, install packs, reset/halt, flash/erase, recover, or rewrite the profile.

Validation:

1. Loads the schema-v2 logical profile.
2. Resolves the intended stable probe identity from inventory and attachment hints.
3. Opens or reuses a bounded non-destructive connection.
4. Reads reviewed silicon identity and compares it with the profile.
5. Loads the single stable map and proves its identity matches the profile.
6. Creates a run-scoped live identity stamp and binds the canonical map digest separately.
7. Releases temporary resources without silently resetting the MCU.

Call validation only when no current live proof exists, connection/probe identity changed, or hardware
identity may have changed after explicit repair or destructive recovery. Do not call it merely
because of build, flash, reset/halt, UART use, refresh, or bookkeeping.

UART attachment readiness and optional console behavior belong to `get_setup_status` and planned
serial actions. Projects that do not need UART are not blocked by UART readiness; console workflows
can require the stronger `ready_for_uart_work` barrier.

On MCU mismatch, preserve the established profile and map, report expected and observed identity,
and ask the user what to do. Only an explicit user choice may enter a new-profile setup route.

#### Acceptance Criteria

- Startup uses familiar board names or the literal `no board` sentinel; users never handle JSON or internal IDs.
- Neither users nor agents provide or select `board_type`; setup resolves reviewed MCU/device support from the exact MCU part and server-hashed official datasheet.
- Custom PCB setup can reuse reviewed MCU/device support and does not require a user-created hardware definition.
- `setup_overview` supplies exact server-owned routing and one-to-one board/connection assignments.
- Profiles are logical and portable; live assignments, stamps, gates, plans, and permissions are run-scoped.
- `memory_map.yaml` is the only persisted safety-authority file below each board safety directory.
- Display names, UART settings, timestamps, report IDs, artifact paths, and ordinary firmware bytes do not stale the stable map.
- `board_safety_refresh(board_id)` rebuilds one complete map and never accepts caller ranges or build artifacts.
- Missing, malformed, old-schema, or inconsistent maps recover through refresh, not setup.
- Refresh updates a map stamp only when same-connection live identity remains valid; it never creates live proof.
- Validation performs bounded identity/map proof only and has the documented trigger categories.
- MCU mismatch cannot rewrite an established profile; adopting different silicon creates a new logical profile after explicit user choice and authorization.
- Normal `connect` is profile-only; manual overrides are plan-guarded, run-scoped, and non-persistent.
- Every flash rehashes its plan-bound artifact and checks target, segments, entry/vector, erase sectors, partition, prohibited, ROM, and unknown space before mutation.
- Changed bytes or failed containment produce zero backend erase/write calls and consume no pre-start budget or permission.
- ELF provides executable/entry/vector evidence; a selected HEX requires a matching ELF companion.
- Breakpoints require current ELF executable-section evidence.
- Build artifacts and provenance bundles remain outside `.firm`; collection grants no memory authority or gate state.
- Exact plan envelopes reject unknown/flattened fields and bind nested action parameters immutably.
- Static clients use only the exact server-generated one-child `action_batch` fallback from an accepted plan.
- Stateful UART workflows use bounded one-open `serial_exchange` when protocol state must survive.
- Successful ordinary actions preserve documented MCU state; reset-and-run is explicit.
- Destructive recovery discloses exact live identity and affected ranges and requires fresh one-time permission.

#### Contract and Verification Status

Exact MCP schemas are versioned in `tests/contracts/product-server-tools.json`; the human plan
contract is generated from `guardrails/plan_defs.py`. Historical extraction contracts and old action
lists are evidence only.

A behavior is not implemented merely because a spec, ADR, plan, or completion record says so. Before
release, the implementation must pass focused suites, the full locked test suite, Ruff, Pyright,
package build/import, bounded stdio startup/shutdown, contract synchronization, and an independent
agent-facing workflow check. Bench claims remain separately labeled.

## Canonical Server B Action Surface

The active product contract controls exact schemas.

### Always-visible ordinary actions

`connect`, `disconnect`, `get_board_info`, `get_state`, `halt`, `resume`, `step`, `reset_and_run`,
`read_cpu_register`, `read_execution_state`, `find_symbol`, `read_memory_symbol`,
`remove_breakpoint`, `wait`, and `action_batch`.

### Always-visible workflow and evidence actions

`initialization_handshake`, `setup_overview`, `load_setup_tool`, `continue_setup`,
`board_safety_refresh`, `board_validate`, `get_setup_status`, and `collect_build_artifacts`.

### Visible plan tools and hidden underlying actions

- `board_setup-plan` -> `board_setup` and paired `board_fix_setup`
- `connect_override-plan` -> `connect_override`
- `write_cpu_register-plan` -> `write_cpu_register`
- `set_execution_state-plan` -> `set_execution_state`
- `read_memory_address-plan` -> `read_memory_address`
- `write_memory-plan` -> `write_memory`
- `set_breakpoint-plan` -> `set_breakpoint`
- `flash_application-plan` -> `flash_application`
- `flash_bootloader-plan` -> `flash_bootloader`
- `register_write-plan` -> `register_write`
- `reset_and_halt-plan` -> `reset_and_halt`
- `connect_under_reset-plan` -> `connect_under_reset`
- `target_unlock-plan` -> `target_unlock`
- `read_serial-plan` -> `read_serial`
- `write_serial-plan` -> `write_serial`
- `serial_exchange-plan` -> `serial_exchange`

Legacy `flash_firmware`, unrestricted core/memory writes, public connection overrides, public
`board_safety_setup`, and `unlock_recover(confirm=true)` are not canonical actions.

## Guarded Action and Recovery Contract

### Target unlock

`target_unlock-plan` is limited to documented backend/vendor recovery mechanisms. It never permits
arbitrary security/provisioning writes. Destructive recovery uses fixed `1,0`, exact live board/MCU
identity, the mechanism, every known erased range/bank/sector, whether all nonvolatile memory is
lost, and expected loss of application, bootloader, configuration, and user data.

Research does not authorize recovery. Mass erase always requires fresh, plan-specific, single-use,
short-lived one-time permission bound to target, probe, map, mechanism, and erase ranges. Prior or
full-session approval cannot be reused. Recovery clears live identity and the gate; successful
recovery must be followed by `board_validate`.

### L1 plan envelope

Every plan begins with the complete NULL-envelope call. The populated call contains the exact
reasoning fields, budgets, nested `action_parameters`, and `user_permission` only when required.
Unknown top-level fields, flattened action fields, unknown nested fields, and populated permission on
non-permission plans are rejected.

Plans are immutable and scoped to exact action, parameters, board, Server Run, gate/session state,
budget, and permission. Replacing a plan atomically closes the old one.

Fixed `1,0` actions are `board_setup`, `board_fix_setup`, `write_cpu_register`,
`set_execution_state`, `write_memory`, `set_breakpoint`, `flash_application`, `flash_bootloader`,
`register_write`, and `target_unlock`. Flexible diagnostic actions are `connect_override`,
`read_memory_address`, `reset_and_halt`, `connect_under_reset`, `read_serial`, `write_serial`, and
`serial_exchange`; their NULL guidance supplies deterministic call and buffer bounds.

Accepted calls consume budget atomically at execution start; pre-start refusals do not. A call that
started and then failed, timed out, was cancelled, or returned inconclusive output consumes one call.

One accepted `board_setup-plan` grants one `board_setup` plus its paired first `board_fix_setup`
attempt. One-time permission applies once to each phase of that one workflow. Further attempts need a
replacement plan and, under one-time permission, a fresh user prompt.

`board_setup`, `set_execution_state`, and `flash_bootloader` accept either `one-time` or
`full-session` permission. One-time permission is consumed by the accepted underlying call.
Full-session permission only suppresses repeated user prompting for that permission-locked action and
board during the current Server Run; every use still needs a new valid plan. Destructive
`target_unlock` is never eligible for reusable full-session permission and always requires fresh
one-time approval after exact loss disclosure.

### Accepted-plan execution transport

An accepted plan returns `plan_id`, action, total calls, exact `preferred_call`, exact one-child
`stable_client_fallback`, conditional paired setup fallbacks, and instructions. Dynamic clients use
the preferred direct action after it becomes visible. Static clients may use only the unchanged
fallback returned by that accepted plan. It carries no permission and passes the identical child
dispatch checks. This is the sole exception to the no-unlisted-call rule.

Before execution the server verifies the active immutable plan, exact action/board/parameters,
remaining budget, Server Run, board/connection and gate state, required permission, artifact digest
when applicable, and all L2 safety rules. Replacement, exhaustion, invalidation, disconnect, or run
closure physically relocks the handler.

#### Always L1-Guarded

Only the corresponding plan tool is initially visible. The underlying action remains physically
locked until an accepted plan authorizes the exact board and parameters.

- Setup: `board_setup`, paired `board_fix_setup`.
- Connection: `connect_override`; manual probe/target/external-config values are run-scoped.
- CPU/execution: `write_cpu_register`, `set_execution_state`; execution-state writes require permission.
- Memory/breakpoints: `read_memory_address`, `write_memory`, `set_breakpoint`.
- Firmware/registers: `flash_application`, `flash_bootloader`, `register_write`; bootloader flash requires permission.
- Reset/recovery: `reset_and_halt`, `connect_under_reset`, `target_unlock`; destructive recovery requires fresh one-time permission.
- UART: `read_serial`, `write_serial`, `serial_exchange`.

`remove_breakpoint` remains always available. Reset actions do not unlock a protected target.

## Initialization Handshake

`initialization_handshake` is called first. It returns the current Server Run identity, the bounded
visible tool index and descriptions, the two-call plan protocol, static-client fallback rule,
setup-first routing, local-first dependency guidance, native-build/artifact workflow, and natural
language user boundary.

The agent then:

1. Asks for one unique familiar name per connected board, or the literal `no board` sentinel.
2. Calls `setup_overview` with those names; the server supplies profile/connection routes and IDs.
3. For an existing profile, loads and calls `board_validate` only when live identity proof is needed.
4. For an unknown profile, initializes `board_setup-plan`, asks for required setup permission, and
   follows the exact new-profile route.
5. Uses `board_safety_refresh` only when the server reports a stable-map problem, never merely after a build.
6. Uses profile-only `connect`; exceptional manual overrides require `connect_override-plan`.
7. Builds with the project's native validated CLI/IDE and compatible installed SDK/toolchain,
   optionally uses an appropriate Zephyr/vendor helper only as a fallback, optionally calls
   `collect_build_artifacts`, and proceeds to the applicable flash plan.

### Good example injection:

> This server intentionally hides some hardware-control tools at startup. Treat the current visible
> tool list as authoritative. Do not invent hidden calls. A visible `*-plan` is a preparation tool:
> first send its complete NULL envelope, then submit only the exact populated envelope and nested
> `action_parameters` it describes.
>
> After an accepted plan, prefer the exact returned direct call. If this client keeps static callable
> bindings and the action is unavailable, use only the unchanged one-child `action_batch` fallback
> returned by that accepted plan. Never invent or edit a hidden child; it still passes every normal
> authorization and safety check.
>
> Ask the user in ordinary conversation for one unique familiar name for each connected board, or
> `no board`. Never ask for JSON, board IDs, connection IDs, or permission enums. Call
> `setup_overview` with the familiar names and copy its server-generated routing values into later
> MCP calls. Existing profiles validate when live proof is absent; unknown profiles use the
> authorized setup route; incomplete same-identity setup uses the returned repair route.
> For new setup, ask for the exact MCU part and local official datasheet PDF, never `board_type` or a
> datasheet digest; the server resolves reviewed support and hashes the document.
>
> Normal connection is profile-only. Manual probe, target, or external-config values require the
> guarded override plan and never rewrite a profile. A live MCU mismatch is reported to the user and
> never silently adopted.
>
> Build with the native validated project workflow. Optionally normalize explicit outputs with
> `collect_build_artifacts`; collection is provenance only. An ordinary rebuild does not require
> safety refresh. The applicable flash plan binds the selected artifact, and the server rechecks its
> bytes and complete containment immediately before any erase or write.
>
> Prefer the project's compatible installed SDK/toolchain. A returned Zephyr or vendor helper is an
> optional advisory fallback, never an automatically executed command or a reason to silently install,
> replace, upgrade, or reconfigure the user's toolchain.
>
> Ordinary conversation is not permission. Ask clearly when a plan requires approval and pass only
> the allowed structured permission value. Keep every board separate, and repeat validation after
> restart, disconnect/reconnect, connection identity change, or destructive recovery.
