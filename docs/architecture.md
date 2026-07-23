# BYO Firmware MCP architecture

## Composition

`firmware_mcp.server` wires the live tool handlers, project store, setup workflow,
connection manager, operation kernel, pyOCD process adapter, UART services, and
reporting. Tool behavior lives in its owning `tools` or `services` module; the
server is the composition boundary.

Profiles and durable setup evidence live under the active project's `.firm`
store. Replay reconstructs support authority and capability-aware identity
evidence; disk records do not substitute for a live silicon read. The setup workflow stores a
transient board-and-connection-bound setup run, so continuations cannot be
applied to a stale or different board.

## Live hardware correctness

`ConnectionManager` owns board-local sessions and rejects assigning one physical
connection to multiple logical boards. `kernel.operations` serializes operations
per board, preserves independent execution across boards, owns cancellation and
owned-process cleanup, and records immutable operation events.

The process-isolated provider obtains current target state, capability-aware
identity evidence, and physical memory-region facts. Raw memory, peripheral, and breakpoint work
uses those live facts for containment and read/write capability. It does not
infer board ownership from an address. Write results distinguish byte readback,
unavailable verification, and caller-selected no-readback.

Flashing parses exact ELF/AXF `PT_LOAD` bytes or Intel HEX bytes, rejects
conflicting overlap, validates every programmed byte against live writable flash,
programs sector-bounded ranges, then reads every image byte back. The canonical
digest is sorted address-plus-byte evidence. A verified program/readback result
is retained if the subsequent reset cannot be observed.

The built-in pyOCD adapter uses the same versioned JSON-lines worker boundary as
an external provider recipe. Recipe inventory and worker argv are direct argv
lists, never shell text. A worker reports a ready frame before requests; each
request and reply includes the protocol version and request ID. The parent owns
cancellation, process markers, session promotion, and finalization, so an
external provider does not become a server plugin or bypass board isolation.
See [provider-worker-protocol.md](provider-worker-protocol.md) for the wire
contract.

## Build and UART

`native_build.build_firmware` is the one direct-argv build implementation used
by both the MCP tool and CLI. It owns the process, returns argv/cwd/environment
override keys/stdout/stderr/duration/exit evidence, and discovers all nonempty
ELF, AXF, HEX, BIN, and MAP files deterministically when outputs are not
declared. Its owned build child receives closed stdin; builds obtain input only
from exact argv, cwd, and environment, never the MCP protocol stream. The
artifact collector is separate and only normalizes explicit files.

UART capture and exchange retain lossless bytes beside decoded text. Serial
writes compare the accepted byte count with the requested payload. Recovery is
provider-capability-driven and reports command acceptance separately from any
observable postcondition.

## Guard core and public contract

`guardrails.core` owns a small run-scoped permission, grant, and exact-plan
store. It atomically consumes both the matching plan action and the exact user
grant before a backend attempt, never refunds a failed/cancelled attempt, and
releases its lock before hardware work. Bindings contain current board/profile/
assignment/session identity evidence plus stable serial or parsed artifact
digest when required. Disconnect, replacement, explicit revoke/cancel, and a
changed binding invalidate the affected records.

The stable public surface includes visible guard controls alongside the direct
and guarded hardware tools; it never hides or reveals tools dynamically. The
`firmware://start-here` resource teaches detect -> configure -> permission ->
plan -> build -> flash -> verify -> debug. Flash classifies the explicit
`application` role as routine and other declared roles as one-time destructive
work; recovery selects an exact current provider mechanism and reports command
acceptance separately from observed effect/session evidence.
