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

## Included command-line utilities

```text
uv run --locked pyocd-pack-repair --help
uv run --locked pyocd-native-build --help
uv run --locked pyocd-collect-artifacts --help
```

Historical implementation notes, progress material, test material, and generated
runtime state are kept outside the server in `../archive/BYO-Firmware-MCP-20260731/`.
