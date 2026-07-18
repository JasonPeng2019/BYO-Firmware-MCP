# BYO Server Firmware Workflow

BYO Server is a guarded local MCP server for embedded-board setup, debugging, serial I/O,
deployment, and recovery. Its agent workflow lives in the `.agent-workspace` submodule.

## Install the workflow and launch an agent

Add the workflow submodule to an existing BYO Server checkout once:

```powershell
cd C:\path\to\BYO-Server
git submodule add <agent-workspace-repository-url> .agent-workspace
git submodule update --init --recursive
```

When cloning a checkout that already records the submodule, use either:

```powershell
git clone --recurse-submodules <BYO-Server-repository-url>
# or, inside an existing clone:
git submodule update --init --recursive
```

Run the submodule loader to render the selected mode and mirror its skills into `.claude/skills`,
`.agents/skills`, and `.codex`:

```powershell
py .agent-workspace\bin\setup-project --mode firmware
```

Run it again after updating the submodule. To change mode after initial setup, use:

```powershell
py .agent-workspace\bin\set-mode firmware-full
```

Launch an agent from the outer BYO Server directory:

```powershell
py .agent-workspace\bin\codex-mode firmware
# or
py .agent-workspace\bin\claude-mode firmware
```

Then explicitly tell the agent to register and connect to the local server by running:

```powershell
uv run --project C:\path\to\BYO-Server byo-mcp-register server
```

Replace the path with this checkout. The workflow never silently connects an agent to hardware.

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

Agents connected through MCP can use the same behavior through the always-visible
`collect_build_artifacts` tool. Its indexed description gives the exact call contract and its
response returns canonical paths plus the flash-plan handoff. For guarded firmware, normally
supply the coherent ELF and linker map with `expected_roles=["elf", "map"]`. The standalone CLI
remains useful to developers and terminal-driven agents outside an MCP session.

`pyocd_debug_mcp.zephyr_build` remains an optional Zephyr convenience. It uses generated sysbuild
domain metadata to keep the application ELF and linker map together instead of guessing by path,
and it exports the same canonical bundle without deleting the incremental native build tree.
`get_setup_status` presents that command only as a labeled toolchain fallback; the native project
build plus the visible collector is the default for every MCU and build system.

Reviewed board geometry and attach facts are packaged data, not Python board-name branches. Missing
facts stay missing and setup/validation names the repair. Serial resolution uses generic USB
identity first, with configured vendor helpers only as ambiguity fallbacks. Destructive recovery
uses the target-neutral `backend_mass_erase` capability after live-backend support, complete erase
disclosure, and fresh one-time permission checks; `manual_only` remains fail-closed.

## Firmware workflow skills

Skills are manually invoked with `/name` in Claude Code or `$name` in Codex, except `mcp-help`,
the sole auto-invocable firmware skill. It gives the model MCP setup, gate, and two-call plan
guidance without exposing server internals.

Common skills include `spec`, `read-spec`, `plan`, `test-first`, `verify`, `adversarial`,
`implement-high`, `implement-low`, `bug-fix-simple`, and `bug-fix-complex`.

Firmware adds `board-setup`, `verify-firmware-hil`, `implement-hil`,
`bug-fix-firmware-hil`, `verify-firmware-large`, `implement-firmware-large`,
`bug-fix-firmware-large`, `hard-fault-triage`, `memory-map-safety`,
`peripheral-contract`, `rtos-patterns`, and `server-help`.

Run `py .agent-workspace\bin\doctor --mode firmware` after setup. For the full server reference,
see [SERVER_GUIDE.md](SERVER_GUIDE.md).

## Verified

The checked-in software workflows and hardware evidence are identified in docs/verification.md.

## Pending verification

Hardware-dependent results remain bounded by the exact identities and blockers recorded in docs/verification.md.

