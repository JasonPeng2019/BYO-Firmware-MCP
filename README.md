# BYO Server Firmware Workflow

BYO Server is a guarded local MCP server for embedded-board setup, debugging, serial I/O,
deployment, and recovery through pyOCD.

## Install and connect

Install the locked environment from the checkout:

```text
uv sync --locked
```

Register this stdio command in any MCP-compatible client:

```text
uv run --project <absolute-path-to-BYO-Server> --locked pyocd-debug-mcp
```

For example:

```text
codex mcp add byo-firmware -- uv run --project C:\Users\Jason\Documents\Jason\FirmCLI_Tester\BYO-Firmware-MCP --locked pyocd-debug-mcp
```

-> For the user Jason, using codex, where the server is stored at \FirmCLI_Tester\BYO-Firmware-MCP, this is the registration command.

The server is client-neutral and never launches a client workflow or silently connects to hardware. See
[SERVER_GUIDE.md](SERVER_GUIDE.md) for the full setup and tool workflow.

## Firmware MCP capabilities

The server provides a guarded board-development surface:

- **Board readiness:** discover connections, route familiar board names to validation or setup,
  establish board profiles, validate live hardware, and report readiness.
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

### Portable native-build artifacts

The server does not require a particular IDE or build system. Build with the project's native
tooling, then optionally normalize its explicit outputs with the small collector:

```text
uv run --locked python -m pyocd_debug_mcp.artifact_collector \
  --output-dir build/collected --producer native-project-build \
  --elf path/to/application.elf --hex path/to/application.hex \
  --map path/to/application.map --expect elf --expect map
```

The collector copies explicitly typed ELF, HEX, BIN, and linker-map files byte-for-byte into
canonical `firmware.*` names and records portable SHA-256 provenance in `build-manifest.json`. It
does not run a build, discover memory permissions, access hardware, or authorize flashing. Use the
canonical output in the matching flash plan; safety refresh accepts no build artifacts, and a raw
BIN has no trusted load address.

MCP clients can use the same behavior through the always-visible
`collect_build_artifacts` tool. Its indexed description gives the exact call contract and its
response returns canonical paths plus the flash-plan handoff. For guarded firmware, normally
supply the coherent ELF and linker map with `expected_roles=["elf", "map"]`. The standalone CLI
remains useful to developers and terminal-driven workflows outside an MCP session.

For a server-directed build, inspect the project's own build files and use the
`pyocd_debug_mcp.native_build` argv template from `get_setup_status.build_guidance`. Put the exact
native build argv after `--`; the helper runs it directly without a shell and reports its cwd,
network policy, exit code, ELF/HEX format checks, and map provenance. It does not invent universal
ELF/map coherence where linker-map formats cannot prove it. If discovery finds multiple ELF or map
outputs, it reports sorted candidates without selecting one; rerun with `--artifact-elf`,
`--artifact-map`, or `--artifact ROLE=PATH`. `--cwd`, repeatable `--env KEY=VALUE`,
`--timeout-seconds`, and repeatable `--artifact ROLE=PATH` declarations cover arbitrary SDKs,
vendor CLIs, IDE wrappers, output formats, and future build systems without a server adapter. Known
ELF/HEX formats receive structural checks; opaque formats are reported only as existing nonempty
files. Prefer a compatible installed toolchain, but acquire one normally
when none exists. Network access is inherited by default; `--offline` is an intentional best-effort
set of common-client environment guards, not an OS network sandbox or implicit policy. The server
does not detect or select a provider, SDK root, compiler, board target, or installation layout.
Every build system uses the same exact-argv path. The helper never accesses hardware or grants
safety authority.

Fresh boards do not need a checked-in board YAML. Given an exact MCU ordering code and local PDF,
setup first replays verified local support and otherwise requests either an exact
installed pyOCD target or one official CMSIS-Pack. The server—not the client—replays built-in static
geometry or bounds and hashes the archive, proves its exact PDSC leaf, and derives the pack target.
It tests a non-destructive live attach before recording project-local support; a built-in target
with partial metadata remains usable for only the capabilities it can prove. Setup captures the
exact datasheet bytes as immutable source evidence and creates a
schema-v3 map from separate pack flash/RAM/ROM ranges and optional SVD peripheral blocks. Unknown
identity, erase, peripheral, deployment, or recovery facts disable only the dependent capability;
they are never guessed from a part name. Core-compatibility identity permits bounded read/debug and
guarded artifact-contained application programming; bootloader and recovery authority remain
separate, and setup status reports that distinction before deployment planning.

The checkout ships no board catalog, pack manifest, board profile, reference firmware, or generated
runtime state. Missing facts stay missing and setup/validation names the repair. An optional external serial-helper registry may be configured explicitly; generic USB identity and
manual serial selection remain available without it. Destructive recovery
uses the target-neutral `backend_mass_erase` capability after live-backend support, complete erase
disclosure, and fresh one-time permission checks; `manual_only` remains fail-closed.

## Further documentation

- [Server operation and recovery](SERVER_GUIDE.md)
- [MCP client contract](docs/client-contract.md)
- [Architecture and state ownership](docs/architecture.md)
- [Plan-tool contract](docs/plan-tool-contract.md)

