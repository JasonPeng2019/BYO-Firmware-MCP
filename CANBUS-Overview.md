# CANBUS Overview

This guide explains the CANBUS update in **BYO-Firmware-MCP**, on the consolidated
`test/canbus` branch. It describes the reviewed source revision
`b98bb53f0d200f73051c4a2a69fd54b02a38c9be`. The Windows instructions at the bottom
test that exact revision so the results can be compared with its CI run.

The update provides **Classical CAN tools and software simulation**. It also adds
controls over which messages can be exchanged, records the results, and improves
how communication processes start and stop. Physical CAN connections remain
disabled pending hardware qualification.

**Highest priority: fix the three blockers to real CAN use.**

1. **Setup tiers are unsupported:** CAN connect/read/send operations require
   `setup-full`; there is no working CAN access path for `no-setup` or
   `setup-lite`.
2. **Physical hardware is disabled:** a physical CAN profile prevents the MCP
   server from starting. Capability checks below startup also refuse the normal
   physical path.
3. **Full setup does not work for a CAN-only device:** the setup path expects an
   MCU debug target, probe, silicon-ID memory read, and memory map. Physical CAN
   profiles also require debug-probe and CMSIS-pack metadata that CAN does not
   define. A CAN-only device cannot satisfy this path merely by being connected
   to the CAN bus.

These blockers prevent the advertised toolset from providing a usable physical
CAN workflow for ordinary CAN-only devices. Status and cleanup can still respond
when the server starts, and tests exercise a simulated debug context. Neither
establishes a working CAN-only setup and hardware-access path.

**The seven registered tools are also insufficient for broad product use.**
They provide status, connection lifecycle, individual raw sends, bounded reads,
and two planning operations. Fixing the blockers above is necessary, but that
interface alone does not let an agent investigate and verify a running robotics
or firmware system effectively. Background monitoring, signal-aware operations,
repeatable scenarios, combined debug evidence, regression analysis, replay,
simulated peers, and robotics protocol support remain substantial product gaps.

Sections 1-12 and the Windows test block describe the existing implementation
and its software evidence. [Section 13](#13-open-product-gaps-and-recommended-work)
records the required fixes and recommended additions; those capabilities are not
implemented by this document.

## 1. Background: where CAN fits into this project

BYO-Firmware-MCP is a server that exposes embedded development and debugging
operations to clients through the **Model Context Protocol (MCP)**. MCP is a
standard way for a client, including an AI development assistant, to discover
tools and call them with structured arguments.

The project already has concepts for selecting a target board, establishing a
debug connection, validating that connection, and planning operations. This
update connects CAN communication to that existing workflow.

**CAN**, short for Controller Area Network, lets embedded devices exchange small
messages called **frames**. A frame contains an identifier and a payload. The
identifier labels the message; it is not proof of which device sent it. The
payload is the data carried by the message.

This update supports Classical CAN data frames: standard 11-bit identifiers or
extended 29-bit identifiers, with zero to eight payload bytes. For example, a
frame with identifier `0x123` and payload `CAFE` carries two bytes, `CA` and `FE`.
CAN FD, remote-request frames, and error frames are outside this data-frame
interface. The tools exchange raw frames; they do not automatically interpret
those bytes as temperatures, motor commands, or another application protocol.

The software path is: **MCP client → server policy checks → CAN worker process →
selected communication backend → simulated peer**. A backend is the component
that actually sends and receives frames. A peer is another participant that can
send or receive them.

## 2. Seven CAN tools integrated into MCP

The update registers seven CAN tool names. They remain discoverable even when
CAN is disabled, so clients have a stable interface. Discovering a tool does not
mean that the current session is permitted to use it.

| Tool | What it does |
| --- | --- |
| `get_can_status` | Reports the configured profile, connection ownership, limits, remaining capacity, backend capabilities, and cleanup state. |
| `connect_can` | Opens CAN for an eligible, currently validated board session. |
| `disconnect_can` | Closes that board's CAN session and reports cleanup state. |
| `read_can-plan` | Records the intended read duration and maximum frame count before a read. |
| `read_can` | Collects permitted frames within the accepted plan and configured limits. |
| `send_can-plan` | Records the exact frame and timeout before a send. |
| `send_can` | Sends the planned frame if transmission is enabled and the frame is allowed. |

A read is a bounded collection, rather than a subscription that runs forever.
The global ceilings are ten seconds and 128 frames per read; a profile can set
lower limits. A send submits one frame and has a maximum configured send timeout
of half a second. These operation settings are not guarantees about the total
time spent in scheduling, process cleanup, or saving evidence.

A read result includes frame counts, elapsed time, why collection stopped, and
whether collection was truncated. Reaching the requested frame cap can be a
successful result. It means the collection limit was reached, not that the
program crashed.

## 3. Native simulation on Windows, macOS, and Linux

The update provides two software backends on all three operating systems. They
serve different testing purposes.

### The `virtual` backend: focused adapter testing

This backend uses the `python-can` library's in-memory virtual bus. It is useful
for testing frame handling and the worker's lifecycle without opening hardware.

Its bus is local to one process. Because this project runs its adapter inside a
worker process, the virtual tests put the other participant inside that worker
too. Starting a separate Python program with the same virtual channel name does
not connect it to the worker's bus.

### The `tcp_sim` backend: independent programs exchanging frames

This backend uses TCP sockets on `127.0.0.1`, the local machine's loopback address.
A small simulator accepts connections and forwards frames between participants.
The server's CAN worker and an independent test peer can therefore exchange
frames across process boundaries.

This is the main backend for the complete simulated MCP workflow. The tests
start the simulator automatically and choose an available local port. They check
exact frame contents, identifier filtering, partial TCP packet handling, empty
reads, cancellation, and loss of the simulator connection. An optional copy of
the sender's own frame is identified as transmitted traffic, rather than being
misrepresented as a response from another participant.

A successful TCP send means the local socket accepted the frame. It does not
by itself prove that a peer received it. The communication tests add that proof
by checking what the independent peer actually receives.

### Linux `socketcan_vcan`: a separate software test path

Linux also supports `socketcan_vcan`, which exchanges frames through a virtual
CAN interface in the Linux kernel. That path has its own integration tests and
requires permission to create or manage the interface. It is not a native
Windows backend. Running it inside WSL would test Linux behavior, not Windows
process and socket behavior.

None of these simulations reproduces physical wiring, electrical faults, real
CAN arbitration, or hardware bit timing. Configuring a bitrate in a software
profile does not establish that a physical bus works at that bitrate.

## 4. Configuration that stays fixed for the server run

CAN is optional. The base installation does not include its backend libraries;
the `can` dependency extra installs them. With no CAN configuration, CAN is off.
The supported modes are `off` and `available`.

An operator supplies a YAML or JSON **profile**, which is a configuration file
describing the permitted CAN activity. It identifies the backend and channel,
eligible boards, allowed receive and transmit identifiers, time and frame
limits, and evidence-retention settings. Transmission must be explicitly
enabled; an omitted transmit-enable setting defaults to false.

The server validates this profile at startup and retains a fixed snapshot.
Changing the original file afterward does not silently change the running
session's permissions. SHA-256 hashes identify the source configuration and the
normalized profile contents. Optional expected hashes let an operator require
the exact configuration they intended to run.

Unknown settings, invalid values, unsupported modes, and mismatched hashes are
rejected. Missing backend dependencies produce an installation remedy. Physical
CAN profiles are rejected at startup even if their configuration parses.

This makes a test reproducible: its evidence can identify which configuration
was actually in force. The supplied fixtures include receive-only virtual,
transmit-enabled virtual, and TCP simulator examples. The automated Windows
tests prepare their own configurations, so no manual profile editing is needed.

## 5. Board ownership and operation plans

Opening CAN is tied to an existing board session. The board must be allowed by
the CAN profile, have the project's full setup policy, have an active debug
connection, and have a current validation result. Validation is the project's
check that the selected debug session satisfies its configured requirements.

One board owns the active CAN session. A different board cannot reuse that
session merely because it knows the tool name. Requests made while the session
is opening, closing, or in an uncertain cleanup state are also restricted.

Reads and sends require an accepted **plan**. A plan records the exact action
parameters and its permitted use count. For a send, that includes the identifier,
payload, standard/extended format, and timeout. For a read, it includes duration
and frame count. The plan tools first provide guidance through an initialization
call with null fields, then accept a populated plan.

Plans do not expand the startup profile's permissions. For example, planning a
send to `0x456` does not authorize it if the profile permits only `0x123`.
Exhausted plans cannot be silently reused. The end-to-end test deliberately
tries to reuse a consumed send plan and checks that the server rejects it.

Disconnect remains available for cleanup even after the board's validation is
no longer current. Repeating disconnect is designed to be safe: callers should
not have to retain a valid operational session just to release its resources.

## 6. Message filtering, rate limits, and total-use limits

Receive and transmit permissions use exact identifier lists. The standard or
extended frame format is part of the identifier match, so matching the numeric
value alone is insufficient. Frames outside the allowed receive set are not
returned as accepted captures. Disallowed transmissions are refused before they
reach the peer.

The server also limits how much work a run can perform. Limits cover individual
read sizes and durations, transmit rate, total transmit attempts, received
frames, CAN calls, policy refusals, and capture-file count and size.

For example, a profile can allow ten transmissions per second but only one
hundred attempts over the server process's lifetime. These are separate limits:
staying below the rate does not remove the total limit.

Process-wide counters survive disconnect and reconnect, including eligible
changes of board ownership. Reconnecting therefore cannot reset an exhausted
budget. Status exposes counters and remaining capacity so clients can explain
why an operation was refused.

## 7. Saved captures and separate payload-visibility controls

A successful production `read_can` can retain an immutable capture file under
`.firm/runs/<run-id>/can-captures/`. Its response includes the artifact name,
SHA-256 hash, byte count, format, and payload policy. An **artifact** is simply a
saved output file that can be inspected later.

There are separate controls for the response sent to the MCP client and for the
saved file. The response can include full payload bytes or metadata only. The
file can contain full payloads or payload hashes. This allows a client to receive
a compact result while a local capture retains more detail for investigation.
Hashes allow comparisons without including the original payload bytes; they
are not encryption or a guarantee that small, predictable payloads stay secret.

Capture count and byte budgets prevent unlimited accumulation. A file-write
failure is reported as a failed operation, rather than as a successful saved
capture. Snapshot and capture handling also reject unsafe file-path cases such
as prohibited links. These controls do not make the local machine's owner unable
to alter files; the hashes provide evidence for integrity checks.

The end-to-end test reads the saved file back and verifies its bytes, size, hash,
profile identity, and explicit software-simulation label. Its summary preserves
the verified capture document and metadata after the temporary test workspace
is removed.

## 8. Worker isolation, timeouts, and cancellation

Backend communication runs in a separate **worker process**, rather than inside
the MCP request handler. This gives the server a resource it can stop and track
if a backend blocks or fails.

The update introduces a five-second caller startup budget covering worker
creation and readiness. Python's underlying operating-system process creation
call cannot be interrupted portably. The server therefore tracks a slow launch
separately: the caller can stop waiting, while the launch owner remains
responsible for retiring any child process that appears later.

Cancelling startup revokes permission to submit backend commands. A late child
must not begin CAN work just because process creation eventually succeeded.
Until retirement is proven, status can report `startup-pending` or unknown
cleanup instead of claiming the resources are closed.

The tests cover slow startup, cancellation during startup, creation failures
after a child exists, blocked receives, and concurrent cancellation. A specific
regression also covers a deadlock where cancellation and request retirement
waited on each other.

These are application-level timeout improvements, not hard real-time promises.
Operating-system scheduling, interpreter exit, and stalled filesystem writes
are not universally deadline-bounded by this mechanism.

## 9. Cleanup reports that distinguish success from uncertainty

Stopping a process and proving that a backend closed successfully are different
events. The worker supplies a **cleanup receipt**, a report of its close outcome.
The parent tracks that report alongside process exit and any escalation needed
to terminate the worker.

If closure cannot be confirmed, the session retains the appropriate uncertain
state instead of clearing ownership and announcing success. New bus operations
are blocked while cleanup is uncertain. A hard termination can stop a hung
process without establishing that the adapter completed a clean close.

Follow-up fixes preserve cleanup receipts when cancellation arrives late and
remove the cancellation/request-retirement deadlock. On POSIX systems, recovery
can retire an old process marker only after checking that both its process and
process group are absent; it does not blindly erase unresolved live ownership.

The shutdown tests use isolated runtime directories. This prevents unrelated
recovery markers from a developer's previous session from breaking a fresh
test. Failures also report missing scenario output directly, instead of hiding
the startup problem behind an unhelpful iterator error.

## 10. Predictable status and error responses

The five operational tools return structured JSON containing the operation,
board, success flag, status, and current CAN state. Refusals and failures include
stable codes that a client can use without guessing from prose.

A **refusal** means the request was not admitted, such as an unauthorized frame
or a busy board. A **failure** means an admitted operation failed, such as a
transport problem or an operation timeout. For example, `can/board-busy` and
`can/operation-timeout` describe different conditions.

The two plan tools use the project's textual planning workflow. Invalid MCP
arguments can also be rejected by the framework before reaching the CAN handler.
Clients should therefore not assume every response from every CAN-related tool
has the same JSON shape.

## 11. Complete simulated workflow and platform regressions

The new acceptance test exercises a real MCP client/server connection over
standard input/output, real worker processes, and real local TCP traffic. It
prepares a simulated board policy and debug probe, validates the board, connects
CAN, accepts plans, sends a frame, receives a response, verifies a capture,
rejects plan reuse, and disconnects. Finally it checks that the server returned,
its communication streams closed, and no active CAN workers or board assignments
remain.

The board policy is supplied by the test and SWD debugging is simulated. SWD is
the hardware debug connection normally used to access the board. Consequently,
this test proves the software integration but does not validate a real board,
probe, or the entire physical onboarding process.

The update also includes supporting platform fixes:

- **Windows:** worker standard handles are isolated from MCP communication;
  monitor identity creation works without a POSIX-only file operation; regression
  tests preserve concurrent registry updates and reject unsafe workspace junctions.
- **macOS:** release-evidence reading and workspace attachment avoid the failing
  `/dev/fd` path traversal; SWD worker cleanup handles exited processes correctly.
- **CI and evidence:** native test jobs run on Windows, macOS, and Linux; runtime
  output stays outside the source checkout; Windows checkout avoids unrelated
  branch names that differ only by capitalization. Retained release evidence was
  regenerated after the fixes.

These checks qualify the tested software paths. They do not establish complete
Windows support for every project feature. Native Windows workspace attachment
remains outside this qualification and uses a separate WSL workflow.

## 12. What the Windows run proves

The test block below reproduces the three Windows CI test groups. It uses native
Windows Python, automatically starts the required simulated components, and
requires no CAN hardware, Docker, WSL, or AI account.

| Group | Main coverage | Expected result at the documented revision |
| --- | --- | --- |
| CAN and MCP | Traffic, policies, virtual worker, deadlines, cancellation, captures, public MCP workflow, monitor identity | 100 tests; 97 pass and 3 skip |
| Recovery and shutdown | Process recovery, shutdown isolation, and deterministic evidence-harness regressions | 32 tests; 25 pass and 7 skip |
| Windows regressions | Concurrent registry updates and workspace junction rejection | 2 tests; both pass |

The total is **124 passed tests and 10 expected POSIX-specific skips**. An `OK`
summary is required for each group. Additional skips, errors, or failures need
investigation; they should not be counted as a clean Windows result. The selected
harness tests do not launch a live AI agent or require authentication.

Physical adapter drivers, wiring, bus timing, electrical fault behavior, and
real board/probe acceptance remain separate work. Required-mode CAN health and
terminal-proof behavior from the older Firmware-CLI project is not enabled by
this BYO update; a health-check section in a fixture is not proof of that feature.

For implementation detail, see the [CAN client contract](docs/client-contract.md#classical-can),
[server reference](docs/server-reference.md#h1-classical-can-preview-contract),
[qualification runbook](docs/EXTERNAL_QUALIFICATION_RUNBOOK.md#track-c-native-windows-and-macos-qualification),
and [CI test selection](.github/workflows/software-can.yml). The recorded CI run
for the documented revision is [Software CAN run 38690423833](https://github.com/JasonPeng2019/BYO-Firmware-MCP/actions/runs/38690423833).

## 13. Open product gaps and recommended work

### Priority: restore access first, then deliver useful agent workflows

The following three issues share the **highest priority**. They are product
blockers, not optional enhancements:

| Highest-priority issue | Required outcome | Detail |
| --- | --- | --- |
| CAN does not support all setup tiers | Working CAN access at `no-setup`, `setup-lite`, and `setup-full`, with appropriate CAN permissions and checks. | Section 13.2 |
| Physical CAN hardware is disabled | An operator can open a supported physical adapter through normal MCP startup and tool calls, exchange real traffic, and close it. Resolve both the startup rejection and the lower-level capability blockers. | Section 13.1 |
| The full-setup/profile model does not fit CAN-only devices | A CAN device can complete the applicable CAN setup without an MCU debug target, SWD probe, CMSIS pack, or MCU memory map. Debug metadata remains optional for combined CAN/debug work. | Section 13.2 |

Fix these together. Removing the full-tier restriction alone leaves debug
connection dependencies in the handlers and profiles. Removing the physical
startup exception alone leaves the capability refusals. Keeping MCU-only setup
as the only path to full setup leaves ordinary CAN devices unable to qualify.
The completion test must exercise a real CAN-only device at every setup level,
not only a CAN simulator attached to a preconfigured MCU debug session.

**Restoring access does not complete the CAN product.** The seven existing names
are five operational tools and two plan tools. They expose basic raw-frame
operations, not the complete set of workflows needed for broad robotics and
CAN firmware use. More planning or policy around the same single-frame tools
does not fill that functional gap. The agent needs to observe an active system,
understand defined messages, run experiments, correlate failures, and verify
behavior after firmware changes.

Treat the monitoring, signal-aware communication, scenario testing, combined
CAN/UART/debug evidence, regression analysis, replay, simulated peers, and
CANopen work in sections 13.3-13.9 as the next major product work. Do not reduce
that work to minor convenience wrappers or reject it solely because it requires
substantial implementation. Protocol features whose value depends on particular
devices remain conditional as described in section 13.10.

The target is a CAN integration that helps an agent investigate real robotics
systems and verify firmware behavior. Work should be prioritized by practical
value and breadth of use. A substantial addition is worthwhile when it lets the
agent complete common tasks reliably. These recommendations are not limited to
small wrappers around library functions.

The gaps below remain open. Existing simulation results do not establish that
these capabilities work, and the pinned Windows test counts below do not cover
them. New implementations will need their own tests and qualification evidence.

### 13.1. Enable physical CAN hardware access

**Current gap:** `load_can_startup()` in
[can_startup.py](src/pyocd_debug_mcp/can_startup.py) unconditionally rejects
physical profiles with this error:

```text
Physical CAN startup is disabled until a separate hardware qualification is accepted
```

The profile marks `adapter.kind: socketcan` as physical. Selecting one therefore
fails server startup before the adapter can open. Physical SocketCAN code exists,
but it is inaccessible through normal MCP startup. Neither `virtual`, `tcp_sim`,
nor Linux `socketcan_vcan` provides access to a physical bus.

There is another access blocker below startup. The default Linux inspector in
[can_socketcan.py](src/pyocd_debug_mcp/adapters/can_socketcan.py) reports bounded
physical operations and transmit quiescence as `UNKNOWN`, while
[can_capabilities.py](src/pyocd_debug_mcp/can_capabilities.py) requires positive
support for the relevant operations. Removing the startup rejection alone would
still leave the normal physical path refused by those checks.

**Required work:** replace the blanket rejection with a supported physical CAN
workflow. Let an operator select an authorized adapter and channel, inspect its
capabilities, connect, exchange traffic, and disconnect through MCP. Retain the
profile permissions, operation limits, and cleanup reporting. Review the
capability checks against real adapters; removing the startup exception alone
does not establish working hardware support.

Record the tested operating systems, adapters, drivers, and bus settings. Verify
exact transmit/receive traffic with an independent physical peer, receive-only
operation, failure reporting, cancellation, disconnect, and reconnect. Replace
the existing test that requires blanket physical rejection with tests for the
supported access path and specific unsupported configurations. Keep simulation
and physical-hardware evidence clearly identified.

### 13.2. Support every setup level with CAN-appropriate profiles

**Current gap:** the server requires `setup-full`, an active debug connection,
and current board validation before opening CAN, reading, or sending. This also
applies to the software backends. Status and disconnect have separate rules so
cleanup does not require current validation. The operating prerequisites prevent
ordinary CAN inspection of a motor controller, sensor, or assembled robot that
has no debug probe attached.

The coupling goes beyond the full-tier guard. CAN handlers obtain probe identity
from the active debug connection. The physical CAN configuration requires
nonempty `probe_uids` and a `debug_attachment` containing a pyOCD version and
CMSIS-pack metadata. Those fields are required even when the intended operation
is simply to open a CAN adapter and collect traffic. See
[can_config.py](src/pyocd_debug_mcp/can_config.py) and
[can.py](src/pyocd_debug_mcp/tools/can.py).

#### Remove mandatory MCU debug metadata from CAN configuration

**Architectural gap:** CAN itself defines neither CMSIS packs nor MCU
debug-probe IDs. The integration imposes those requirements; they are not
missing CAN facts that an operator needs to discover or supply.

**Required fix:** remove mandatory debug `probe_uids`, `debug_attachment`,
pyOCD-version, and CMSIS-pack prerequisites from standalone CAN configuration
and connection checks at every setup level, including `setup-full`. Keep MCU
debug metadata only as an optional association when the operator also uses that
device through a debug interface. Validate that interface separately.

A CAN adapter's serial number or backend identifier is distinct from an ST-Link
or J-Link debug-probe ID. A protocol-specific node ID, such as a CANopen node ID,
is another distinct fact. Do not repurpose `probe_uids` to mean either of these
or require placeholder pack/probe values to pass configuration validation.

Acceptance must include a CAN-only profile with no debug probe, pyOCD target,
CMSIS pack, or debug attachment at each setup level. The selected adapter and
CAN operating policy must still be checked. A separate combined CAN/debug case
must preserve the actual debug interface's own requirements.

The existing MCU setup workflow does not become a CAN setup workflow when CAN
is enabled. It still resolves a debug target and validates the attached MCU.
That result does not validate a CAN node, its message definitions, or its
association with a particular device on the bus.

#### Existing setup requirements that do not transfer directly

The schema-v2
[board profile](src/pyocd_debug_mcp/firmstore/profiles.py) requires MCU and debug
facts. The [live validator](src/pyocd_debug_mcp/setup_flow/validate.py) selects a
debug probe, confirms target support, connects through the debug backend, reads
a reviewed silicon-identity address, and associates a memory safety map. Lite
setup also uses confirmed memory regions and flash/recovery facts.

| Existing requirement | Applicability to CAN |
| --- | --- |
| MCU part number/family and debug target such as `pyocd_target` | Relevant when debugging that MCU; often unknown or irrelevant when using a finished CAN device. |
| Debug probe selection and an active debug connection | Relevant to SWD/debug operations; not a prerequisite for using a CAN adapter. |
| CMSIS packs or other debug-target support metadata | Describes debug and flash support; does not define a CAN protocol or establish that a CAN node works. |
| Silicon-ID memory reads and a memory safety map | Appropriate for direct MCU memory access; unavailable over generic CAN and insufficient to establish which node produced a frame. |
| RAM/flash/peripheral regions, erase geometry, and recovery methods | Relevant to debug/flash operations; ordinary CAN traffic is governed by messages, commands, and device behavior instead of memory address containment. |
| UART configuration and readiness | Optional context for combined investigations; not a condition for CAN readiness. |

These profiles still fit a custom MCU board that is connected to both a debug
probe and CAN. They do not fit a general CAN adapter, an entire bus, or a
CAN-only sensor or motor controller. A device may offer protocol-specific memory
or firmware services, but generic CAN does not supply the existing debug API.
Do not require invented MCU, pack, probe, memory-map, or UART values to make a
CAN-only device pass setup.

#### Required tier behavior

CAN must be usable at `no-setup`, `setup-lite`, and `setup-full`. More setup should
provide better confirmed context and additional checks. It must not make MCU
debug setup a prerequisite for all CAN communication.

| Setup level | Intended CAN behavior |
| --- | --- |
| `no-setup` | Permit explicit raw CAN operations after selecting the adapter/channel and bus settings and satisfying CAN permissions and limits. Require neither a committed MCU profile nor SWD validation, DBC, or known device identity. Report that device identity and message meaning have not been established. |
| `setup-lite` | Use confirmed CAN connection and device/protocol facts where supplied. Offer guarded operations based on those facts and explicit raw routes consistent with the existing lite model. Do not claim live device identity merely because configuration was confirmed. |
| `setup-full` | Use reviewed CAN definitions, command constraints, supported device procedures, and the live protocol/device checks that the hardware can actually provide. Retain planned operations and evidence. Require MCU debug validation only for operations that use the debug interface. |

These are intended CAN semantics, not functionality already implemented. The
availability of FD, RTR, DBC, or CANopen should depend on the actual backend,
definitions, and device capabilities, not on an unrelated MCU setup gate. A
device that cannot report an identity must not lose all CAN access; record the
limitation and restrict only operations whose prerequisites actually need that
proof. Preserve existing handling of corrupt policy and explicit refusals.

Update tier routing, capability discovery, status guidance, plan prerequisites,
handler dependencies, and configuration together. The current route inventory
has planned CAN reads/sends but no registered raw CAN counterparts. Either add
the appropriate raw/safe routes or make the existing CAN routes tier-aware;
never direct the agent to nonexistent tools. Discovery must report the same
prerequisites that dispatch enforces, including CAN being off, unconfigured,
disconnected, or unsupported on the selected backend.

#### Separate CAN connection, device, and debug information

The profile model needs to represent these distinct subjects:

- **Adapter and connection:** backend, channel, adapter identity as available,
  nominal/data bitrates, frame formats, controller capabilities, and bus state.
  The current physical schema requires USB vendor/product IDs and a serial
  number. Support appropriate identity facts for other supported adapters too,
  including non-USB CAN controllers; do not manufacture USB identity values.
- **CAN operating policy:** authorized receive/transmit traffic, command and
  payload constraints where known, rates, durations, retention, and task
  ownership. An MCU memory map does not substitute for this policy.
- **Device or node definitions:** logical role, protocol, node addressing where
  the protocol defines it, message definitions such as DBC or EDS/DCF, units,
  supported commands, expected feedback, and available product/serial/firmware
  identity checks. Multiple nodes may share one bus. A CAN identifier is a
  message label, not universal device identity or authentication.
- **Optional MCU debug association:** an existing board profile, debug probe,
  memory map, and firmware artifact when the same device is also being debugged
  or flashed through that interface.

Keep confirmed configuration, observed communication, protocol-reported device
identity, and MCU silicon validation distinct in status and evidence. Binding a
CAN session to a logical `board_id` does not prove that all observed traffic came
from that board. Reuse the existing profile store and tier concepts where they
fit, but give CAN its own applicable facts and validation path. CAN-only setup
must not need to complete the MCU setup phases. Combined CAN/SWD work should
link the two contexts without treating proof from one interface as proof of the
other.

### 13.3. Complete frame support and bus diagnostics

**Current gap:** the MCP operations expose only Classical CAN data frames with
11-bit or 29-bit identifiers and zero to eight payload bytes. The service rejects
FD, remote-request, and error frames from accepted captures. Some internal frame
records already represent these flags, but that does not expose them through the
tools. Backend capabilities and cleanup status are not a complete bus diagnostic
interface.

**Recommended work:**

- Support CAN FD through the normal send/read interface, including payloads up
  to 64 bytes, bitrate switching, nominal/data bitrate settings, and adapter
  capability checks. Update profiles, plans, adapter translation, worker
  messages, capture formats, and tests together. Extend simulation coverage
  without treating it as physical FD qualification.
- Support Classical CAN remote-request frames as a frame option. Preserve the
  requested DLC independently of payload bytes and reject invalid combinations
  such as FD plus RTR. RTR is a compatibility feature, not a separate protocol
  stack.
- Report controller state and available error events and counters. Distinguish
  a quiet bus from an observed controller or transport failure. Identify
  unsupported or unavailable diagnostics instead of assuming a healthy bus.
  Error notifications must not disappear under ordinary data-frame filtering.
  Electrical fault injection is separate work.

These features have concrete robotics uses. For example, the
[ODrive protocol](https://docs.odriverobotics.com/v/latest/manual/can-protocol.html)
supports CAN FD and uses RTR for certain requests. Its cyclic feedback also
illustrates why captures must handle more than occasional individual frames.

### 13.4. Add background monitoring and searchable captures

**Current gap:** `read_can` collects frames during one tool call, with global
ceilings of ten seconds and 128 returned frames. The CAN service holds
its operation lock throughout a read, so sends through that service wait for the
read to finish. There is no persistent monitoring workflow that an agent can
start, leave running while performing other work, and query afterward. At 100
frames per second, 128 frames represent only about 1.28 seconds of traffic.

**Recommended work:** add managed background capture with start, status, query,
and stop operations. Let collection continue while the agent sends commands,
flashes or resets a board, or inspects debug state. Coordinate reception so
captures, protocol handlers, and response checks can observe the traffic they
need without consuming one another's messages.

Retain larger captures in files under explicit duration and storage budgets.
Return useful summaries and selected time ranges to the agent rather than
putting the entire bus log in every response. Support queries by identifier,
direction, time, payload change, and decoded signal where definitions exist.
Report message rates, gaps, capture truncation, and observed dropped-frame
counts; distinguish unavailable loss information from a confirmed zero count.
Track the capture owner and ensure cancellation, disconnect, and shutdown retire
the collection resources.

### 13.5. Add signal-aware communication

**Current gap:** tools exchange hexadecimal payloads without message or signal
definitions. The agent cannot inspect a DBC through the CAN interface, send a
named message using signal values, or query decoded feedback.

**Recommended work:** integrate DBC loading, message discovery, signal
inspection, encoding, and decoding using an existing library. Expose signal
names, units, scaling, enumerated states, and declared limits. Apply definitions
to live traffic and saved captures. Associate the selected definition file and
its hash/version with results so a later investigation can reproduce decoding.
Bind planned sends to the actual encoded frame as well as the named inputs.

Keep raw CAN usable without a DBC. Provide documented device adapters or other
explicit definitions for protocols that do not use DBC. Unknown payloads should
remain unknown until their interpretation is supported by a definition or
verified protocol knowledge.

### 13.6. Add repeatable scenarios and firmware tests

**Current gap:** the public tools provide individual sends and reads. They do
not provide a reusable scenario that coordinates traffic, timing, expected
responses, periodic transmission, and test evidence.

**Recommended work:** let an agent define, execute, save, and rerun scenarios
that can:

- Begin observing traffic before sending a request, then match a fresh response
  within a specified timeout without accepting a stale queued response.
- Send sequences and periodic traffic while continuing to receive feedback.
- Check expected identifiers, payloads, decoded values, heartbeat intervals,
  state transitions, and response timing.
- Report pass, fail, cancellation, and incomplete observation distinctly, with
  the relevant frames and measurements attached.
- Run again after a firmware change and support automated regression testing.

The server or worker must execute schedules and response matching. Repeated
agent tool calls are not a timing mechanism. Record measured timing and backend
limitations rather than promising hard real-time execution. Bind scenario
messages, rates, durations, and parameters to the configured permissions and
accepted plans. Stop active transmissions on cancellation, disconnect, or owner
shutdown and report cleanup outcomes.

### 13.7. Correlate CAN with debugging and compare firmware behavior

**Current gap:** CAN captures retain profile and runtime identity, but there is
no dedicated workflow for correlating CAN behavior with UART output, flashing,
resets, breakpoints, or inspected firmware state. There is also no built-in
comparison of captures from different firmware versions.

**Recommended work:** preserve a shared investigation record containing the
selected board, firmware artifact identity, definitions, test configuration,
and timestamped CAN/UART/debug events. Record clock sources and alignment
uncertainty; tool completion times alone cannot establish exact event timing.
Let the agent retrieve the traffic around a reset, fault, or debug event.

Add baseline comparisons for active identifiers, message rates, missing
heartbeats, new fault states, response latency, and decoded signal behavior.
Keep the underlying evidence accessible and distinguish an actual behavioral
change from different configuration, missing payloads, capture limits, or
incomplete observations. This is a substantial agent-facing analysis capability,
not just a longer list of raw frames.

### 13.8. Add replay and simulated peers for reproducible tests

**Current gap:** the TCP simulator forwards raw frames between participants. It
does not provide a reusable device model, selective capture replay, or a
workflow that compares a device's new responses with a recorded baseline.

**Recommended work:** let scenarios replay selected peer traffic with explicit
timing, identifier/direction selection, duration, and stop behavior. Separate
traffic to inject from responses that the device under test must produce; do
not replay both sides of a recorded exchange as if the device passed the test.
Record actual replay timing and any traffic omitted or altered.

Support simulated sensors, supervisory controllers, or other boards that can
respond to requests, publish feedback, and exercise communication failures such
as a missing heartbeat. This lets the agent test firmware when the complete
robot is unavailable. Define the limits of each model: frame replay and software
peers do not reproduce physical arbitration, wiring faults, or electrical timing.

### 13.9. Add CANopen support for robotics workflows

**Current gap:** there is no CANopen-aware MCP interface. Raw frame access alone
does not provide object-dictionary inspection, device commissioning, or
meaningful CANopen fault and drive-state reporting.

**Recommended work:** provide a separately enabled CANopen module using an
established stack. Plan substantive support for node inspection, EDS/DCF and
object dictionaries, parameter access through SDO, PDO configuration/monitoring,
network management, heartbeats, emergency messages, and relevant motor-drive
states such as CiA 402. Qualify supported procedures against real devices and
document which device profiles are covered.

CANopen is a meaningful robotics feature, not something to reject merely
because the integration is large. The
[ROS 2 CANopen framework](https://github.com/ros-industrial/ros2_canopen)
provides a concrete example of its use in this audience. A separate module keeps
its configuration out of projects that use other protocols; it does not make
the feature unimportant.

### 13.10. Keep device-dependent additions conditional

ISO-TP and UDS should be added when supported hardware needs their transport,
diagnostic, configuration, or firmware-update services. Their priority rises
when those are explicit target workflows. Vendor-specific update procedures
also need a defined device-support strategy; protocol availability alone does
not supply a manufacturer's update sequence.

Electrical fault injection and conformance testing are separate capabilities
that may need specialized hardware. Automatic analysis of undocumented payloads
can assist investigation, but proposed interpretations must not be presented
as verified signal definitions. These areas need concrete user/device demand
rather than inclusion solely because a library exists.

### 13.11. Reuse existing libraries and define completion evidence

Most underlying functionality already exists. The product work is making it
cooperate through MCP, share session and receive ownership correctly, preserve
permissions, and return trustworthy evidence.

| Capability | Existing implementation to reuse |
| --- | --- |
| CAN hardware, FD/RTR representation, periodic traffic, listeners, logs, and playback | [python-can](https://python-can.readthedocs.io/en/stable/), already an optional dependency here; support varies by backend and driver |
| DBC definitions, signal encoding/decoding, plotting, and message expectations | [cantools](https://cantools.readthedocs.io/en/stable/) |
| CANopen communication and object dictionaries | [canopen](https://canopen.readthedocs.io/en/stable/) |
| ISO-TP transport, when needed | [can-isotp](https://can-isotp.readthedocs.io/en/latest/) |
| UDS services, when needed | [udsoncan](https://udsoncan.readthedocs.io/en/latest/) |

A representative acceptance workflow should use real hardware to flash a known
firmware artifact, observe startup, verify the heartbeat and its rate, send a
request, decode the response, execute periodic test traffic while capturing
feedback, and save the result with firmware/configuration/definition identities.
It should also exercise standalone CAN without a debug probe at all three setup
levels, including a CAN-only device with no MCU debug profile. Verify discovery
and dispatch agree and that a full MCU profile does not imply proven CAN-node
identity. Cover multiple device definitions on one bus and the identity model
of each supported adapter. Capture comparison, replay, simulated peers, and
CANopen procedures need additional acceptance cases that prove their own
behavior.

Include failure, cancellation, resource-limit, and cleanup cases at the service
and public MCP boundaries. Qualify each advertised hardware/platform combination
and retain software-only results separately. Updating this document does not
enable any of these capabilities or change the historical Windows results below.

## Windows test instructions — one copy-and-paste block

The block is divided into numbered sections. Sections 1–2 prepare an isolated,
reproducible environment; sections 3–5 run the tests; sections 6–7 explain the
evidence and retain the results. Run the whole block in order, because later
sections use the variables and environment prepared earlier.

```powershell
# Run in a NEW native Windows PowerShell window, from a writable local folder.
# Use an ordinary directory, not a symlink or junction. No administrator rights
# are required for the simulator tests. Internet access is needed for downloads.
#
# Prerequisites: Git for Windows and uv must be installed and on PATH.
# Git: https://git-scm.com/downloads/win
# uv:  https://docs.astral.sh/uv/getting-started/installation/
# The recorded CI used uv 0.12.0 and Python 3.12. Record your uv version below;
# use uv 0.12.0 if you need the same dependency-management tool version as CI.
# If either tool was just installed, reopen PowerShell before pasting this block.
#
# This creates a fresh clone, pins the reviewed revision, installs dependencies,
# runs all three Windows test groups, and saves logs outside the checkout.
# It stops on a failed command. It does not alter an existing repository clone.

& {
    # SECTION 1 — Check prerequisites and isolate the run
    # This prepares the test environment; it does not test CAN communication.
    # Verify native Windows and the required tools, then use fresh directories
    # so old recovery markers or captures cannot contaminate this run.
    $ErrorActionPreference = 'Stop'
    if ($env:OS -ne 'Windows_NT') { throw 'Run this block on native Windows.' }
    Get-Command git -ErrorAction Stop | Out-Null
    Get-Command uv -ErrorAction Stop | Out-Null

    function Invoke-CanCommand {
        param([string]$Program, [string[]]$Arguments)
        & $Program @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$Program failed with exit code $LASTEXITCODE. See the transcript."
        }
    }

    $canRunId = [guid]::NewGuid().ToString('N')
    $canCheckout = Join-Path (Get-Location).Path "BYO-Firmware-MCP-canbus-$canRunId"
    $canTestRoot = Join-Path $env:TEMP "byo-canbus-$canRunId"
    $canRevision = 'b98bb53f0d200f73051c4a2a69fd54b02a38c9be'
    New-Item -ItemType Directory -Path $canTestRoot | Out-Null

    $env:PYOCD_MCP_RUNS_ROOT = Join-Path $canTestRoot 'owned-runs'
    $env:BYO_CAN_ACCEPTANCE_EVIDENCE_DIR = Join-Path $canTestRoot 'evidence'
    Start-Transcript -Path (Join-Path $canTestRoot 'windows-tests.txt')
    try {
        # SECTION 2 — Select the source and install the test environment
        # Pin the reviewed source and install its locked dependencies on Python
        # 3.12. Recording the commit, lockfile hash, and tool versions makes a
        # failure reproducible. Installation success alone is not a CAN pass.
        Write-Host 'SECTION 2: Source revision and test environment'
        # Single-branch cloning avoids historical refs that differ only by case.
        Invoke-CanCommand 'git' @(
            'clone', '--single-branch', '--branch', 'test/canbus',
            'https://github.com/JasonPeng2019/BYO-Firmware-MCP.git', $canCheckout
        )
        Set-Location $canCheckout
        # Detached HEAD intentionally selects the exact revision covered here.
        Invoke-CanCommand 'git' @('switch', '--detach', $canRevision)
        Invoke-CanCommand 'git' @('rev-parse', 'HEAD')
        Invoke-CanCommand 'uv' @('--version')
        Write-Output ([System.Environment]::OSVersion.VersionString)
        $PSVersionTable | Out-Host
        Get-Date -Format o
        Get-FileHash -Algorithm SHA256 -Path 'uv.lock' | Format-List

        Invoke-CanCommand 'uv' @('python', 'install', '3.12')
        Invoke-CanCommand 'uv' @(
            'sync', '--locked', '--python', '3.12', '--extra', 'can', '--group', 'dev'
        )
        Invoke-CanCommand 'uv' @('run', '--locked', 'python', '--version')

        # SECTION 3 — CAN communication, policy, and the complete MCP workflow
        # Test real local TCP frame exchange and the process-local virtual bus.
        # Check exact identifiers/payloads, receive filtering, empty-read
        # timeouts, lost connections, cancellation, and slow worker startup.
        # Check transmit permissions, quotas, board/session ownership, canonical
        # tool responses, and refusal of invalid or unsupported configuration.
        # The MCP scenario validates a simulated board, plans a send/read,
        # exchanges frames, verifies the saved capture, rejects plan reuse,
        # and checks clean shutdown. The board/probe remain simulated.
        # Also check Windows monitor identity creation without POSIX fchmod.
        # Expected: "Ran 100 tests" and "OK (skipped=3)".
        Write-Host 'SECTION 3: CAN communication, policies, and MCP acceptance'
        Invoke-CanCommand 'uv' @(
            'run', '--locked', '--extra', 'can', 'python', '-m', 'unittest', '-v',
            'tests.test_can_native_sim',
            'tests.test_can_mcp_sim_e2e',
            'tests.test_can_config_gate',
            'tests.test_can_worker_virtual',
            'tests.test_can_session',
            'tests.test_can_tools',
            'tests.test_can_server_contract',
            'tests.test_monitor_paths.MonitorStoreRootTests.test_identity_persists_without_optional_fchmod'
        )

        # SECTION 4 — Recovery, shutdown, and trustworthy test evidence
        # Check process ownership/recovery and cancellation through the server.
        # Verify that tests use their own runtime state, and that failed startup
        # produces useful diagnostics instead of an unrelated iterator error.
        # Check how the evidence harness counts completed/failed tool events
        # and retires its server. These deterministic harness tests do not
        # require a live AI agent or an account. POSIX-only cases skip on Windows.
        # Expected: "Ran 32 tests" and "OK (skipped=7)".
        Write-Host 'SECTION 4: Recovery, shutdown, and evidence-harness regressions'
        Invoke-CanCommand 'uv' @(
            'run', '--locked', '--extra', 'can', 'python', '-m', 'unittest', '-v',
            'tests.test_process_cleanup',
            'tests.test_can_shutdown',
            'tests.test_monitor_codex_e2e.HarnessEvidenceTests'
        )

        # SECTION 5 — Windows registry concurrency and workspace junctions
        # Check that simultaneous process registrations preserve all acknowledged
        # entries in the project's probe registry (not the Windows OS Registry).
        # Check that a workspace junction is rejected without writing outside
        # the intended workspace. A junction redirects a directory to another
        # location. These are supporting Windows regressions, not CAN traffic
        # tests or proof of complete native Windows workspace attachment.
        # Expected: "Ran 2 tests" and "OK", with no skips.
        Write-Host 'SECTION 5: Windows registry and workspace-junction regressions'
        Invoke-CanCommand 'uv' @(
            'run', '--locked', '--extra', 'can', 'python', '-m', 'unittest', '-v',
            'tests.test_remote_probes.LoadSaveTests.test_cross_process_registrations_do_not_lose_acknowledged_entries',
            'tests.test_tiered_acceptance_workspace.TieredWorkspaceAcceptanceTests.test_native_windows_workspace_junction_is_rejected_without_external_writes'
        )

        # SECTION 6 — Inspect the retained MCP capture and cleanup report
        # Section 3 already asserts capture contents, file size/hash, and cleanup.
        # Here, require its summary file to exist and display it for review.
        # This is inspection of the completed scenario, not another test group.
        # Look for the expected received frame, rejected plan reuse, closed
        # streams, and empty worker/board lists. Physical hardware must be false.
        Write-Host 'SECTION 6: Retained capture and shutdown evidence'
        $canEvidencePath = Join-Path $env:BYO_CAN_ACCEPTANCE_EVIDENCE_DIR 'mcp-simulation-e2e.json'
        if (-not (Test-Path -LiteralPath $canEvidencePath)) {
            throw 'The expected MCP simulation evidence file is missing.'
        }
        Get-Content -LiteralPath $canEvidencePath
        # Expected evidence values:
        # physical_hardware: false; setup_policy: "fixture-seeded"
        # validation_status: "validation_passed"
        # capture.backend: "tcp_sim"; capture.software_simulation: true
        # capture.frames[0]: arbitration_id 801 (0x321), data_hex "50415353" (PASS)
        # plan_reuse_refused: true; stdio_closed: true
        # shutdown.main_returned: true; shutdown.can_phase: "closed"
        # shutdown.active_can_workers: []; shutdown.assigned_boards: []
        # A truncated capture with stop_reason "frame_cap" is expected here:
        # this scenario deliberately asks to collect just one frame.

        Write-Host 'All three test commands passed. Check the expected skip counts above.'
        Get-Date -Format o
    }
    finally {
        # SECTION 7 — Retain results, including when a command fails
        # Stop the transcript and print the output locations. Saving a log does
        # not mean the run passed: require all three OK summaries, the expected
        # skip counts, and the evidence file before accepting the result.
        Stop-Transcript
        Write-Host "Checkout: $canCheckout"
        Write-Host "Saved log and evidence: $canTestRoot"
    }

    # Keep windows-tests.txt and evidence/mcp-simulation-e2e.json for review.
    # Copy them out of TEMP if you need long-term retention.
    # On a failure, preserve the log and the first failing test's traceback.
    # Do not treat unexpected skips or missing evidence as a pass.
    # To rerun, paste the whole block again; it creates a new isolated run.
}
```
