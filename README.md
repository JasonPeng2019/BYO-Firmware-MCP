# BYO Server

BYO Server is a headless local MCP server for safe embedded-board setup,
debugging, serial I/O, flash, and recovery through pyOCD. It runs over stdio
only. Any compatible MCP client can use it; the server, not the client, owns
plans, permissions, board routing, validation, safety containment, timeouts,
and cleanup.

## Run from the checkout

Operational use is checkout-only and requires the complete checkout because board profiles, pack
metadata, firmware, and `.firm` evidence are not wheel package data. Python
3.12 is the team pin; package metadata supports Python 3.10 and newer.

1. Read [init.md](init.md) for host prerequisites.
2. Follow [stage0_setup.md](stage0_setup.md) for board readiness.
3. Register this command with an MCP client:

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

Replace the placeholder with this checkout. Stdout is reserved for MCP
framing. `pyocd-debug-mcp` intentionally has no conventional `--help` mode.

Checkout utilities include:

```text
uv run --locked python host_bootstrap.py --help
uv run --locked python stage0_check.py --help
uv run --locked python scripts/migrate_boards_to_firm.py --help
uv run --locked python scripts/run_fresh_workspace_e2e.py --help
uv run --locked pyocd-pack-repair --help
uv run --locked pyocd-zephyr-build --help
```

After setup validation, `get_setup_status` returns advisory build guidance for
a known MCU, including the exact Zephyr board target and a directly executable
`<server-python> -m pyocd_debug_mcp.zephyr_build ...` command that does not
depend on ambient `PATH`. The helper automatically uses a short scratch path
for generated files when a Windows checkout path would exceed west/CMake
limits. Final ELF/map artifacts still require `board_safety_refresh`; build
guidance never grants memory authority.

For a brand-new artifact root, `scripts/run_fresh_workspace_e2e.py` is the
checkout-local setup-only orchestrator. It takes the exact board, MCU, probe,
stable UART identity, datasheet path, and artifact-root identity, requires the explicit
`--authorize-setup` flag, drives the real MCP stdio handshake/plan/setup/
validation sequence, and writes fixed-path machine-readable evidence. It has
no callback, shell command, code-generation, build, flash, or UART-write
option. Any terminal status other than completed setup plus current-run
readiness stops the process before a coding workflow can begin.

Plan fields, budgets, and permission modes are listed in
[`docs/plan-tool-contract.md`](docs/plan-tool-contract.md), which is generated
from the same definitions used by the live MCP schemas. Setup resolves the
current UART port from the selected stable identity and computes the datasheet
SHA-256 itself; agents never need to bind a COM path or run a hash command.

## Tool surface

Call `initialization_handshake` first. The live `tools/list` response is the
authoritative advertised surface; visibility can change after plan/setup calls.
A visible tool is never proof of authorization.
After a plan is accepted, dynamic clients should use its newly exposed direct
action. Clients with a static function binding can use the exact returned
single-child `action_batch` fallback; it follows the identical guarded dispatch
path and is never permission to invent hidden calls.

Always-advertised operational tools cover:

- connection and inspection: `connect`, `disconnect`, `get_board_info`,
  `get_state`, `read_cpu_register`, `read_execution_state`, `find_symbol`, and
  `read_memory_symbol`;
- ordinary execution: `halt`, `resume`, `step`, `reset_and_run`,
  `remove_breakpoint`, and bounded `wait`;
- setup and safety: familiar-name `setup_overview`, `load_setup_tool`,
  setup-first `board_setup-plan`, strict `continue_setup`,
  `board_safety_setup`, build-aware `board_safety_refresh`, `board_validate`,
  and the non-authoritative `get_setup_status` readiness barrier;
- orchestration: `action_batch`; and
- the `*-plan` tools for guarded actions.

Guarded actions are registered but hidden until their exact plan unlocks them:

- connection/execution: `connect_override`, `connect_under_reset`,
  `reset_and_halt`, `write_cpu_register`, and `set_execution_state`;
- memory/register/debug: `read_memory_address`, `write_memory`,
  `register_write`, and `set_breakpoint`;
- serial and flash: `read_serial`, `write_serial`, single-open
  `serial_exchange`, `flash_application`, and
  permission-locked `flash_bootloader`;
- destructive recovery: `target_unlock`, which requires fresh one-time
  approval and leaves the validation gate closed; and
- setup mutation: `board_setup` and `board_fix_setup`, exposed only through
  the setup loader and plan workflow.

After the handshake, ask the user only for familiar board names and pass them
to `setup_overview`. The normalized phrase `no board` is a literal sentinel
that must be passed alone, never treated as a candidate profile name. Every
matching YAML routes to validation first; unknown names route to setup, and
validation may return a specific repair. The response supplies bounded
`load_call`, `next_call`, or plan-template objects with every server-known ID
already filled. Copy those fields into MCP calls; never ask the user to invent
them or to hash a datasheet. `load_setup_tool` then returns guidance only for
the requested setup tool. If setup or validation returns a friendly choice,
relay its prose and copy its exact `accepted_response`; do not scrape labels,
invent a target, or ask the user for internal IDs.

Superseded unified reset/core-register/memory/flash tools and
`unlock_recover` are absent. Exact schemas and status payload behavior are in
[docs/agent-contract.md](docs/agent-contract.md) and the live MCP descriptions.

## Safety model

Each logical board has one live connection and one operation boundary.
Same-board calls serialize while different boards can execute concurrently.
Guarded requests are scoped to their run, board, session, exact parameters,
plan budget, and permission.

Only successful `board_validate` opens the in-memory gate. Writes recheck the
current aggregate safety fingerprint on every call and apply typed containment
before backend mutation. Disconnect and restart clear live assignments, plans,
permissions, and gates. Files under `.firm` are durable evidence only and can
never restore authority.

Target recovery and bootloader flash are destructive operations with stronger
approval rules. Never treat conversational approval, a report, tool visibility,
or a prior run as current authorization.

## Validation

Run the software suite from the checkout:

```text
uv run --locked pytest
uv run --locked ruff check .
uv run --locked pyright
```

M10 performance targets are measured without making host speed a CI gate:

```text
uv run --locked python scripts/measure_m10_performance.py --samples 7
```

The tool records host and dependency context and measures gate/freshness,
eight-device enumeration, and NULL-plan/handshake latency. Current dated
evidence is in `docs/evidence/`.

Hardware acceptance is separate because it requires positively identified,
recoverable bench boards and explicit destructive authorization. See
[docs/verification.md](docs/verification.md) for the evidence labels and open
hardware/client matrix.

## More detail

- [Architecture and state ownership](docs/architecture.md)
- [Agent interaction contract](docs/agent-contract.md)
- [Contract snapshot history](docs/contract-history.md)
- [Verification status](docs/verification.md)
- [Historical extraction provenance](docs/extraction-manifest.json)

No authoritative project-root LICENSE or NOTICE was available. This project
makes no license claim; publication remains blocked on the authoritative human
licensing decision.

## Verified

The current software suite covers the MCP product contract, board-scoped
routing, plans and permissions, safety containment, managed cleanup, stdio-only
exposure, authority-free persistence, relay text, Unicode profile names, and
the non-gating M10 performance measurements. `InMemorySessionStore` remains the
process-local session implementation; durable reports are evidence only. The
optional R11 path is a Codex-specific benchmark and does not define ordinary
server behavior.

Build/setup guidance is local-first for heavy dependencies. Agents are told to
reuse validated SDKs, RTOS trees, toolchains, packs, and large libraries from
bounded standard locationsâ€”including NCS/Zephyr and STM32CubeIDE-provided
STM32Cube/ThreadXâ€”before using a managed network fallback. Discovery validates
versions and executable tools and never recursively scans a whole disk.

## Pending verification

Fresh hardware/client acceptance remains separately scoped in
[docs/verification.md](docs/verification.md), including the exact
`nrf52833dk` and `nucleo_l476rg` pair, cross-host proof, and any authorized destructive work. Publication licensing
and independent process-tree cleanup evidence also remain human/bench gates.
