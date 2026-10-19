# BYO Firmware MCP

BYO Firmware MCP is a local, stdio-based MCP server for guarded embedded-board
setup, inspection, serial I/O, debug control, firmware deployment, and recovery
through pyOCD.

## Current state

The server implementation lives in `src/pyocd_debug_mcp`. It is distributable
without bundled board profiles, firmware, packs, generated reports, or hardware
state. Board-specific state is created in the firmware project's `.firm/`
directory, never in this server checkout.

The server is intentionally headless and client-neutral. It exposes its current
tool surface through the MCP protocol; a visible tool is not, by itself,
authorization for a guarded operation.

## C/C++ code analysis

Five C/C++ analysis tools are available without a board or setup:
`get_code_analysis_status`, `find_code_definition`, `find_code_references`,
`get_code_hover`, and `get_code_diagnostics`. They read saved files using one
selected native build's `compile_commands.json`. Start with status; see
[analysis setup and recovery](SERVER_GUIDE.md#cc-code-analysis).

Source launches resolve clangd from development PATH. Managed launches require
the accepted runtime manifest mapping; that packaging prerequisite is still
unresolved, so they currently report `unavailable`. Queries never install tools,
build firmware, or grant hardware permissions. No bundled-platform readiness is
claimed by this server change.

## Run

Install the locked environment:

```text
uv sync --locked
```

Start the stdio server:

```text
uv run --locked pyocd-debug-mcp
```

Register this command with an MCP-compatible client, preferably from the
firmware project's directory:

```text
uv run --project <absolute-path-to-BYO-Firmware-MCP> --locked pyocd-debug-mcp
```

If the client does not start the server from the firmware project, set
`BYO_MCP_ARTIFACT_ROOT` to that project's absolute path. Do not point it at this
repository.

To isolate passive monitor records for one launch, set
`BYO_MCP_MONITOR_ROOT` to an absolute directory before starting the server:

```text
BYO_MCP_MONITOR_ROOT=/absolute/path/to/byo-monitor uv run --locked pyocd-debug-mcp
```

A nonblank monitor root is exclusive: `server_data/` and `simulated_remote/`
are created beneath it, never beneath per-user app data or the artifact root.
If that directory cannot be resolved or written, monitoring buffers in memory;
server startup and hardware behavior continue normally.

## Server documentation

- [Server guide](SERVER_GUIDE.md): setup, tool workflow, and safety model.
- [Architecture](docs/architecture.md): runtime and state ownership.
- [Client contract](docs/client-contract.md): MCP behavior and response contract.
- [Plan-tool contract](docs/plan-tool-contract.md): guarded-operation plan fields.
- [Sentry evidence](sentry-evidence/README.md): final passing test suite,
  specifications, plans, and recorded results for the Sentry monitor/logger work.

## Firmware MCP capabilities

### Tiered capability routing

Each named logical board has an explicit project-local safety tier. A newly
named board is `no-setup`: use `connect` with a current `setup_overview`
assignment or an explicit `probe_uid` (optionally a `target` override), and
the `*_raw` tools when the operator deliberately
owns unbounded-address risk. `setup-lite` adds confirmed coarse map containment
and retains raw escape hatches; every lite raw response instructs the agent to
display that bypass warning to the human. `setup-full` preserves the existing
validated/guarded workflow and refuses new raw routes.

A UID must resolve to exactly one current probe; a full UID or an unambiguous
partial UID is supported. A mistaken UID, the string `"null"`, or an unknown
label without a probe selection refuses before opening hardware. A target
override alone never selects a singleton. Raw identity remains unasserted;
generic/core compatibility does not establish an exact MCU package or flash
programming support. When a known profile's probe is missing or owned by another
board in this server, discovery reports that condition and preserves its binding.
Other visible probes are inventory options, without a substitute validation route.

Use `get_capabilities(board_id)` or `get_setup_status(board_id)` before acting;
both report the per-board tier, policy status and authoritative project root.
An incomplete initial setup (or a post-downgrade no-setup policy) remains raw.
An interrupted escalation from lite or full retains its prior active tier and
records the incomplete attempt separately. A corrupt committed policy is never
treated as raw and must be repaired before hardware access.

`$downgrade` and `$mass-erase` are manual-only workspace skills. The server
uses their project-local grants under `.agent-workspace/runtime/manual-permissions`,
resets those grants at every startup, and consumes each authorization once. Ask
the human to invoke the named skill; do not create or edit a grant directly.
Mass erase reserves that exact disclosure before plan acceptance and consumes it
before the backend call; a cancelled, changed, or failed plan discards the old
grant and requires a fresh skill invocation.

`target_unlock_raw` is also a mass erase and needs its own fresh `$mass-erase`
grant. Without one it is refused before any backend I/O and returns the live
identity, the uncontained whole-device erase disclosure, and the `manual_grant`
bindings to relay to the human; retry unchanged after the skill runs. The grant
is bound to the board, live session, policy, mechanism, and that raw scope, so a
reconnect or a safe-route grant does not authorize it. An attempt spends the
grant and disconnects the board even when the backend fails.

Connecting never erases. `connect`, `connect_under_reset`, setup, and validation
open pyOCD with `auto_unlock` disabled, so a debug-protected target (for example
Nordic APPROTECT or Kinetis flash security) fails with a locked-target error
(event code `target/locked`) and no session instead of being mass-erased to
attach. Both unlock tools need a connected session, so this server cannot
recover such a target; removing the protection erases the whole device and is
the owner's decision, made outside the server with the vendor's procedure.

Every flash route (`flash_application`, `flash_bootloader`, and `flash_raw`)
succeeds only after it reads back every byte the artifact addresses and finds
it unchanged. Before any reset or programming, a live target without a usable
flash algorithm for each addressed byte is refused with
`flash/unsupported-target`; a generic `cortex_m` connection is refused this way
because its default memory map has no flash. A no-op, partial, or unreadable
write fails with `flash/verification-failed`. A result reports `programming`
(verified byte count and ranges) separately from `target_state` (`running`,
`halted`, or `reset_state_unconfirmed`); an unconfirmed final reset leaves only
execution state unknown, never the verified bytes. ELF/AXF/HEX load addresses
come only from the artifact, and holes between them are neither padded nor
read. `flash_raw` also accepts a `.bin`, but only with an explicit
`base_address`; the server never infers one. Guarded flash still accepts only
ELF/AXF/HEX, and a raw base address grants no partition authority.

Raw memory, flash, and UART routes are deliberately bounded: raw serial reads,
writes, exchanges, payloads, and captured output have finite operation limits.
Existing planned full UART behavior is preserved.
Lite safe routes require the explicitly confirmed region/config/backend facts
for that operation; full safe routes additionally retain their normal live
identity and plan prerequisites.

The server provides a guarded board-development surface:

- **Board readiness:** discover connections, route familiar board names to validation or setup,
  establish board profiles, validate live hardware, and report readiness. When a machine cannot
  enumerate a debugger or serial port natively, an agent can author a local discovery hook under
  the project's `.firm/discovery_hooks` so the server can see it; hook output names hardware and
  grants no authority.
- **Debug and inspection:** inspect board, CPU, execution, symbol, and bounded-memory state;
  control breakpoints, stepping, reset/run, and other bounded diagnostic actions.
- **Serial evidence:** capture UART output and run controlled serial exchanges for declared tests
  or diagnosis.
- **Firmware deployment:** bind the selected build output in a flash plan, verify it against the
  reviewed stable map at execution time, and flash the approved application. A successful flash
  is deployment evidence, not proof of behavior.

`find_symbol`, `read_memory_symbol`, and symbol-backed `write_memory` accept the current project's
ELF explicitly. Pass `elf_artifact` after a server restart; within one uninterrupted Server Run, a
successful application flash also creates a temporary convenience binding. The server never swaps
in implicit checkout firmware as another project's symbol table, and symbol addresses remain
subject to the board's memory-map containment policy.
- **Guarded mutation and recovery:** writes, execution changes, bootloader work, and recovery
  require the current board gate, the exact `*-plan` action, and any required human permission.
- **Per-board safety:** validation, plans, permissions, budgets, and results never transfer between
  connections. Disconnects and new server runs require validation; stable-map problems use the
  server's refresh path.

Always follow live MCP guidance. Visibility is not authorization: guarded actions use an all-null
`*-plan` guidance call, the returned populated plan submission, then the paired action.

## Included command-line utilities

```text
uv run --locked pyocd-pack-repair --help
uv run --locked pyocd-native-build --help
uv run --locked pyocd-collect-artifacts --help
```

### Portable native-build artifacts

The server does not require a particular IDE or build system. Build with the project's native
tooling, then optionally normalize its explicit outputs with the small collector
(`pyocd-collect-artifacts`, above).

Historical implementation notes, progress material, test material, and generated
runtime state are kept outside the server in `../archive/BYO-Firmware-MCP-20260731/`.
