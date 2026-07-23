# BYO Firmware MCP

BYO Firmware MCP is a local stdio MCP server for firmware setup, direct native
builds, built-in pyOCD debug/flash work, UART evidence, and provider-supported
recovery. It also accepts a provider-neutral direct-argv recipe for an external
debug provider that implements the documented worker protocol. The server is
the `byo-firmware-mcp` distribution, version 0.2.0; its Python package is
`firmware_mcp`. It preserves per-board connection isolation and reports hardware
evidence rather than inventing a result.

## Install and register

From a checkout, install the locked environment:

```text
uv sync --locked
```

Use a portable absolute checkout path when registering the server. POSIX shells:

```text
uv run --project /absolute/path/to/BYO-Firmware-MCP --locked byo-firmware-mcp
```

PowerShell:

```powershell
uv run --project 'C:\absolute\path\to\BYO-Firmware-MCP' --locked byo-firmware-mcp
```

Read the MCP resource `firmware://start-here` after connecting. It gives the
current detect -> configure -> build -> flash -> verify -> debug workflow.

## Normal workflow

For a new board, call `get_setup_overview` and follow its `next_call` only when
it exists. A route with `template_status=non_executable` is not directly
invokable: collect every `required_user_facts` item, replace each unknown in
`arguments_template`, then construct and call the complete `setup_board`
request. Do not invent values or invoke the partial template. Continue with
`continue_board_setup` or
`repair_board_setup` if setup requests more evidence, then use
`validate_board`, `connect_board`, `build_firmware`, and `flash_firmware`.

After connecting, make a routine `refresh_safety_map` plan before memory,
peripheral-register, or breakpoint work. It publishes the one canonical
`.firm/safety/<board_id>/memory-map.json` from live provider regions plus any
selected JSON/YAML layout and ELF evidence. The layout records explicit
half-open regions (`name`, `role`, `start`, `end`, `source_path`, and
`source_locator`); a readable `unknown` role remains observable but is reported
as uncertainty. Refresh binds the selected bytes, returns its digest, and a
changed map invalidates prior board plans.

Live identity is capability-aware: an exact current part observation may
match the configured profile; a reread compatible proof (including CPUID) is
not an exact part claim; and an unavailable comparison is reported with its
reason. A verified exact or compatible contradiction stops map publication and
target work. Compatible or unavailable comparison alone does not replace the
independent route, region, semantic-role, plan, permission, readback, and
reset checks, nor does it block them merely for lacking an exact part fact.

Before each target-affecting setup, connect, debug, memory, breakpoint, or UART
call, use the visible guard workflow: call
`request_hardware_permission`, relay its MCP elicitation or exact
returned approval command to the cooperative user. The exact `approval_argv`
starts with the server's absolute Python interpreter followed by
`-m firmware_mcp.server approve-hardware`; it is preferred when the client can
execute argv directly. `approval_command` renders those same tokens for POSIX
`sh` or Windows PowerShell, never generic `cmd.exe`; relay either unchanged.
Then call `get_hardware_permission`, create one
exact `create_hardware_plan`, then supply its `plan_id` to the matching action.
The agent never supplies the authoritative call budget or runs the approval
command. This is a fallibility check against stale/wrong/thrashing hardware
work, not an attacker-defense or project-risk judgement. `get_hardware_plan`,
`revoke_hardware_permission`, and `cancel_hardware_plan` inspect or stop the
same run-scoped state. Changed profile/session/serial evidence, disconnect, or
changed artifact bytes require a new permission and plan.

`flash_firmware` requires `flash_role`. An `application` image follows the
ordinary routine grant/plan flow; `bootloader`, `full-device`, and `sensitive`
images require a single-action `disclosure-required` plan and one exact
`destructive-once` user approval. The disclosed image bytes, target comparison,
touched erase sectors, map roles, and final-reset request are rechecked before
the provider receives a server-owned immutable snapshot. Artifact metadata is
compared only when both it and a current exact live part observation exist;
otherwise target comparison is unavailable, never guessed. `recover_target` likewise
requires one exact live `mechanism` shown by `get_board_info`; provider command
acceptance is not erase verification, and an unproven preserved session must be
reconnected, validated, and mapped again.

For a returning board, copy its exact `board_id` key from `get_setup_status`
or `get_setup_overview`, request permission, and create an exact
`connect_board` plan from its stored profile and assignment while disconnected.
Then call `connect_board`, validate if diagnostics request it, and choose debug,
serial, build, or flash work. Do not replace the returned key with an example
spelling or provide an external board-config path. Assign each physical
connection to only one `board_id`; operations on a board serialize while
separate boards remain independent.

`build_firmware` runs caller-provided argv directly, without a shell and with closed stdin.
Build commands receive required input through their exact argv, working directory, and environment,
never the MCP protocol stream. It reports
argv, cwd, environment override keys, process evidence, timeout outcome, and all
declared or discovered artifacts. `collect_build_artifacts` is the separate
normalization operation for explicit ELF, HEX, BIN, and MAP inputs.

## Live tool surface

- Setup and safety: `get_setup_overview`, `setup_board`, `repair_board_setup`,
  `continue_board_setup`, `validate_board`, `get_setup_status`.
- Connection and execution: `connect_board`, `disconnect_board`,
  `get_board_info`, `get_target_state`, `halt_target`, `resume_target`,
  `step_target`, `reset_target`.
- Registers and memory: `read_cpu_register`, `write_cpu_register`,
  `write_peripheral_register`, `read_memory`, `write_memory`, `find_symbol`.
- Debug and firmware: `set_breakpoint`, `remove_breakpoint`, `build_firmware`,
  `collect_build_artifacts`, `flash_firmware`.
- Safety publication: `refresh_safety_map`.
- UART and recovery: `read_serial`, `write_serial`, `exchange_serial`,
  `wait_duration`, `recover_target`.
- Guard controls: `request_hardware_permission`, `get_hardware_permission`,
  `revoke_hardware_permission`, `create_hardware_plan`, `get_hardware_plan`,
  `cancel_hardware_plan`.

Every live tool description specifies its exact schema, evidence returned, and
the next recovery action. See [SERVER_GUIDE.md](SERVER_GUIDE.md),
[architecture](docs/architecture.md), the [client contract](docs/client-contract.md),
and the [provider-worker protocol](docs/provider-worker-protocol.md).
