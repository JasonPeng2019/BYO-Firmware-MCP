# BYO Firmware MCP client contract

Read `firmware://start-here` first. Its flow is detect -> configure -> build ->
flash -> verify -> debug, and it includes recovery routes. Use only the live
schemas below; no alternate public names are supported.

## Cooperative permission and exact plans

Every target-affecting setup, connection, execution, register/memory,
breakpoint, UART, flash, and recovery action has a required `plan_id`. First
call `request_hardware_permission(board_id, scope="routine-session")`; relay
the MCP elicitation or the exact returned approval command to the user. The
response contains exact structured `approval_argv`, beginning with the server's
absolute Python interpreter and `-m firmware_mcp.server approve-hardware`
(preferred for direct process execution), plus `approval_command` text rendered
for POSIX `sh` or Windows PowerShell, not generic `cmd.exe`; relay either unchanged.
The agent never runs the command or supplies the
authoritative finite call budget. Then call `get_hardware_permission`, create
one `create_hardware_plan(grant_id, board_id, objective, expected_result,
actions)`, and call its exact action with the returned `plan_id`.
`get_hardware_plan`, `revoke_hardware_permission`, and
`cancel_hardware_plan` inspect or stop those records. Permission is a
cooperative fallibility control for stale/wrong/thrashing calls, not an
attacker-defense or project-risk judgement. Changed connection/profile/identity,
stable serial, or artifact evidence invalidates the affected state.

## Setup and connection

- `get_setup_overview(board_names=None, connection_assignments=None)` inventories
  and routes candidates.
- Follow an emitted `next_call` only when it exists. A fresh-board route with
  `template_status=non_executable` is not a callable request: collect every
  `required_user_facts` item, replace unknowns in `arguments_template`, then
  construct and call the complete `setup_board` request. Do not invent values
  or invoke the partial template.
- `setup_board(board_id, connection_id, display_name, mcu_part_number, requires_uart, baud, serial_id, datasheet_path)` starts a new board-bound setup run.
- An unknown provider may pass `provider_recipe` with exactly `provider_id`,
  `inventory_argv`, and `worker_argv` to `get_setup_overview` and `setup_board`.
  Its returned connection ID is `provider:<provider_id>:<connection_id>`; neither
  component may contain `:` so this public route remains unambiguous and reversible.
- `repair_board_setup(board_id)` retries the current run; `continue_board_setup(board_id, continuation_id, response)` supplies the exact requested continuation.
- `validate_board(board_id)` returns current diagnostics; `get_setup_status(board_id)` reports profile and connection evidence.
- `connect_board(board_id, probe_id=None, target=None, board_config_path=None, under_reset=False)` creates one board-local session. Copy the exact returned `board_id` key; a disconnected returning board can request permission and plan this stored route while disconnected, before connecting. Null `probe_id` uses its stored assignment, a pyOCD spelling must be stably equivalent, external provider IDs match exactly, `target` must match verified support, `board_config_path` must be null, and `under_reset` uses the same route. `disconnect_board(board_id)` closes it. `get_board_info(board_id)` returns live board facts.

## Execution and physical access

`get_target_state(board_id)`, `halt_target(board_id)`, `resume_target(board_id)`,
`step_target(board_id)`, and `reset_target(board_id, halt_after_reset=False)`
report observed state rather than promising persistent execution.

Use `read_cpu_register(board_id, register_name)` and
`write_cpu_register(board_id, register_name, value, verify=True)` for supported
core registers. Use `write_peripheral_register(board_id, address, mask, value,
width_bits=32, verify=True)` for mapped peripheral state. `address` can be a
number or decimal/hex text. `read_memory(board_id, address, width_bits=32,
length_bytes=None)` and `write_memory(board_id, address, value, width_bits=32,
verify=True)` use current provider memory facts. `find_symbol(board_id, query,
elf_path)` requires an explicit ELF. `set_breakpoint(board_id, address)` and
`remove_breakpoint(board_id, address)` use numeric addresses returned by symbol
search or another trusted source.

## Build, firmware, and serial

`build_firmware(project_dir, build_dir, command, working_dir=None,
environment=None, artifacts=None, timeout_seconds=None)` runs an
exact argv list without a shell and with closed stdin: builds receive required input through argv,
working directory, and environment, never the MCP protocol stream. `collect_build_artifacts(output_dir, elf_path,
hex_path, bin_path, map_path, expected_roles)` normalizes explicit outputs.
`flash_firmware(board_id, firmware_path, flash_role, halt_after_reset=False,
artifact_target_evidence_path=None, plan_id)` requires an explicit physical
role. `application` is routine; `bootloader`, `full-device`, and `sensitive`
are one-action, one-attempt destructive plans with `grant_id=null`, followed
by `request_hardware_permission(scope="destructive-once", plan_id=...)`.
The approval view is the exact canonical disclosure of image bytes, ranges,
touched erase sectors, roles, live identity/map evidence, and optional exact
artifact-part metadata. Target-part comparison is `matched` only when both the
artifact and the current live observation are exact; otherwise it is honestly
`unavailable` and does not infer a part. `recover_target(board_id, mechanism,
plan_id)` has no default: select an exact current mechanism reported by
`get_board_info`, approve its disclosed affected ranges, and treat provider
acceptance as distinct from effect verification/session preservation.

`read_serial(board_id, timeout_seconds, expected_text=None, baud=None,
port=None, reset_on_open=False)`, `write_serial(board_id, text,
timeout_seconds, baud=None, port=None, line_ending="none")`, and
`exchange_serial(board_id, steps, timeout_seconds, ...)` each require a
caller-supplied positive finite `timeout_seconds` in seconds; the server has no
serial timeout default. For example, `timeout_seconds=3.0` is a caller-chosen
capture duration, not a server default. `baud` is bits per second and
`line_ending` is one of `none`, `lf`, `cr`, or `crlf`; exchange step objects use
the same spelling. `wait_duration(board_id, duration_seconds)` accepts positive
finite seconds.

## Evidence and recovery

All mutations report whether values were verified, unavailable, or not
requested. A failed flash can retain successful program/readback evidence while
showing a reset failure separately. When a connection drops, reconnect with
`connect_board`, then use `validate_board` if diagnostics request it. For an
incomplete setup continuation, use `continue_board_setup` and retry
`repair_board_setup`. For a build failure, correct the exact argv or artifact
path and rerun `build_firmware`; for UART mismatch or timeout, inspect the raw
bytes returned by `read_serial` or `exchange_serial` before changing a timeout.
