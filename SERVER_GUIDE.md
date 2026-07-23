# BYO Firmware MCP server guide

BYO Firmware MCP is the `byo-firmware-mcp` version 0.2.0 distribution; its
Python package is `firmware_mcp`. Start the local stdio server with the portable
command in the [README](README.md), then read `firmware://start-here`. The server does not
select a board, probe, serial port, toolchain, or firmware on a caller's behalf.

## Work with boards

Use `get_setup_overview` to inventory candidate boards and routes. Follow an
emitted `next_call` only when it exists. For a fresh-board route with
`template_status=non_executable`, collect every `required_user_facts` item,
replace each unknown in `arguments_template`, then construct and call the
complete `setup_board` request. Do not invent values or invoke the partial
template. If setup requests research or a choice, provide its exact continuation
through `continue_board_setup`, then use `repair_board_setup` as often as
needed. `validate_board` returns live diagnostic evidence, while
`get_setup_status` reports persisted configuration and whether a connection is
currently present.

`connect_board` establishes the board-local session. Copy the exact `board_id`
key returned by `get_setup_overview` or `get_setup_status`; an illustrative
name is never a route. A returning board may request permission and create its
exact `connect_board` plan while disconnected: the plan replays the saved
profile and assignment. A null `probe_id` uses that assignment; a supplied
pyOCD UID must be stably equivalent to it, while external provider connection
IDs must match exactly. A supplied `target` must match verified saved support,
`board_config_path` must be null, and `under_reset=true` uses that same route
with wired reset. Call `disconnect_board` when board work ends. A physical
connection cannot be assigned to two board IDs, and same-board work is
serialized.

After connecting, publish current semantic evidence with
`refresh_safety_map(board_id, layout_path, application_elf_path, plan_id)`.
It is a visible routine guarded call, not destructive authority. It binds the
exact selected layout/ELF bytes and atomically associates the canonical
`.firm/safety/<board_id>/memory-map.json` with the profile. A layout is JSON or
YAML schema version 1 with the exact board ID and `regions` containing `name`,
one explicit semantic `role`, half-open `start`/`end`, `source_path`, and
`source_locator`. Provider facts without semantic evidence remain `unknown`;
readable unknown spans can be observed with that uncertainty, but writes do not
gain authority. A changed digest invalidates existing plans for that board.
The associated live identity record is capability-aware: `exact` includes a
matched current part, `compatible` proves only a compatible family/core fact,
and `unavailable` names the missing observation. A verified exact or compatible
contradiction blocks the operation; compatible and unavailable evidence never
pretend to be exact-part authority.

## Cooperative-user plan workflow

Visible hardware actions require an exact `plan_id`; visibility is not
authority. Before a routine action, call
`request_hardware_permission(board_id, scope="routine-session")`. Relay the
MCP elicitation to the cooperative user, or relay the returned exact
approval command unchanged when elicitation is unavailable. The response's
exact `approval_argv` starts with the server's absolute Python interpreter and
`-m firmware_mcp.server approve-hardware` (prefer it when direct argv execution
is available); `approval_command` renders those exact tokens for POSIX `sh` or
Windows PowerShell, not generic `cmd.exe`. The agent does not run that
command or choose the authoritative finite budget. After direct user approval,
call `get_hardware_permission`, then
`create_hardware_plan(grant_id, board_id, objective, expected_result, actions)`
with each exact action argument except `plan_id`; pass the returned `plan_id` to
that one action. `get_hardware_plan`, `revoke_hardware_permission`, and
`cancel_hardware_plan` expose or stop the state. A plan is bound to board,
profile/session evidence, serial evidence where relevant, and artifact bytes
where relevant; a changed binding needs a new request and plan. This protects
against honest stale/wrong calls and thrashing, not against an attacker or a
user-selected project risk.

## Build, flash, and debug

Call `build_firmware` with the project and build directories plus an exact argv
list. It runs that command directly and returns complete process evidence and
all declared or discovered artifacts. A zero exit with no artifact is honestly
reported as process success with the next action to supply or locate output.
Use `collect_build_artifacts` when a canonical manifest/copy of known outputs is
useful.

`flash_firmware` requires an ELF, AXF, or Intel HEX `firmware_path` and one
explicit `flash_role`. `application` uses the ordinary finite routine grant;
`bootloader`, `full-device`, and `sensitive` use a single-action destructive
plan with `grant_id=null`, followed by one exact `destructive-once` approval.
The approval displays reproducible image, erase-sector, role, identity, and
target-comparison evidence. The provider receives a server-owned snapshot of
those exact bytes, then Slice-1 byte readback remains required.

Use `get_target_state`, `halt_target`, `resume_target`, `step_target`, and
`reset_target` for core control. Use `read_cpu_register`, `write_cpu_register`,
and `write_peripheral_register` only for provider-supported state. Physical
memory access is through `read_memory` and `write_memory`; it checks both live
provider region facts and the current map role, and reports unknown readable
evidence honestly. `write_memory` is limited to mapped `ordinary_ram` and
peripheral writes to mapped `peripheral`; special physical roles remain pending
the future destructive disclosure path. Resolve symbols with `find_symbol`; a
numeric `set_breakpoint` also needs an exact `elf_path` whose file-backed
executable PT_LOAD bytes cover the address. `remove_breakpoint` rechecks current
executable map coverage without reopening an ELF.

## UART and recovery

`read_serial` preserves empty exploratory captures as successful transport
evidence and returns raw byte evidence. `write_serial` and `exchange_serial`
report partial writes as failures. Use `baud`, `port`, `timeout_seconds`, and
`line_ending` consistently. `wait_duration` deliberately holds the board-local
operation boundary for a finite positive duration.

`get_board_info` lists the connected provider's live recovery mechanisms.
`recover_target(board_id, mechanism, plan_id)` has no default mechanism and
uses the same exact one-time destructive disclosure. A returned command only
proves provider acceptance; unless current-session preservation, identity, and
regions are freshly observed, reconnect, validate, and refresh the safety map.

## Non-hardware import check

```text
uv run --locked python -c "import firmware_mcp.server"
```

The live tool schemas and their descriptions remain the executable contract.
