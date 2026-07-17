# Server B Design

## Server B Main

### Capabilities

#### Server B Layer 1

* Guards specific hardware-level tools
* Returns text prompt guides to the client agent on how to properly use hardware tools before unlocking them
* Responsible for prompt-injection to client Agent

#### Server B Layer 2

* Responsible for safeguards around hardware tools (safeguarded design, such as not letting agents write security bytes, and requiring user permission

### Structure

Server B is a local stdio MCP server, accessed by one computer with no authentication.

Server B (Guarded Hardware Server): This is a guardrail’d MCP server. It exposes direct actions to the board, but each action is guarded by a set of guardrail actions that require the Middleman or Client Agent to execute the guardrail actions first, before Server B exposes the tools in Server B. Thus any agent trying to directly access the board must go through:
- **Layer 0 — Security Setup:** Confirm immutable or locked bytes that must never be changed and establish hardware security.
- **Layer 1 — Guardrail Actions:** Require a reason, hypothesis, expected result, safety proof, and cleanup instructions before direct hardware actions are exposed.

**Server B Layer 2 — Hardware Actions:** Actions that affect the board directly, then return the result to the agent with a reminder to exit the hardware layer safely. Every Layer 2 action is guarded by a Layer 1 action.

### Server B L2 Safety

#### Server Safety

The main danger with the server and models, is, given regular SWD/JTAG and UART Read/Write abilities (no extra download programs - giving these to the agent would be accepting the risk that comes with giving the agent this capability), is flash and register write ability, which, if flashing the wrong image or writing the wrong registers, could permanently cause MCU lockout. Other capabilities may cause application crash, bootloader crash, etc., but will not render the MCU un-recoverable permanently, so we will let the agent recover the board in these cases.

We have determined a few  types of safety level actions when SWD/JTAG is enabled:

1. Flash-Application
2. Flash-Bootloader
3. Register-Write (for peripheral/config registers)
4. Memory_Access:
   1. Find_symbol
   2. Write (takes Symbol or Address)
   3. Read (takes Symbol or Address)
5. Write-CPU-Register (normal)
6. Set-Execution-State
7. Reset-Device:
   1. reset_and_run
   2. reset_and_halt
   3. connect_under_reset

These need different safety levels for these different level of actions.

##### Safety Setup

There should be a command that locks all memory/flash/SWD-JTAG write operations: Safety-Setup

This should make a memory map in the workspace of the user for the following regions using the Pack/Target + Datasheet:

1) Prohibited Registers: option bytes, UICR, OTP, or other registers that render the board in lockout memory. Most security/provisioning registers. These should be entered into the server and the server will prevent any actions that include these addresses within the range.
2) CPU Registers (from pack/target and datasheet for specific types of CPU, like cortex-M)
3) Peripheral Register Window (from pack/target and datasheet)
4) Flash (from datasheet and target/pack)
5) ROM Bootloader
6) Bootloader Flash (linker)
7) Application (linker)
8) RAM

Following flash, read/write register operations, etc., can be safe if using this memory map as a reference. A compact implementation of unsafe operations can be safe with these checks:

* MCU identity matches the selected board definition.
* Artifact segments fit entirely inside the selected partition.
* Required erase sectors fit entirely inside that partition.
* Entry point and vector table are inside the expected application or bootloader range.
* No loadable segment targets option bytes, UICR, OTP, or unknown memory.
* Bootloader flashing and application flashing are separate commands.
* Security configuration are unavailable to the ordinary agent.
  * Mass-erase must require user permission *every time*; mass erase when requesting permission should tell the user the memory range it will erase.

##### Flash-Application

flash_application
    May write only the application slot.

Treat the existing application linker script as the intended application boundary. The server only needs to check:

* Final ELF/HEX write ranges ⊆ linker-defined application FLASH region ⊆ Datasheet and Pack/Target defined application RAM or FLASH region (wherever the application is designed to run from)

That is tighter and more useful than merely checking against the MCU’s total flash range. It also prevents accidental bootloader overwrite.

This works well when:

* The linker script was established before normal application work.
* The agent normally edits source code, not the linker.
* Linker changes are treated as exceptional and reviewed separately.

Do not let the agent provide arbitrary allowed ranges per request. The board definition should supply the ranges, while the agent selects a named operation such as `flash_application`.

For custom layouts, generate the policy from the project’s authoritative linker configuration during the build, then compare it with a checked-in board layout. For example:

* linker-defined application range must equal board-policy application range

Then inspect the resulting ELF to ensure the linker actually produced segments inside that range.

##### Flash-Bootloader

flash_bootloader
    May write only the bootloader slot.
    Default:

* Return failure and ask for the agent to use `flash_bootloader-plan` with user permission first.
* `flash_bootloader-plan` takes whether the user picked access this one time or for all future calls this MCP-server live run. Only once the plan is approved does flash_bootloader stop returning a failure.

For bootloader work, use the bootloader’s linker script and confirm that its output lands in ordinary flash according to the pyOCD target/pack:

Final bootloader ELF ranges ⊆ bootloader linker FLASH region ⊆ target-reported physical flash

The pack confirms that the address is real programmable flash. The linker defines where your bootloader belongs.

Do not let the agent provide arbitrary allowed ranges per request. The board definition should supply the ranges, while the agent selects a named operation such as `flash_application`.

For custom layouts, generate the policy from the project’s authoritative linker configuration during the build, then compare it with a checked-in board layout. For example:

* linker-defined application range must equal board-policy application range

Then inspect the resulting ELF to ensure the linker actually produced segments inside that range.

##### Register-Write

The server does not need intelligence or knowledge of every board.

The model can:

1. Read the reference manual/datasheet.
2. Read the SVD.
3. Confirm that the register name, address, and field agree.
4. Submit the exact address, mask, and value.

For a compliant agent, this is a reasonable practical approach. Register writes involving flash security, option bytes, OTP, debug protection, or lifecycle configuration can simply require an explicit higher-risk task designation or be unavailable in ordinary workflows.

##### Memory (RAM) Access

Keep **one memory tool**, but make symbol use the default path.

```text
memory_access(
  action: "read" | "write" | “find_symbols”,
  symbol?: string,
  address?: integer,
  value?: ...,
  allow_address_fallback?: boolean
)
```

Behavior:

1. When given a symbol, resolve it from the ELF/DWARF and access that variable.
2. When given an address without `allow_address_fallback`, reject it with:

   > “Try a symbol first. Provide a symbol name or explicitly request address fallback.”

3. Raw-address access requires:
   * `allow_address_fallback: true`
   * a brief reason symbols are unsuitable
   * normal target-reported RAM bounds checking

The tool description should tell the model:

```text
Prefer symbol access whenever source code or debug symbols identify the
intended variable. Use raw addresses only for dynamically allocated,
pointer-derived, stack, optimized-out, or otherwise unsymbolized memory.
```

You can also let the same tool search:

```text
memory_access(
  action: "find_symbols",
  query: "motor speed"
)
```

So it supports:

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

**Set_execution state should require special permission from the user:**

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

#### Server Cleanup

For clean MCP server exit:

You only need a few simple lifecycle rules:

1. Start each external command in its own process group.
2. Give every operation a timeout.
3. On success, failure, cancellation, or server shutdown:
   * close UART and pyOCD sessions;
   * terminate the entire process group;
   * force-kill it if it does not exit.
4. Allow only one active operation per physical probe or board.
5. On server startup, clean up helper processes left from the previous run.
6. Define the intended final board state, such as `reset and run`, rather than accidentally leaving it halted.”

#### Action Cleanup

##### Mandatory Action Cleanup

Always run this on success, error, cancellation, or timeout:
stop active reads/writes
close UART
close pyOCD session
terminate owned subprocesses (see below)\*
release reset/control lines
release the board lock

This must be deterministic and must not depend on the model providing anything.

MCP cancellation tells the server to stop processing and free associated resources. A client disconnect automatically kills Server B.

**Cancellation Handling (Claude CLI only right now - cancellation notifications not supported on codex):**
For a normal tool call, the client sends:

```json
{
  "method": "notifications/cancelled",
  "params": {
    "requestId": 123,
    "reason": "User interrupted"
  }
}
```
The server should then stop the corresponding operation and free its resources. MCP clients are also expected to send this notification when their request timeout expires.

**Cancellation Handling: What the server should do:**

All MCP tool actions should be wrapped in a subprocess.\*

Associate every running operation with its MCP request ID:

request ID
    → running subprocess or pyOCD session
    → serial connection
    → board/probe lock
When cancellation arrives:
1. Mark the operation cancelled
2. Stop or terminate its worker
3. Close pyOCD
4. Close UART
5. Release the board lock
Put that cleanup in server-owned finally logic. Python explicitly recommends try/finally for cleanup when asynchronous tasks are cancelled, and pyOCD recommends closing its session in a finally block or context manager.

```python
Conceptually:
async def flash_tool(..., request_id):
    async with board_lock:
        operation = ManagedBoardOperation()
        active_operations[request_id] = operation

        try:
            return await operation.run()
        finally:
            # Runs after success, error, or MCP cancellation.
            await operation.cleanup()
            active_operations.pop(request_id, None)
```

Your MCP framework should connect the cancellation notification to cancellation of operation.run(). For example, the TypeScript MCP SDK exposes an AbortSignal to each handler specifically for this purpose.

**A disconnected stdio client automatically kills Server B.**

**Flashing caveat**
For UART monitoring, GDB servers, and ordinary debugging, cancellation can normally stop the action immediately.
For an active flash operation, a safer interpretation is:
Cancellation requested
→ let flash finish
→ close the pyOCD session
→ release the probe
Killing a flash process halfway through may leave incomplete firmware, but it should still be recoverable through another SWD flash as long as security configuration was not changed. However, we still would like to avoid incomplete firmware.
The critical requirement is to verify that the particular MCP client you plan to use actually sends notifications/cancelled when the user presses Stop. MCP supports it, but a server cannot force every client implementation to send it.

##### Optional device-specific finalizer tools

 Examples: send a UART “exit bootloader,” “stop test,” or “reset” command.

* **Different from mandatory deterministic cleanup, which is:** close UART, close pyOCD, kill subprocesses, release locks, and optionally reset/run through SWD.

The finalizer should run first when safe, followed by deterministic cleanup regardless of whether the finalizer succeeds.

```text
Process:
tool completes or is cancelled
        ↓
optional device finalizer
        ↓
close/kill/release everything
```

*Don’t accept arbitrary cleanup shell commands*

Regarding optional device specific finalizers, the best design is:

**Server-owned deterministic cleanup for every tool; optional structured device finalizers only for tools that genuinely require protocol-level exit behavior.**

You do not need a general model-provided cleanup parameter on every tool. Add it only to long-running or stateful tools such as UART sessions, custom bootloader sessions, manufacturing tests, and interactive debug operations

The server should always be able to **release its own resources without knowing every device-specific exit sequence**.

That makes the board and probe available to the next tool call. The MCU might remain:

* Halted
* Running old firmware
* Inside a custom bootloader
* Waiting for UART input
* In reset

But none of those normally prevents the next call from reconnecting through SWD or reopening UART.

**Optional structured finalizer format**

The original tool call may include something like:

```json
{
  "on_exit": {
    "action": "uart_write",
    "data": "exit\\r\\n",
    "timeout_ms": 300
  }
}
```

Or later:

```json
{
  "on_exit": {
    "action": "reset_and_run"
  }

}
```

The sequence becomes:

```text
try optional finalizer

always perform mandatory cleanup
```

The finalizer should be:

* Supplied in the original request, because the model may not get another turn after cancellation
* Best-effort and short
* Structured rather than an arbitrary shell command
* Allowed to fail without preventing resource cleanup

You can initially support no finalizers, then add `uart_write`, `reset_and_run`, or other actions as genuine use cases appear as deterministic finalizer actions.

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

Load_setup_tool
 Board_setup-plan
 Board_setup
 Board_fix_setup
 Board_safety_setup
 Board_safety_refresh
 Board_validate
 target_unlock-plan

- Board_setup-plan: the initially visible, permission-locked plan gate for setup and repair.

- Board_setup: first-time setup for a repository/board-profile pair; creates the core profile and complete safety map.

- Board_fix_setup: resumes the first unverified phase of an incomplete or failed setup.

- Board_safety_setup: performs a full safety-map build or rebuild.

- Board_safety_refresh: rebuilds only map regions affected by identifiable configuration drift.

- Board_validate: validates a completed profile against one assigned hardware connection and opens that board’s session gate.

- target_unlock / target_unlock-plan: separate action governed by its own specification. Setup and validation only report locked state.

There is no separate research provider and no user-facing terminal-command layer.

#### Session Startup, Board Assignment, and Tool Choice

At the start of every MCP Server Live Run, before board-specific work, the agent asks the user conversationally which boards are connected. The user gives each board its unique familiar name, or says **“no board.”** The user never supplies board_id, connection IDs, permission enums, or JSON.

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

Each active connection has exactly one board_id, and one board_id cannot address two active connections. Assignments and gates clear on disconnect or at the end of the MCP Server Live Run.

##### Explicit per-board startup flow

The following flow runs independently for every user-named connection:

1. Resolve the supplied name against display_name in .firm/boards/\*.yaml.

2. If exactly one matching profile exists, assign the selected connection provisionally, call Load_setup_tool, and call Board_validate only.

3. If validation succeeds, bind that connection to the profile’s board_id and name for the rest of the active session and open only that board’s gate.

4. If validation reports a hardware/profile mismatch, ask the user to correct the connection-to-name assignment. Do not rewrite or silently reassign the profile.

5. If no matching profile exists, begin the Board_setup-plan flow below.

6. If the profile exists but setup state is incomplete or failed, begin the same flow in repair mode.

A matching board name therefore means **validate, not setup**. A missing name/profile means **plan, then setup**. Setup and validation of one board never apply to another connection.

##### Board_setup-plan scope

Board_setup-plan follows the permission-locked plan flow. Setup-specific behavior:

- A setup plan is scoped to one intended connection and one logical profile. For a new profile it uses the requested display_name and a proposed/generated board_id; for repair it uses the existing board_id and recorded setup state.

- The plan’s underlying-tool parameters identify the connection, the profile, and setup versus repair mode.

- One valid setup plan permits one Board_setup call and one Board_fix_setup call. The plan’s redirect names Board_setup for a new profile or Board_fix_setup when recorded state already requires repair.

- When setup completes, validation follows and both actions are relocked behind a new plan.

- If Board_setup fails or is incomplete, the same plan’s single Board_fix_setup call remains available for the first repair attempt without asking the user again — even under one-time permission.

- Any further setup or repair attempt requires a replacement setup plan. With one-time permission the agent must ask the user again first; with full-session permission it continues without re-prompting, within deterministic retry limits.

- Setup/fix authorization also closes on disconnect of the scoped connection, user revocation, or completion or cancellation of the workflow.

##### Setup, repair, and validation sequence

When no matching profile name exists:

1. Call Load_setup_tool, then complete the Board_setup-plan flow, gathering one-time or full-session permission through normal conversation.

2. Call the exposed Board_setup once.

3. If setup completes, call Board_validate.

4. If setup fails or is incomplete, call the already-authorized Board_fix_setup once.

5. If repair completes, call Board_validate.

6. If repair cannot complete, submit a replacement setup plan — asking the user again only under one-time permission. Stop on a deterministic blocked/unresolved result or retry-budget exhaustion.

When a matching profile name exists, skip setup and repair and call Board_validate directly.

##### Mid-session gate closure

The gate closes automatically when configuration or hardware freshness is lost:

- **Fingerprint change:** compare source sub-fingerprints. Call Board_safety_refresh for isolated linker/ELF, pack, or evidence drift; call Board_safety_setup for anchor/schema changes or unclear scope. A configuration-only refresh may re-stamp an otherwise valid hardware session. Changes to the part/target or other hardware anchor require Board_validate after Safety Setup.

- **Disconnect or closed connection:** immediately clear that connection’s assignment, validation stamp, and gate. After reconnection and reassignment, call Board_validate before any guarded action becomes available again.

- **End of the MCP Server Live Run:** all in-memory assignments and gates are lost; repeat startup naming/assignment and validate every connected board.

There is no standalone open-gate action. The responsible successful validation, safety refresh, or safety setup/validation sequence reopens only the affected board’s gate.

#### Scope and Persistence

Board and run ownership is defined later. All project-specific artifacts live under .firm/:

```text
.firm/
   boards/<board_id>.yaml
   packs/manifest.yaml
   packs/files/
   setup/<setup_id>/setup_report.json
   setup/<setup_id>/setup.log
   safety/<board_id>/memory_map.yaml
   safety/<board_id>/source_manifest.json
   safety/<board_id>/safety_report.json
   validation/<validation_id>/validation_report.json
   validation/<validation_id>/validation.log
   cache/attachments.yaml
```

.firm/boards/\<board_id\>.yaml stores portable board facts, including unique board_id and display_name; its filename stem must match the internal ID. Profiles are logical, while current connection_id → board_id assignments remain in-memory only.

.firm/packs/manifest.yaml is the sole owner of external-pack URL, filename, version, checksum, and provided-target metadata. Board YAML stores no pack identifier.

The host-local attachment cache is excluded from source control. It may resolve a likely assignment but does not permanently bind a profile to physical hardware.

The safety map stores source fingerprints so the server can cheaply detect whether it is current.

The gate is never persisted as open. It is maintained separately per assigned connection and starts closed whenever that connection or the MCP Server Live Run starts.

#### Source Authority and Double Verification

Map facts retain explicit source ownership. The server must not treat user assertions, linker data, Pack/CMSIS data, and datasheet research as interchangeable.

##### User-owned data

The user supplies only:

- Board selection or custom board name.

- Exact MCU part number.

- UART baud rate.

- Physical probe/UART selection when ambiguous.

- Intended linker/build configuration when several are valid.

The user does not supply pyOCD targets, register ranges, flash geometry, or protected addresses.

##### Linker-owned firmware ranges

Firmware partitions and loadable firmware ranges come from linker/build artifacts:

- Application flash partition.

- User bootloader flash partition.

- Application and bootloader RAM allocations.

- Entry point, vector table, and loadable ELF segments.

The server parses linker scripts, linker maps, ELF files, or equivalent build metadata. The agent may help the user choose a build configuration, but must not invent partition addresses.

Pack/datasheet data defines physical memory, not the project’s application-versus-bootloader partitioning.

##### Pack/CMSIS plus datasheet-owned hardware ranges

The following require two independently obtained sources:

1. The server deterministically loads the applicable Pack/CMSIS/SVD/target data.

2. The agent supplies facts from the official datasheet or reference manual when requested.

3. The server compares them and accepts the region only when they agree or a representation difference can be deterministically reconciled.

This applies to:

- Option bytes, UICR, OTP, provisioning, lifecycle, protection, and debug-authentication regions.

- CPU and system-control register ranges.

- Peripheral register windows.

- Physical flash and RAM geometry.

- ROM bootloader/system-memory ranges.

- Erase page/sector geometry used to classify physical flash.

The agent supplies datasheet-guided information; the server supplies the Pack/CMSIS side; the user supplies neither.

The comparison checks the exact device variant, addresses, aliases, region type, bank boundaries, register-block identity, pack version, and document revision. Conflicts are recorded and affected actions remain closed.

Prohibited classifications override broader peripheral, flash, or writable classifications. Unknown memory is denied by default.

#### Field Ownership

| Field or record | Primary source | Agent research? | Persisted location |
| :--- | :--- | ---: | :--- |
| board_id | Generated or selected logical profile ID; stable and filename-matching | No | Board YAML |
| display_name | Unique user-facing board name | No | Board YAML |
| mcu_part_number | Exact user input/known-board definition | No | Board YAML |
| mcu_family | Deterministic part derivation | No | Board YAML |
| probe_family, probe_type | Selected probe inventory/mapping | No | Board YAML |
| pyocd_target | Exact detection or validated candidate | If detection is not exact | Board YAML |
| serial_baudrate | User input | No | Board YAML |
| Probe/serial hints | Built-in rules | No | Board YAML |
| test_read_address | Family default or validated candidate | Unsupported families only | Optional Board YAML |
| silicon_id_\* | Pack/CMSIS, official docs, live read | Sometimes | Optional Board YAML |
| expected_uart_substring | Optional user input | No | Optional Board YAML |
| External pack pin | Staged and verified pack | If support unavailable | Pack manifest |
| Probe/UART association | Stable local hardware identities | User confirms ambiguity | Host cache |
| Application/bootloader firmware ranges | Linker/build artifacts | No address research | Safety map |
| ROM bootloader, physical memory, registers | Pack/CMSIS plus official docs | Datasheet evidence | Safety map |

probe_type remains required compatibility/display data, for example stlink → ST-Link.

The agent never writes Board YAML, manifests, maps, reports, or cache records directly.

#### Inputs

| Situation | User provides through normal conversation |
| :--- | :--- |
| Live Run startup | Unique names of all connected boards, or “no board” |
| Known board | Existing profile name/board selection and UART baud rate |
| Custom board | Board name, exact MCU part number, UART baud rate |
| Multiple probes/UARTs | Selection from friendly descriptions |
| Unprovable external UART mapping | Confirmation that it is attached to the board |
| Multiple build configurations | Intended configuration |
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

Before research, setup inventories hardware and workspace state: user input, connected probes and serial ports, attachment-cache matches, built-in and manifest pyOCD targets, auto-detected target, and discovered linker artifacts.

| Condition | Deterministic result | Research? |
| :--- | :--- | ---: |
| No probe | setup/no-probe | No |
| Multiple probes | Agent asks user conversationally | No |
| No required UART | setup/no-uart | No |
| Multiple UARTs | Exact cache match or conversational selection | No |
| External adapter cannot be mapped | Conversational confirmation and cache | No |
| Multiple linker artifacts | Conversational build selection | No |
| pyOCD returns one exact target | Use it | Optional enrichment only |
| pyOCD returns no exact target | Target-research prompt | Yes |

Selected hardware identities belong in reports/cache, never portable Board YAML.

#### Core Board YAML

Setup creates a draft in memory:

```yaml
board_id: my_board
display_name: "My Board"
mcu_part_number: STM32L476RGT6
mcu_family: stm32l476
probe_family: stlink
probe_type: ST-Link
pyocd_target: <resolved later>
serial_baudrate: 115200
probe_hint_terms: [st-link, stlink]
serial_hint_terms: [st-link, stlink, virtual com]
```

The profile is stored at .firm/boards/my_board.yaml; the file stem and internal board_id must match. Core fields are committed after successful target support and connection. Optional test-read/silicon fields are committed only after live validation. The safety-profile reference/hash is committed only after Safety Setup completes.

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

| Support state | Action |
| :--- | :--- |
| Built into pyOCD | Continue |
| Supplied by current manifest | Continue |
| Unavailable | Return pack-research prompt |

The agent may supply one complete official pack candidate. The server stages it, computes SHA-256, compares an official checksum when available, enumerates its targets, requires the requested target, connects using the staged pack, and promotes it only after validation.

A failed candidate is recorded with observed target-listing output; the next prompt requires a materially different candidate.

#### Optional Validation Enrichment

Optional research may supply a test-read address or silicon-identity definition after the target exists. Absence of enrichment does not block core setup when required identity checks can otherwise be completed.

A test-read candidate must classify as safely readable and succeed live.

A silicon-identity candidate must match using the requested width and mask:

```text
(actual_value & mask) == (expected_value & mask)
```

Failed optional values remain out of Board YAML and are recorded with evidence, observed output, and candidate hash.

#### Safety Setup

Board_safety_setup creates the map used to classify hardware requests. It is required during first-time setup and after anchor changes.

Until it completes, write-capable actions remain unavailable or behind their plan tools.

##### Required regions

The map classifies:

- Prohibited persistent configuration/security registers.

- CPU/system-control registers.

- Peripheral register windows.

- Physical flash and erase geometry.

- ROM bootloader/system memory.

- Linker-derived user bootloader firmware.

- Linker-derived application firmware.

- Physical RAM and linker-derived RAM allocations.

###### Prohibited, CPU, peripheral, physical memory, and ROM bootloader

These use server-loaded Pack/CMSIS/SVD/target data plus agent-supplied official datasheet/reference-manual facts. The server accepts them only after deterministic comparison for the exact part.

Prohibited regions include option bytes, UICR equivalents, OTP, provisioning/lifecycle, protection, debug-authentication, and other settings that can persistently configure or lock the MCU. Most are unavailable to ordinary actions.

ROM bootloader/system memory is distinct from user firmware and is not a project flash partition.

###### Application and user bootloader firmware

These ranges are linker-owned. The server derives them from the selected linker script, linker map, ELF, and build configuration. It does not replace linker partitions with Pack or datasheet guesses.

Application and bootloader flashing are distinct action types because each is checked against a different linker-derived partition.

##### Example map

```yaml
schema_version: 1
board_id: my_board
mcu_part_number: STM32L476RGT6
pyocd_target: stm32l476rgtx
fingerprints:
  aggregate: "sha256:..."
  board_profile: "sha256:..."
  pack_cmsis: "sha256:..."
  datasheet_evidence: "sha256:..."
  linker_application: "sha256:..."
  linker_bootloader: "sha256:..."
  safety_schema: "sha256:..."
regions:
  - name: option_bytes
    kind: prohibited_registers
    start: "0x1FFF7800"
    end: "0x1FFF783F"
    verified_by: [pack_cmsis, datasheet]
  - name: rom_bootloader
    kind: rom_bootloader
    start: "0x1FFF0000"
    end: "0x1FFF77FF"
    verified_by: [pack_cmsis, datasheet]
  - name: bootloader
    kind: bootloader_flash
    start: "0x08000000"
    end: "0x08007FFF"
    derived_from: linker
  - name: application
    kind: application_flash
    start: "0x08008000"
    end: "0x080FFFFF"
    derived_from: linker
  - name: sram
    kind: ram
    start: "0x20000000"
    end: "0x20017FFF"
```

Addresses are illustrative only.

##### Safety Setup results

safety_setup_completed
 safety_setup_needs_user_input
 safety_setup_research_required
 safety_setup_incomplete
 safety_setup_conflict
 safety_setup_blocked

needs_user_input tells the agent to ask conversationally about an ambiguous build. research_required includes a datasheet prompt for the agent. Non-complete results keep affected actions closed.

#### Hardware and Configuration Freshness

Hardware identity changes rarely and is expensive to recheck. Linker, ELF, pack, and partition inputs may change repeatedly within one run. The server separates these concerns.

##### Hardware freshness

Board_setup and Board_validate establish that an assigned connection is compatible with its selected profile. Successful validation stamps that connection/session with its board_id, live MCU result, probe identity, and current aggregate safety fingerprint. Hardware freshness applies only to that connection; one board’s validation never covers another.

##### Configuration freshness

The map stores an aggregate fingerprint and source sub-fingerprints for Board YAML, part/target, Pack/CMSIS, datasheet evidence, application linker/ELF, bootloader linker/ELF, flash geometry, and safety schema.

Every write-capable guarded action checks two cheap conditions:

1. Hardware was validated during the current active session.

2. Current inputs still hash to the stamped fingerprint.

A relevant change closes the gate automatically. A purely configuration-side change does not require reconnecting while the active hardware validation remains intact.

#### Board_safety_refresh

Board_safety_refresh handles routine configuration drift:

1. Compare per-source fingerprints.

2. Identify changed source groups.

3. Rebuild only affected map regions.

4. Re-run partition-versus-prohibited overlap checks.

5. Create a new aggregate fingerprint.

6. Re-stamp and reopen the active session when hardware validation is still valid.

Examples:

- Linker/ELF change: rebuild linker-derived application, bootloader, and RAM allocation regions.

- Pack hash change: reload Pack/CMSIS-derived hardware regions and re-run required double verification.

- Datasheet-evidence change: revalidate corresponding hardware regions.

safety_refresh_completed
 refresh_scope_unclear
 safety_conflict
 safety_refresh_blocked

refresh_scope_unclear directs the agent to call Board_safety_setup for a full rebuild.

Refresh cannot reopen a gate after disconnect, server restart, target change, or lost hardware validation. Those require Board_validate.

#### Write-Gate Lifecycle

Each board’s gate is session- and connection-bound, closes automatically, and has no standalone open command. Setup, validation, or refresh reopens only the gate associated with the successfully assigned board_id and connection.

##### Group 1 — Never opened: setup-side

| State | Required action |
| :--- | :--- |
| No Board YAML/profile-name match | Run the Board_setup-plan flow, then Board_setup, its paired Board_fix_setup attempt if needed, and Board_validate after completion |
| Recorded failed/incomplete core setup | Run the Board_setup-plan flow in repair mode, then call the exposed Board_fix_setup; further attempts require a replacement plan under the one-time/full-session rules |
| Core committed; safety incomplete | Board_safety_setup, then Board_validate |
| Safety research/incomplete/conflict/blocked | Resolve the returned cause and continue Board_safety_setup |

##### Group 2 — New session

| Event | Required action |
| :--- | :--- |
| New MCP Server Live Run | Ask for connected board names and assignments, then run Board_validate for each |
| Disconnect, connection close, or connection termination | Immediately clear that assignment, validation stamp, and gate; after reconnecting/reassigning, run Board_validate before reopening |
| Compatible replacement or same board replugged | Assign the intended existing profile and Board_validate; cache matches may resolve the port silently |

Setup artifacts on disk do not restore an open gate.

##### Group 3 — Configuration drift

| Change | Required action |
| :--- | :--- |
| Firmware rebuilt; linker/ELF changed | Board_safety_refresh |
| Pack re-pinned/hash changed | Board_safety_refresh |
| Part number or target changed | Board_safety_setup, then Board_validate |
| Flash geometry, major partition model, or schema changed | Board_safety_setup; validate when the hardware/session anchor is invalidated |
| Unknown fingerprint mismatch | Board_safety_refresh; on refresh_scope_unclear, use Board_safety_setup |

##### Group 4 — Hardware and identity

| State | Required action |
| :--- | :--- |
| Live MCU mismatch | Attach correct board and call Board_validate |
| Target locked | Use target_unlock, then Board_validate |
| Backend/probe unavailable | Restore hardware/host and call Board_validate |

The part number is never changed to match an unexpected target.

#### Action Surface

The always-available and always-L1-guarded action lists are defined by the plan design.

Connect_override may accept manual values such as probe unique_id, pyOCD target, board_id, or external board_config, but these do not silently rewrite persistent profile state.

#### Type-Specific Request Validation

The map is a region/type reference, not a universal checklist or heavy per-write logging framework. Each guarded action validates only what its operation requires.

##### Examples

###### Flash_application

Parse the firmware ELF/build artifact and require all loadable flash segments and any explicit target address to fit inside the linker-derived application region. Reject bootloader, prohibited, ROM bootloader, and unknown regions. Require current session validation and fingerprint freshness.

###### flash_bootloader

Require all loadable segments and any explicit address to fit inside the linker-derived bootloader region. Reject application, prohibited, ROM bootloader, and unknown regions. Apply flash_bootloader-plan permissions.

###### write_memory

Resolve the address or symbol. An address-based RAM write must fit completely inside mapped RAM. A symbol write uses symbol/ELF metadata to determine the region and must match the requested write type. Reject prohibited and unknown regions.

###### register_write

Require the complete range to be inside a mapped peripheral-register region and outside prohibited security/provisioning subranges. A caller cannot make an address a register by labeling it one.

###### write_cpu_register

Require a recognized CPU register supported by the connected architecture/target and apply its L1 plan and permission rules.

###### Set_breakpoint

Resolve the symbol/address and require a mapped executable region supported by the target’s breakpoint mechanism.

###### Action_batch

Validate every contained action under its own rule. A batch cannot bypass freshness checks or region classification.

The same model applies elsewhere: verify that the actual target of the request matches the category claimed by the action.

Action-specific responses and errors are sufficient unless that action’s own specification requires more auditing.

#### Connection, Commit, and Checkpoints

| Result | Action |
| :--- | :--- |
| Connection fails | Record exact error; do not commit unresolved fields or promote staged pack |
| Connection succeeds | Commit core Board YAML and validated pack |
| Optional enrichment succeeds | Persist validated optional fields |
| Safety needs research/incomplete | Return agent prompt; gate remains closed |
| Core and Safety Setup succeed | Commit map/reference, relock setup/fix tools, and call Board_validate |
| Validation succeeds | Bind the assigned profile and open the gate for the active connection/current fingerprint |

Every attempt writes a structured report under .firm/setup/. Reports include inventories, selected hardware, cache outcome, target/pack resolution, research exchanges, candidate validation, connection, safety sources, fingerprints, and terminal status.

Board_fix_setup resumes the first unverified phase and reruns current hardware preflight before trusting recorded state.

| Failed phase | First step | Agent may provide |
| :--- | :--- | :--- |
| Input | Ask user conversationally | Missing user fact |
| Preflight | Re-enumerate | Nothing |
| Probe/UART selection | Present friendly choices | Choice derived from answer |
| Linker selection | Rediscover artifacts | Intended configuration |
| Target resolution | Retry exact detection | One target candidate |
| Target support/pack staging | Recheck support | One materially different pack candidate |
| Connection | Reconnect | Revised part-consistent target only |
| Validation | Rerun failed optional checks | Requested optional fields |
| Safety research | Reload Pack/CMSIS and evidence | Missing official-document facts |
| Safety map | Rebuild/compare | No arbitrary ranges |
| Commit | Retry atomic writes | Nothing |

The tool never trusts old inventory as current fact or blindly resumes a previous hardware operation.

#### Research and Retry Rules

The agent may propose only fields requested by the tool:

- Target research: pyocd_target only.

- Pack research: one complete manifest candidate.

- Validation research: requested test-read/silicon fields only.

- Safety research: official-document facts needed to compare with server-loaded Pack/CMSIS data.

The agent may not change the exact MCU part, invent linker partitions, relax prohibited regions, mark unknown memory writable, authorize hardware actions, persist state directly, or open the gate.

Every candidate is staged and deterministically validated. Separate candidate hashes and retry budgets prevent identical failed proposals. Repeated prompts include prior failures.

Locked targets, missing drivers, and vanished probes do not trigger research prompts when research cannot solve them.

#### Setup, Safety, and Refresh Outcomes

```text
setup_completed
 setup_needs_user_input
 setup_research_required
 setup_blocked
 setup_unresolved
 setup_connection_failed
 setup_validation_failed
 setup_safety_incomplete

 safety_setup_completed
 safety_setup_needs_user_input
 safety_setup_research_required
 safety_setup_incomplete
 safety_setup_conflict
 safety_setup_blocked

 safety_refresh_completed
 refresh_scope_unclear
 safety_conflict
 safety_refresh_blocked
```

Each response includes an agent_prompt explaining how to communicate or proceed without exposing server JSON to the user. The report remains the structured source of truth.

#### Board Validation

Board_validate is the repeatable Stage 0 board-health gate and is called separately for every assigned connection. It validates profile compatibility, not immutable device identity, so a compatible replacement may use an existing board_id. Success binds that connection to the profile’s name and ID for the session. Validation does not recreate Board YAML or rebuild the map.

Default validation is safe and non-destructive:

1. Load Board YAML and the matching safety map/fingerprints.

2. Re-enumerate probes and UARTs.

3. Resolve hardware through current inventory and the attachment cache.

4. Confirm target support through built-in pyOCD or .firm/packs/manifest.yaml.

5. Connect and verify live MCU identity.

6. Perform configured safe test-memory and silicon checks.

7. Open UART and optionally require expected_uart_substring within a bounded capture.

8. Confirm the safety map is complete and internally consistent.

9. Stamp the active connection/session with its assigned board_id, hardware result, and aggregate fingerprint, opening only that board’s gate.

Validation writes:

```text
.firm/validation/<validation_id>/
   validation_report.json
   validation.log
```

| Result | Meaning |
| :--- | :--- |
| validation_passed | Checks passed; session gate opened |
| validation_passed_uart_not_configured | Hardware passed; no expected UART behavior configured |
| validation_needs_user_input | Physical selection ambiguous; agent asks conversationally |
| validation_research_required | Requested validation metadata needs research |
| validation_blocked | Locked target, missing backend, unavailable pack, or invalid safety state |
| validation_failed | A configured check ran and failed |
| validation_incomplete | Required profile/safety data absent |

Ordinary validation does not install packs, flash firmware, or perform recovery. Those are explicit actions governed by their own plan rules.

#### Acceptance Criteria

- At the start of every MCP Server Live Run, the agent asks for unique names of all connected boards, or “no board”; the user handles no JSON or internal IDs.

- The server enumerates connections separately, and the agent maps each name one-to-one to a profile and connection.

- Board YAML stores unique board_id and display_name; the filename stem equals board_id.

- Profiles are logical rather than immutable physical identities; compatible replacement hardware may reuse a profile after validation.

- Session assignments, validation stamps, fingerprints, and gates are isolated per board and never apply to another connection.

- User-input and research responses explicitly tell the agent not to expose JSON, continuation IDs, or internal fields; there is no separate research provider.

- A matching display_name selects validation only; a missing profile-name match enters the Board_setup-plan flow.

- One valid setup plan permits one Board_setup call and one Board_fix_setup call; completed setup relocks both and proceeds to Board_validate.

- Exhausting the paired allowance requires a replacement plan: a new user prompt under one-time permission, none under full-session permission.

- The gate is in-memory, closes on disconnect or the end of the MCP Server Live Run, and validation opens it only for the active connection and current fingerprint.

- Every guarded write checks current-session hardware validation and configuration-fingerprint freshness; a relevant fingerprint change closes the affected gate and requires the applicable refresh or full safety-setup path.

- Board_safety_refresh rebuilds only affected groups and can re-stamp an active validated session without reconnecting; unknown scope falls back to Board_safety_setup; a lost connection requires Board_validate after reconnection.

- Part, target, geometry, major partition model, or schema changes require full safety setup.

- Application and user bootloader firmware ranges come from linker/build artifacts; ROM bootloader, physical memory, and register regions require Pack/CMSIS plus official-document double verification; the exact MCU part remains authoritative.

- Type-specific validation confirms request targets match the declared action category: application flash to application space, bootloader flash to bootloader space, RAM writes inside mapped RAM, register writes inside permitted windows; unknown memory is denied.

- Security/provisioning regions remain unavailable to ordinary actions; target_unlock remains separate and governed by its own specification.

- No research result or normal conversation authorizes a guarded action; no candidate is persisted without deterministic validation.

- All project artifacts live under .firm/; failures, research exchanges, and fingerprint changes are structurally recorded.

#### Verified and Pending Verification

Verified from current repository context:

- probe_type is a current BoardConfig runtime field used by Stage 0 output.

- Removed per-board pack metadata did not control pack provisioning.

- Target availability is based on pyocd_target and locally pinned manifest packs.

- Probe/UART association may require manual confirmation when physical mapping is ambiguous.

Pending implementation and hardware proof:

- MCP setup, safety-refresh, and validation schemas.

- Agent prompts that preserve the natural-language user boundary.

- Resumable continuations and research-response validation.

- mcu_part_number schema migration.

- Target, pack, validation, and datasheet-research handoffs.

- Pack/CMSIS versus datasheet comparison.

- Linker/ELF partition extraction and source fingerprints.

- Scoped safety refresh and in-memory session gate.

- Setup plan-tool state machine and paired setup/fix allowances.

- Attachment-cache persistence/matching and compatibility migration.

- Live-board testing across MCU and probe families.

#### Tool Boundaries

- Board_setup-plan: permission-locked plan gate for one connection/profile; each valid plan permits one setup call and one fix call.

- Board_setup: after a valid plan, establish first-time core setup and a complete safety map; on success, proceed to validation.

- Board_fix_setup: use the paired repair call after setup failure, or a newly planned allowance for later repair attempts, without repeating verified work.

- Board_safety_setup: fully rebuild the map after missing or anchor-changing inputs.

- Board_safety_refresh: cheaply rebuild map groups affected by configuration drift and re-anchor an active validated session.

- Board_validate: verify one assigned connection against its logical profile, bind its session name/ID, and open only that board’s gate for the current fingerprint.

- target_unlock: independent plan-guarded action; validate again after it succeeds.

This makes Stage 0 a repeatable hardware preflight rather than first-run-only onboarding, while allowing the firmware rebuild loop to update linker-derived partitions cheaply within one active session.

## Server B Current Action List

Server B L1 acts as guardrails for the actions in Server B L2.

### Current Server B Layer 2 Actions

Parameters without defaults are required. Every tool returns a string.

#### Session and board

1. `connect(unique_id: string | null = null, target: string | null = null, board_id: string | null = null, board_config: string | null = null)`

   Opens a persistent debug session.

   - `unique_id`: Full or partial probe ID. Falls back to `PYOCD_PROBE_UID`.
   - `target`: pyOCD target override, such as `nrf52833`. Falls back to the board config, `PYOCD_TARGET`, or auto-detection.
   - `board_id`: Board definition from `boards/\<board_id\>.yaml`. Falls back to `PYOCD_BOARD_ID`.
   - `board_config`: External board-config path. Falls back to `PYOCD_BOARD_CONFIG`.

2. `disconnect()`

   Closes the active session and releases the probe.

3. `get_board_info()`

   Returns the active board’s target, MCU/probe family, recovery policy, silicon ID expectation, UART baud rate, and smoke-test address.

#### Core execution

4. `get_state()`

   Returns the core state, such as `HALTED`, `RUNNING`, or `RESET`.

5. `halt()`

   Halts the processor core.

6. `resume()`

   Resumes processor execution.

7. `step()`

   Executes one instruction and returns the new program counter.

8. `reset(halt_after: boolean = true)`

   Resets the target.

   - `halt_after`: `true` performs reset-and-halt; `false` resets and resumes execution.

#### Registers and memory

9. `read_core_register(name: string)`

   Reads a register such as `pc`, `sp`, `r0`, or `xpsr`.

   - `name`: Core register name.

10. `write_core_register(name: string, value: string)`

    Writes a core register.

    - `name`: Core register name.
    - `value`: Hexadecimal (`0x...`) or decimal value.

11. `read_memory(address: string, word_size: integer = 32)`

    Reads one value from memory.

    - `address`: Hexadecimal or decimal address.
    - `word_size`: Transfer width; runtime accepts `8`, `16`, or `32`.

12. `read_memory_block(address: string, length: integer)`

    Reads a byte block and returns space-separated hexadecimal bytes.

    - `address`: Starting address in hexadecimal or decimal.
    - `length`: Number of bytes; must be greater than zero.

13. `read_symbol_u32(elf_path: string, symbol_name: string)`

    Resolves a symbol from an ELF file and reads its 32-bit value from target memory.

    - `elf_path`: Path to the ELF binary.
    - `symbol_name`: Symbol to resolve.

14. `write_memory(address: string, value: string, word_size: integer = 32)`

    Writes one value to memory.

    - `address`: Hexadecimal or decimal address.
    - `value`: Hexadecimal or decimal value.
    - `word_size`: Transfer width; runtime accepts `8`, `16`, or `32`.

#### Breakpoints

15. `set_breakpoint(address: string)`

    Sets a hardware or software breakpoint.

    - `address`: Hexadecimal or decimal address.

16. `remove_breakpoint(address: string)`

    Removes a breakpoint.

    - `address`: Hexadecimal or decimal address.

#### Firmware and UART

17. `flash_firmware(path: string | null = null, halt_after_reset: boolean = false)`

    Flashes firmware through pyOCD.

    - `path`: Explicit artifact path. If omitted, uses the connected board’s configured default artifact.
    - `halt_after_reset`: Whether to leave the target halted after flashing.

18. `read_serial(expected_text: string | null = null, read_seconds: number = 3.0, baudrate: integer | null = null, port: string | null = null, reset_on_open: boolean = false)`

    Captures bounded UART output.

    - `expected_text`: Optional text to look for. With `null`, any output counts as a match.
    - `read_seconds`: Capture duration; must be greater than zero.
    - `baudrate`: UART baud rate; defaults to the board configuration and must be positive.
    - `port`: Explicit serial-port override.
    - `reset_on_open`: Reset the target after opening UART to capture early boot output.

19. `write_serial(text: string, baudrate: integer | null = null, port: string | null = null, append_newline: boolean = false, timeout_seconds: number = 1.0)`

    Writes bounded UTF-8 text to the board UART.

    - `text`: Required text to send.
    - `baudrate`: UART baud rate; defaults to the board configuration and must be positive.
    - `port`: Explicit serial-port override.
    - `append_newline`: Append a newline to the transmitted text.
    - `timeout_seconds`: Write timeout; must be greater than zero.

#### Recovery

20. `unlock_recover(confirm: boolean = false)`

    Runs the board-specific unlock/recovery operation.

    - `confirm`: Must be `true` to authorize the destructive operation. Unsupported board recovery modes fail explicitly.

The extraction manifest records `_brain_sync_timeouts` as intentionally excluded, so it is **not** available in this BYO server. The implementation is in `BYO-Server/src/pyocd_debug_mcp/server.py` (approximately lines 390–1210).

## Server B Changes Proposed

### Locked Targets/Recovery

Any supported unlock or recovery operation must be planned through the separate `target_unlock-plan` MCP tool and performed through the underlying `target_unlock` MCP tool.

`target_unlock-plan` is limited to documented vendor recovery operations. It does not permit arbitrary writes to security or provisioning registers.

The tool must first be called with every parameter set to `NULL`. It must not accept a populated plan until this initial call has occurred.

The initial call returns instructions to call target_unlock-plan again with:
board_id
hypothesis
strategy
hypothesis_made
strategy_evaluated
expected_fail_return
expected_success_return
max_calls = 1
max_calls_buffer = 0
additional target-unlock parameters
user_permission

target_unlock-plan must reject plans with call-budget values other than:
max_calls = 1
max_calls_buffer = 0

When the recovery mechanism is unknown, `target_unlock-plan` may return a research prompt asking the agent to identify the documented vendor recovery procedure. The returned candidate must describe a recovery mechanism supported by the server’s underlying probe or target tooling.

Research does not authorize execution.
Before any destructive action, the tool returns a permission request containing:

* Exact live MCU and board identity.
* Proposed recovery mechanism.
* Whether the operation performs a mass erase.
* Every known memory range or partition that will be erased.
* Applicable erase banks or sectors.
* Whether all nonvolatile memory will be erased.
* Expected loss of application, bootloader, configuration, or user data.
* Plan identifier.

Example:

```json
{
  "status": "permission_required",
  "plan_id": "unlock_...",
  "operation": "mass_erase",
  "board_id": "my_board",
  "mcu_part_number": "STM32L476RGT6",
  "erase_ranges": [
    {
      "start": "0x08000000",
      "end": "0x080FFFFF",
      "description": "Entire internal flash"
    }
  ],
  "warning": "This operation will erase the bootloader and application."
}
```

If the device exposes only a full-chip erase primitive, the request must explicitly state that the entire addressable nonvolatile memory will be erased and list every known affected region.

Mass erase requires fresh user permission every time.

After permission is granted, the agent calls `target_unlock-plan` again with the complete unchanged plan and one-time user permission. The tool then closes any previous plan, activates the approved plan, changes the tool list to include the real `target_unlock`, and returns an instruction redirecting the agent to call it.

Approval must be:

* Specific to one plan.
* Single-use.
* Short-lived.
* Bound to the target identity and erase ranges.
* Invalidated if the target, probe, safety map, or plan changes.

Prior approval, general workspace permission, conversational assent, agent recommendation, or approval for another board must not be reused.

**Multi-board Configuring:**
Each action should have the target board as a parameter, its id rendered from the board id in the board yaml - for example, if I want to read out of board 1, I don’t want to read out of board 2 on accident. This way, the agent can work on multiple boards.

**Proposed Changes to memory access/writing for L1 safety:**

1. Flash-Application
   1. **Replaces flash_firmware**
2. Flash-Bootloader
   1. **Replaces flash_firmware**
3. Read/Write-Register (for peripheral/config registers)
   1. **Replaces write-memory**
4. Memory_Access Tools:
   1. **Replaces write-memory, read memory,**
   2. Find_symbol
   3. Write (takes Symbol or Address)
   4. Read (takes Symbol or Address or Block)
   5. Prefers Symbol by default, needs specific parameter override to write directly to address; limited to only RAM access
5. Read/Write-CPU-Register (normal)
   1. **Replace Read/Write core register**
   2. **Specifically prohibits security/provisioning/non-volatile registers**
6. Read/Write-Execution-State
   1. **Replace Read/Write core-register**
7. Reset-Device Tools:
   1. **Replaces specific unlock recover, and reset**
   2. reset_and_run
   3. reset_and_halt
   4. Connect_under_reset
   5. Target_unlock
      1. None of b,c,d unlock locked targets; they merely reset and start executing from the reset vector. They only reset the chip or perform a safe reset operation. If something needs to be wiped that’s locked, target_unlock is used instead.
8. Wait (ms)
9. Action-Batch
   1. Batch call of other tools all executed back to back in the order they are listed

**In summary:**

Here is the full revision of desired changes:

### Revised Actions

#### Notes

Persistent locks reset every MCP Server Live Run. When the server dies or restarts, all active plans, user-permission locks, and temporary tool unlocks reset.

User permission may be:

```text
user_permission = "one-time"
user_permission = "full-session"
```

* `one-time` applies to one underlying tool call.
* `full-session` applies to that permission-locked tool and `board_id` for the current MCP Server Live Run.

User permission is a soft gate: the client agent must obtain legitimate user approval, but Server B cannot hard-enforce it without Claude Code special elicitations, which are buggy on Codex.

Every plan and hardware action requires `board_id`. The initial all-`NULL` plan-details call is the only exception.

L1 plan tools provide reasoning scaffolding. L2 independently enforces hardware safety.

Hidden tools must also be physically locked by their server handlers. Tool-list visibility is not authorization.

#### L1 Plan Tools

L1 reasoning tools use the `*-plan` suffix and remain visible while their underlying hardware actions are hidden.

Examples:

read_serial-plan
write_memory-plan
set_execution_state-plan
flash_bootloader-plan

Plans are immutable. Submitting another valid plan for the same underlying tool and `board_id` closes the previous plan and atomically replaces it.

##### Initial `NULL` Call

Every `*-plan` description must instruct the model to call the plan tool with every parameter set to `NULL` before submitting a plan.

The server must reject any populated plan call until the all-`NULL` call has occurred for that plan tool during the current MCP Server Live Run.

The all-`NULL` response must explain:

* the purpose of the underlying tool;
* the required plan fields;
* what each field should contain;
* the additional underlying-tool parameters;
* whether the plan uses fixed `1,0` or permits multiple calls;
* any user-permission requirement;
* any additional instructions for the underlying tool.

The response instructs the model to call the plan tool again with:

```text
board_id
hypothesis
strategy
hypothesis_made
strategy_evaluated
expected_fail_return
expected_success_return
max_calls
max_calls_buffer
additional underlying-tool parameters
```

`hypothesis_made` and `strategy_evaluated` must be `true`, and the corresponding text fields must contain the actual reasoning.

##### Creating or Replacing a Plan

A populated `*-plan` call must include correctly formatted values for all required fields.

When valid, the server must:

1. Close any existing plan for the same underlying tool and `board_id`.
2. Create the new plan.
3. Bind it to the underlying tool, exact action parameters, and `board_id`.
4. Physically unlock the underlying handler.
5. Add the underlying tool to the tool list.
6. Return instructions redirecting the model to call the underlying tool.

The response should include:

```text
plan_id
underlying_tool
total_calls
instructions
```

Changing any plan field or underlying action parameter requires a complete new call to the corresponding `*-plan` tool. The new plan replaces the old plan; plans are never edited in place.

##### Plan Call Budgets

Every plan contains:

```text
max_calls
max_calls_buffer
```

The total available calls are:

```text
total_calls = max_calls + max_calls_buffer
```

`max_calls` represents expected calls. `max_calls_buffer` provides leeway for calls that execute but return empty, mistimed, inconclusive, timed-out, or otherwise unhelpful results.

The server internally tracks the remaining calls.

Each plan tool’s all-`NULL` response must state whether its underlying action is:

* fixed to `max_calls = 1` and `max_calls_buffer = 0`; or
* permitted to request multiple calls and buffer calls.

For fixed `1,0` tools, the server must reject any plan containing different values.

Every accepted underlying call consumes one call, including calls that fail, time out, are cancelled after beginning, or return no useful result. Requests rejected before execution do not consume a call.

##### Fixed `1,0` Actions

* `Board_setup`
* `Board_fix_setup`
* `write_cpu_register`
* `set_execution_state`
* `write_memory`
* `Set_breakpoint`
* `Flash_application`
* `flash_bootloader`
* `register_write`
* `target_unlock`

##### Multiple-Call Actions

* `Connect_override`
* `read_memory_address`
* `reset_and_halt`
* `connect_under_reset`
* `read_serial`
* `write_serial`

All calls within a plan must use the exact parameters declared in that plan. Different parameters require a replacement plan.

##### Permission-Locked Plan Tools

Permission-locked tools require both an active L1 plan and valid user permission.

They include:

* `Board_setup-plan`
* `set_execution_state-plan`
* `flash_bootloader-plan`
* `target_unlock-plan` when recovery is destructive

These tools follow the same all-`NULL`-then-populated flow.

After the initial all-`NULL` call, the populated plan call must include:

user_permission

When valid permission and plan fields are supplied, the plan tool unlocks the real underlying tool and returns instructions redirecting the model to call it.

##### One-Time Permission

One-time permission requires:

max_calls = 1
max_calls_buffer = 0

The permission is consumed by the one accepted underlying call.

##### Full-Session Permission

After full-session permission is granted for a tool and `board_id`, later all-`NULL` responses must state that permission is already active and that `user_permission` may be left `NULL`.

A later populated plan with `user_permission = NULL` succeeds when all other fields are correct.

Without active full-session permission, a missing, `NULL`, or invalid permission value causes the plan call to return instructions requesting valid permission and corrected fields.

Full-session permission removes only the repeated permission request. Every underlying use still requires an active L1 plan.

##### Server Enforcement

Before executing an L1-guarded action, its server handler must verify:

* an active plan exists;
* the plan authorizes that exact tool;
* the supplied `board_id` matches the plan;
* the action parameters exactly match the plan;
* the plan belongs to the current MCP Server Live Run;
* the plan has remaining calls;
* the board and session remain valid;
* any required user permission is active;
* all L2 safety checks pass.

The remaining-call count must be decremented atomically when execution begins.

When a plan is replaced, exhausted, invalidated, or the server ends, the handler must be physically relocked. The tool should also be removed from the tool list when no other active plan exposes it.

##### Always Available

These tools should remain visible:

* `Board_setup-plan`
* `Board_safety_setup`
* `Board_safety_refresh`
* `Board_validate`
* `Connect`
* `Disconnect`
* `Halt`
* `Step`
* `get_board_info`
* `get_state`
* `resume`
* `read_cpu_register`
* `read_execution_state`
* `find_symbol`
* `read_memory_symbol`
* `remove_breakpoint`
* `Reset_and_run`
* `Action_batch`
* `Load_setup_tool`
* `Connect_override-plan`
* `write_cpu_register-plan`
* `set_execution_state-plan`
* `read_memory_address-plan`
* `write_memory-plan`
* `Set_breakpoint-plan`
* `Flash_application-plan`
* `flash_bootloader-plan`
* `register_write-plan`
* `reset_and_halt-plan`
* `connect_under_reset-plan`
* `target_unlock-plan`
* `read_serial-plan`
* `write_serial-plan`

**Setup Tools**

`Board_setup-plan`, `Board_safety_setup`, `Board_safety_refresh`, and `Board_validate` require:

Load_setup_tool(board_id, tool)

before use.

`Load_setup_tool` returns the detailed setup workflow and unlocks the selected setup tool for that MCP Server Live Run.

`Board_setup-plan` follows the permission-locked flow and guards both `Board_setup` and `Board_fix_setup`. One valid setup plan permits each action once. When setup completes, both actions are relocked behind a new plan.

#### `Action_batch`

All batch children must use the same `board_id`.

A guarded child action requires an active plan and consumes one call from that plan. The batch handler must perform the same plan, permission, parameter, and L2 checks as a direct call.

Nested and multi-board batches must be rejected.

#### Always L1-Guarded

Only the corresponding `*-plan` tool is initially available. The real action becomes visible and server-unlocked after a valid plan.

##### Setup

* `Board_setup`
* `Board_fix_setup`

##### Connection

* `Connect_override`

Allows manual probe `unique_id`, pyOCD target, board definition, and external board configuration.

##### CPU and Execution

* `write_cpu_register`
* `set_execution_state`

`set_execution_state` requires user permission and is used to write PC, SP, LR, xPSR, CONTROL, PRIMASK, BASEPRI, FAULTMASK, and related execution-state registers.

##### Memory and Breakpoints

* `read_memory_address`
* `write_memory`
* `Set_breakpoint`

`remove_breakpoint` remains always available.

##### Firmware and Registers

* `Flash_application`
* `flash_bootloader`
* `register_write`

`flash_bootloader` requires user permission.

##### Reset and Recovery

* `reset_and_halt`
* `connect_under_reset`
* `target_unlock`

Destructive recovery requires user permission.

##### UART

* `read_serial`
* `write_serial`

## Initialization Handshake

Initialization Handshake - description that says to call it when the agent first connects to the MCP server. Its a prompt return that describes what the agent should do:

1. Setup
   1. Ask the user for the board_names of the connected boards, or say “no board” if no board is connected
   2. If setup was completed in a prior session for that board_name, use board_validate to unlock the write gate.
   3. Load the appropriate setup tool (Board_setup-process (user_permission=) [loaded details should detail both setup and fix setup, as well as explicit \*-process call for user permission], Board_safety_setup, Board_safety_refresh, Board_validate) before calling it.
2. Prompt injection to tell agent what the context and instructions to operate the server are.
   1. Prompt Injection:
      1. Index of the tools available (tools/list) that excludes the hidden ones, as well as what \*-process means
      2. Instruction to use \*-process for tools that are hidden - process will provide the instructions

#### Good example injection:
A good injection for initialization handshake:
“This server intentionally does not show every hardware-control tool when it starts.

Use the currently visible tool list as authoritative. Do not guess, request, or call a tool that is not currently listed.

Start by asking the user, in normal conversation, which boards are connected. Ask for a unique familiar name for each board, or “no board.” Never ask the user for JSON, board IDs, connection IDs, or permission values. 

If no board is connected, do not begin board setup, validation, or hardware actions.

For each named board, follow the setup or validation instructions returned by the visible server tools. Existing board profiles must be validated; unknown boards must be set up; incomplete profiles must be repaired. If the server reports an ambiguous physical-board choice, present its friendly choices to the user conversationally.

For each user-named board:

- If its name matches one known board profile, treat it as an existing board. Assign the matching physical connection, call `Load_setup_tool` for `Board_validate`, then call `Board_validate`. Do not run first-time setup merely because this is a new server run.

- If the known profile is incomplete or previously failed setup, treat it as a repair case. Load the setup instructions, then follow the returned repair flow.

- If its name does not match a known board profile, treat it as a new board. Load the setup instructions, begin `Board_setup-plan`, obtain the required user approval conversationally, then follow the returned setup flow. Validate the board after setup completes.

- If the physical connection or profile match is ambiguous, ask the user using only the server-provided friendly choices. Do not silently choose, rename, or rewrite a profile.

Some visible tools end in `-plan`. They are preparation tools for a currently unavailable action. When you need one, first call the *-plan with every parameter set to NULL. Follow the returned instructions exactly. If it succeeds, it will name the next tool to call and make that tool available.

Do not treat normal conversation as permission. If a preparation tool asks for user approval, ask the user clearly, then pass the resulting one-time or full-session approval only as instructed by that tool.

Keep every board separate. Never reuse another board’s validation, approval, plan, or hardware result. When a board disconnects or this server run ends, repeat the applicable setup and validation flow before using guarded actions again.”