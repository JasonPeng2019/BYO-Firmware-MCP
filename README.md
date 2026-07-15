# BYO Server

BYO Server is the headless, local stdio MCP server for Firmware-CLI's board-debug
substrate. Any compatible MCP client can use its ordinary tools. The optional
R11 harness is a separate Codex-specific benchmark; it is not the definition of
normal BYO use.

The server owns the safety boundary. Clients request operations, while
`server.py`, the guardrails, and the board-facing services validate arguments,
enforce flash/recover policy, record sessions, and block repeated unsafe
mutation patterns. MCP tool names, schemas, and operation contracts live in the
tool docstrings in `src/pyocd_debug_mcp/server.py`; this project intentionally
does not maintain duplicate per-tool sidecar documentation.

## Supported deployment

Operational use is **checkout-only**. Run commands from this `BYO-Server/`
directory. Board profiles, firmware, pinned-pack metadata, bootstrap scripts,
R11 cases, and benchmark workspaces are checkout-owned resources and are not
bundled into the wheel. The wheel is built and fresh-installed only to verify
metadata, console entrypoints, and importability; an installed wheel is not a
supported board-control or benchmark deployment.

Python 3.12 is the team pin in `.python-version`; package metadata retains the
runtime's Python 3.10 minimum. Windows and macOS have bounded setup scripts.
Linux is not currently covered by a host setup script.

## Start here

1. Read [init.md](init.md) for host prerequisites and initial setup.
2. Follow [stage0_setup.md](stage0_setup.md) for board-scoped readiness and
   Stage 0/1 validation.
3. Register the stdio server with an MCP client. A generic command shape is:

   ```json
   {
     "mcpServers": {
       "pyocd-debug": {
         "command": "uv",
         "args": [
           "run",
           "--project",
           "<absolute-path-to-BYO-Server>",
           "--locked",
           "pyocd-debug-mcp"
         ]
       }
     }
   }
   ```

   Use the client's normal configuration mechanism and replace the placeholder
   with this checkout's absolute path. The server uses stdio; stdout belongs to
   MCP framing.

The retained public console scripts are:

- `pyocd-debug-mcp`: start the stdio MCP server. It has no conventional
  `--help` mode; its contract is the MCP initialize/list-tools surface.
- `pyocd-pack-repair --help`: inspect or repair selected CMSIS-Pack index
  descriptors.
- `pyocd-zephyr-build --help`: provision or use a Zephyr build environment.

Board bootstrap, Stage 0, Stage 1, and R11 remain checkout commands rather than
new public entrypoints:

```text
uv run --locked python host_bootstrap.py --help
uv run --locked python stage0_check.py --help
uv run --locked python -m tests.harness.stage1_smoke --help
uv run --locked python -m tests.harness.r11_benchmark --help
```

## Product boundary

Included here are the local MCP server, adapters, guardrails, shared
board-facing services, board and firmware data, pack/bootstrap support, and the
R10/R11 test and benchmark closure. The turnkey brain, UX, provider-memory
runtime, Codex app-server services, R12 harness, skills/playbooks bundles, Rich,
and prompt_toolkit are excluded.

The ordinary server is model-agnostic. R11 launches `codex exec`, requires
Codex registration/provider access plus live hardware, enforces its own frozen
prompt/result/scoring contract, and writes benchmark artifacts under `runs/`.
Passing R11 demonstrates that benchmark path only; it is not universal proof
for every MCP client.

## Current state and limits

- Sessions use a process-local `InMemorySessionStore` plus durable JSONL and
  summary files under `runs/`. There is no Redis implementation in this copy.
- A server restart loses the in-memory session index even though prior run
  files remain on disk.
- Explicit disconnect and normal service paths close owned pyOCD/serial
  handles, but full descendant process-tree and interrupted-run cleanup proof
  remains open. A timeout must not be interpreted as proof that every child or
  hardware-adjacent resource was cleaned up.
- The official proof pair is `nrf52833dk` plus `nucleo_l476rg`.
  `nrf52840dk` is retained alternate Nordic evidence and does not substitute
  for exact `nrf52833dk` or official-pair proof.
- This extraction ran non-hardware checks only. Fresh standalone hardware,
  provider, MCP Inspector, and cross-host proof remain pending; see
  [docs/verification.md](docs/verification.md).

## Distribution and licensing

No authoritative project-root LICENSE or NOTICE file was available to copy.
This project does not invent one and `pyproject.toml` makes no license claim.
Publishing or redistributing the standalone artifact is blocked on a human
decision identifying the authoritative license and required notices.

## More detail

- [Architecture and boundaries](docs/architecture.md)
- [Verification status and handoff](docs/verification.md)
- [Extraction provenance](docs/extraction-manifest.json)

## Verified

The S1-S4 extraction, ordinary MCP contract parity, standalone resource roots,
R11 corpus isolation, and the BYO-only packaging/documentation contract have
non-hardware evidence recorded in the extraction artifacts.

## Pending verification

Complete clean-room integration (S6), independent review, fresh Windows/macOS,
official-board hardware, live Codex/R11, MCP Inspector, cleanup, layout/drift,
and authoritative licensing decisions remain pending.
