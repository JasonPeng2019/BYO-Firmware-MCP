# BYO Turnkey Brain and Guarded Hardware Server

The checkout contains the paired local product defined by `Server_A_functionality.md`:

- **Server A (`pyocd-turnkey`)** is the user-facing stdio turnkey brain. Its load tools teach and
  unlock `bug_fix`, `complex_implementation`, and `complex_task`; each call owns one fresh,
  persistent middleman agent and requires a deterministic green check before success.
- **Server B (`pyocd-debug-mcp` / `pyocd-debug-mcp-http`)** is the guarded hardware manager for
  setup, debugging, serial I/O, deployment, and recovery.

`pyocd-turnkey` verifies and reuses a separately managed loopback Server B, or starts one for the
Client A stdio lifetime. A child it starts terminates at EOF by default; set
`BYO_SERVER_B_PERSIST=1` only for an explicitly managed daemon. An OS lease prevents a second
physical-board owner.
The `.agent-workspace` submodule remains the outer agent workflow and skills product.

## Turnkey launch

Configure an operator-owned provider wrapper through `BYO_MIDDLEMAN_CONFIG`. Register Server A's
stdio command with Client A and, when Client A needs direct guarded hardware tools, also register
Server B's loopback streamable-HTTP endpoint `http://127.0.0.1:8765/mcp`:

```powershell
$env:BYO_MIDDLEMAN_CONFIG = "C:\path\to\middleman-provider.json"
$env:BYO_CLIENT_PROVIDER = "my-provider"
uv run --locked pyocd-turnkey
```

The one `pyocd-turnkey` process starts or reuses both servers. Client A uses the stdio registration
for turnkey load/agentic tools and the HTTP registration for direct Server B setup, plan, and
hardware tools. The middleman receives that same verified HTTP endpoint automatically. Clients
that use only turnkey workflows may omit the direct Server B registration.

Provider configuration is vendor-neutral:

```json
{
  "schema_version": 2,
  "provider_id": "my-provider",
  "command": ["C:\\path\\to\\provider-wrapper.exe"],
  "inherit_env": ["PROVIDER_API_KEY"],
  "env": {}
}
```

The wrapper stays alive for one agentic tool call. Before reading prompts it must initialize MCP at
the exact `BYO_SERVER_B_URL`, list tools, call `initialization_handshake`, copy that live process's
`server_run_id`, and write this exact readiness frame:

```json
{"type":"ready","provider_id":"my-provider","server_b_url":"<exact BYO_SERVER_B_URL>","server_b_product_id":"pyocd-debug-mcp-server-b","server_b_contract_version":1,"server_b_run_id":"<exact live handshake value>","mcp_initialized":true,"tools_listed":true}
```

It then reads JSON lines of the form
`{"type":"prompt","prompt":"..."}` from stdin and writes exactly one middleman decision JSON
object per line to stdout. Server A supplies `BYO_SERVER_B_URL`; the wrapper registers that
streamable-HTTP MCP endpoint with its provider CLI. This isolates provider-specific flags without
hard-coding Codex, Claude, or another vendor into the product.
Server A does not pass the expected run id to the wrapper. It independently probes Server B and
accepts readiness only when the wrapper proves it reached the same live process.
The provider id must match `BYO_CLIENT_PROVIDER`; this is the generic same-provider contract, not a
Codex/Claude allowlist.

Provider configuration is resolved lazily. The handshake and load guides remain usable when it is
missing; the first agentic call returns a structured configuration remedy instead of preventing the
MCP server from starting.

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
uv run --project C:\path\to\BYO-Server --locked pyocd-turnkey
```

Replace the path with this checkout. The workflow never silently connects an agent to hardware.

## Server B firmware capabilities

The server provides a guarded board-development surface:

- **Board readiness:** discover connections, route familiar board names to validation or setup,
  establish board profiles, validate live hardware, and report readiness.
- **Debug and inspection:** inspect board, CPU, execution, symbol, and bounded-memory state;
  control breakpoints, stepping, reset/run, and other bounded diagnostic actions.
- **Serial evidence:** capture UART output and run controlled serial exchanges for declared tests
  or diagnosis.
- **Firmware deployment:** check each selected artifact against the stable memory map at flash time
  and flash the approved application. A successful flash is deployment evidence, not behavior proof.
- **Guarded mutation and recovery:** writes, execution changes, bootloader work, and recovery
  require the current board gate, the exact `*-plan` action, and any required human permission.
- **Per-board safety:** validation, plans, permissions, budgets, and results never transfer between
  connections. New builds do not stale the map; disconnects and new runs require validation, while
  missing/corrupt or genuinely changed stable maps require `board_safety_refresh`.

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

Built-in flash evidence recognizes ELF and Intel HEX by content, not filename. Other toolchains can
install an `pyocd_debug_mcp.artifact_evidence` entry-point adapter that returns the same typed load,
entry, vector, and executable evidence; target adapters use the
`pyocd_debug_mcp.target_backends` entry-point group and `BYO_TARGET_BACKEND`. Caller-supplied allowed
ranges and raw load addresses remain forbidden.

The collector copies explicit native-build outputs byte-for-byte and records portable SHA-256
provenance in `build-manifest.json`. ELF, HEX, BIN, and linker maps have convenient fields and
canonical names; `native_artifacts` accepts provider-defined roles for S-record, UF2, TI-TXT,
vendor containers, or future toolchain outputs without changing core code. Collection does not
make a format flashable: the installed artifact-evidence/backend provider must parse and contain
it at flash time. A raw BIN has no trusted load address.

Agents connected through MCP can use the same behavior through the always-visible
`collect_build_artifacts` tool. Its indexed description gives the exact call contract and its
response returns canonical paths plus the next flash-plan handoff. For guarded firmware, normally
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

