# BYO Firmware MCP Server

This repository contains BYO's local, client-neutral MCP server for embedded
board setup, debugging, serial I/O, firmware deployment, and recovery through
pyOCD. It communicates over stdio and is compiled into the native BYO runtime.

The server never launches a client, silently connects to hardware, chooses a
firmware artifact, or treats tool visibility as authorization.

## Use the packaged server

Most users should not run this source checkout or register its Python entry
point directly. Install the current native bundle from
[`BYO-Releases`](https://github.com/buh07/BYO-Releases/releases), then initialize
the firmware project:

```sh
cd /path/to/your/firmware-project
byo init
byo codex firmware
```

`byo init` configures both clients to start the compiled sidecar through
`byo mcp serve`. The launcher supplies the validated project root, runtime root,
version, protocol, and capsule context. Restart an already-open client after
initialization so it reloads the MCP configuration. To launch Claude instead,
run `byo claude firmware`.

## Capabilities

- **Board setup and readiness:** discover probes, create project-local board
  profiles, admit verified device support, validate live hardware, and report
  capability-specific readiness.
- **Debug and inspection:** connect, halt, resume, step, reset, inspect CPU and
  execution state, resolve symbols, and perform bounded memory access.
- **Serial evidence:** capture UART output and run controlled serial exchanges.
- **Build artifacts:** normalize explicit ELF, HEX, BIN, and linker-map outputs
  with SHA-256 provenance without choosing a build system or toolchain.
- **Deployment and recovery:** flash reviewed application or bootloader plans
  and perform explicitly approved target recovery.

The live MCP `tools/list` response and tool descriptions are authoritative.
Guarded operations follow a `*-plan` call, submission of the returned populated
plan, and then the paired action. A successful flash is deployment evidence,
not proof that the firmware behaves correctly.

## Safety model

- Validation, plans, permissions, budgets, and results are scoped to one server
  run and one logical board; they never transfer between connections.
- Disconnecting or restarting clears live gates, plans, permissions, and
  assignments. Durable `.firm` files are evidence only and cannot restore
  authority.
- Memory access is contained to authoritative mapped ranges. Unknown,
  prohibited, or write-only regions fail closed.
- Writes, execution changes, bootloader operations, and recovery require the
  current validation gate and exact plan. Destructive recovery also requires
  fresh one-time human approval.
- MCP stdout is reserved for protocol framing. Diagnostics and actionable
  failures go to stderr or structured tool results.

The repository intentionally ships no board catalog, generated board profiles,
reference firmware, pack manifest, or runtime `.firm` state. Missing facts stay
missing until project-local setup proves them from built-in support or an
explicit official CMSIS-Pack and datasheet.

## Develop from source

Python 3.12 and [`uv`](https://docs.astral.sh/uv/) are the supported contributor
environment:

```sh
uv sync --locked
uv run pyocd-debug-mcp self-test
```

`self-test` is hardware-free and checks imports, packaged data, writable project
state, and the provider-worker handshake. Direct `serve` execution is an
integration interface, not a quick-start command: it requires explicit
`--project-root`, `--runtime-root`, `--launcher-version`, and
`--workflow-protocol` values plus a compatible project capsule. Use
`uv run pyocd-debug-mcp serve --help` when developing that boundary.

Available helper entry points:

| Command | Purpose |
|---|---|
| `pyocd-collect-artifacts` | Copy explicit native outputs into canonical names with provenance |
| `pyocd-native-build` | Run an exact build argv without a shell and report artifacts |
| `pyocd-pack-repair` | Inspect and repair the verified CMSIS-Pack index |

Each helper supports `--help`. None accesses hardware or grants safety
authority merely by producing an artifact or metadata.

Run the repository quality checks:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run python -m unittest discover -s tests
uv build
```

## Protocol references

- [Architecture and state ownership](docs/architecture.md)
- [MCP client contract](docs/client-contract.md)
- [Plan-tool contract](docs/plan-tool-contract.md)
- [Installer ↔ sidecar contract](https://github.com/JasonPeng2019/BYO-Installer/blob/InstallerV1/docs/sidecar-contract.md)
