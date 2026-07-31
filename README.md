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

## Server documentation

- [Server guide](SERVER_GUIDE.md): setup, tool workflow, and safety model.
- [Architecture](docs/architecture.md): runtime and state ownership.
- [Client contract](docs/client-contract.md): MCP behavior and response contract.
- [Plan-tool contract](docs/plan-tool-contract.md): guarded-operation plan fields.
- [Sentry evidence](sentry-evidence/README.md): final passing test suite,
  specifications, plans, and recorded results for the Sentry monitor/logger work.

## Firmware MCP capabilities

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
