# Current plan-tool contract

This document is generated from `guardrails/plan_defs.py`, the runtime source of truth.
Do not edit field lists by hand. Regenerate it after changing a plan definition; the
contract test compares this entire file with the live deterministic rendering.
Historical design prose remains under `archive_docs/` and is not runtime authority.

Every plan tool is first called with its complete NULL envelope. A populated call accepts
only the plan JSON object, binds the exact action parameters below, and rejects extra fields.

## `board_setup-plan`

- Action: `board_setup`
- Purpose: Create or repair one logical board profile and its safety evidence.
- Budget mode: `fixed`
- Permission mode: `required`
- Safety mode: `session`
- Timeout: `300` seconds
- Populated plan fields, in order: `board_id`, `hypothesis`, `strategy`, `hypothesis_made`, `strategy_evaluated`, `expected_fail_return`, `expected_success_return`, `max_calls`, `max_calls_buffer`, `action_parameters`, `user_permission`
- Exact action-parameter fields, in order: `mode`, `connection_id`, `display_name`, `mcu_part_number`, `requires_uart`, `serial_baudrate`, `serial_id`, `datasheet_path`

| Action field | Type | Constraints | Description |
| --- | --- | --- | --- |
| `mode` | `text` | required; choices='setup', 'repair' | Exactly setup or repair. |
| `connection_id` | `text` | required | Intended enumerated physical connection. |
| `display_name` | `text` | required | User-provided familiar board name. |
| `mcu_part_number` | `text` | required | Exact user-provided MCU part number. |
| `requires_uart` | `boolean` | required | True only when this firmware workflow uses UART. |
| `serial_baudrate` | `integer` | nullable; >= 1 | Positive UART baud rate when requires_uart is true; otherwise NULL. |
| `serial_id` | `text` | nullable | Stable UART identity selected from current setup inventory; the server resolves its current port path at execution time; NULL when UART is unused. |
| `datasheet_path` | `text` | required | Local authoritative PDF datasheet path supplied by the user. |

Extra instructions: Do not guess hardware choices or rewrite the user-supplied MCU part number. The server resolves the current UART port and computes the datasheet digest.

## `connect_override-plan`

- Action: `connect_override`
- Purpose: Connect using explicitly reviewed exceptional identifiers without changing profiles.
- Budget mode: `flexible`
- Permission mode: `none`
- Safety mode: `session`
- Timeout: `30` seconds
- Populated plan fields, in order: `board_id`, `hypothesis`, `strategy`, `hypothesis_made`, `strategy_evaluated`, `expected_fail_return`, `expected_success_return`, `max_calls`, `max_calls_buffer`, `action_parameters`
- Exact action-parameter fields, in order: `probe_uid`, `target_override`, `external_board_config`

| Action field | Type | Constraints | Description |
| --- | --- | --- | --- |
| `probe_uid` | `text` | nullable | Manual stable probe identifier. |
| `target_override` | `text` | nullable | Manual pyOCD target. |
| `external_board_config` | `text` | nullable | External board configuration path. |

Extra instructions: Override values are run-scoped and never silently update a profile.

## `connect_under_reset-plan`

- Action: `connect_under_reset`
- Purpose: Attach while physical reset is asserted, then halt and release reset.
- Budget mode: `flexible`
- Permission mode: `none`
- Safety mode: `session`
- Timeout: `30` seconds
- Populated plan fields, in order: `board_id`, `hypothesis`, `strategy`, `hypothesis_made`, `strategy_evaluated`, `expected_fail_return`, `expected_success_return`, `max_calls`, `max_calls_buffer`, `action_parameters`
- Exact action-parameter fields, in order: `probe_uid`, `target_override`

| Action field | Type | Constraints | Description |
| --- | --- | --- | --- |
| `probe_uid` | `text` | nullable | Stable probe identifier. |
| `target_override` | `text` | nullable | Exact target override. |

Extra instructions: Fail clearly when the probe has no wired reset-line support; do not degrade silently.

## `flash_application-plan`

- Action: `flash_application`
- Purpose: Flash a validated artifact wholly inside the application partition.
- Budget mode: `fixed`
- Permission mode: `none`
- Safety mode: `fresh-write`
- Timeout: `120` seconds
- Populated plan fields, in order: `board_id`, `hypothesis`, `strategy`, `hypothesis_made`, `strategy_evaluated`, `expected_fail_return`, `expected_success_return`, `max_calls`, `max_calls_buffer`, `action_parameters`
- Exact action-parameter fields, in order: `artifact`

| Action field | Type | Constraints | Description |
| --- | --- | --- | --- |
| `artifact` | `text` | required | Local ELF or HEX artifact path. |

Extra instructions: Load addresses come only from the artifact; no caller-supplied address or allowed range is accepted.

## `flash_bootloader-plan`

- Action: `flash_bootloader`
- Purpose: Flash a validated artifact wholly inside the bootloader partition.
- Budget mode: `fixed`
- Permission mode: `required`
- Safety mode: `fresh-write`
- Timeout: `120` seconds
- Populated plan fields, in order: `board_id`, `hypothesis`, `strategy`, `hypothesis_made`, `strategy_evaluated`, `expected_fail_return`, `expected_success_return`, `max_calls`, `max_calls_buffer`, `action_parameters`, `user_permission`
- Exact action-parameter fields, in order: `artifact`

| Action field | Type | Constraints | Description |
| --- | --- | --- | --- |
| `artifact` | `text` | required | Local ELF or HEX artifact path. |

Extra instructions: Load addresses come only from the artifact. Permission is partition-specific and never authorizes application or prohibited ranges.

## `read_memory_address-plan`

- Action: `read_memory_address`
- Purpose: Read a mapped address or bounded memory block when symbol access is unsuitable.
- Budget mode: `flexible`
- Permission mode: `none`
- Safety mode: `validated-read`
- Timeout: `30` seconds
- Populated plan fields, in order: `board_id`, `hypothesis`, `strategy`, `hypothesis_made`, `strategy_evaluated`, `expected_fail_return`, `expected_success_return`, `max_calls`, `max_calls_buffer`, `action_parameters`
- Exact action-parameter fields, in order: `address`, `width`, `length`

| Action field | Type | Constraints | Description |
| --- | --- | --- | --- |
| `address` | `text-or-integer` | required | Exact address. |
| `width` | `integer` | required; choices=8, 16, 32 | Transfer width: 8, 16, or 32 bits. |
| `length` | `integer` | nullable; >= 1; <= 65536 | Optional block length up to 64 KiB. |

Extra instructions: Prefer read_memory_symbol when debug metadata identifies the value.

## `read_serial-plan`

- Action: `read_serial`
- Purpose: Capture bounded UART output from the selected board.
- Budget mode: `flexible`
- Permission mode: `none`
- Safety mode: `validated-read`
- Timeout: `30` seconds
- Populated plan fields, in order: `board_id`, `hypothesis`, `strategy`, `hypothesis_made`, `strategy_evaluated`, `expected_fail_return`, `expected_success_return`, `max_calls`, `max_calls_buffer`, `action_parameters`
- Exact action-parameter fields, in order: `expected_text`, `read_seconds`, `baudrate`, `port`, `reset_on_open`, `on_exit`

| Action field | Type | Constraints | Description |
| --- | --- | --- | --- |
| `expected_text` | `text` | nullable | Optional expected text. |
| `read_seconds` | `number` | required; > 0 | Positive bounded capture duration. |
| `baudrate` | `integer` | nullable; >= 1 | Positive baud rate. |
| `port` | `text` | nullable | Current serial port path. |
| `reset_on_open` | `boolean` | required | Reset after opening the port. |
| `on_exit` | `object` | nullable | Optional exact structured uart_write or reset_and_run finalizer. |

Extra instructions: A port path is runtime-only; it is never persisted as attachment identity.

## `register_write-plan`

- Action: `register_write`
- Purpose: Apply one masked value to a documented peripheral register.
- Budget mode: `fixed`
- Permission mode: `none`
- Safety mode: `fresh-write`
- Timeout: `30` seconds
- Populated plan fields, in order: `board_id`, `hypothesis`, `strategy`, `hypothesis_made`, `strategy_evaluated`, `expected_fail_return`, `expected_success_return`, `max_calls`, `max_calls_buffer`, `action_parameters`
- Exact action-parameter fields, in order: `address`, `mask`, `value`

| Action field | Type | Constraints | Description |
| --- | --- | --- | --- |
| `address` | `text-or-integer` | required | Exact documented register address. |
| `mask` | `text-or-integer` | required | Exact documented field mask. |
| `value` | `text-or-integer` | required | Exact value to apply. |

Extra instructions: Security, provisioning, option-byte, OTP, and lifecycle registers are unavailable.

## `reset_and_halt-plan`

- Action: `reset_and_halt`
- Purpose: Reset the selected board and halt immediately at startup.
- Budget mode: `flexible`
- Permission mode: `none`
- Safety mode: `session`
- Timeout: `30` seconds
- Populated plan fields, in order: `board_id`, `hypothesis`, `strategy`, `hypothesis_made`, `strategy_evaluated`, `expected_fail_return`, `expected_success_return`, `max_calls`, `max_calls_buffer`, `action_parameters`
- Exact action-parameter fields, in order: none

| Action field | Type | Constraints | Description |
| --- | --- | --- | --- |
| _none_ | - | - | - |

Extra instructions: This reset does not unlock a protected target.

## `serial_exchange-plan`

- Action: `serial_exchange`
- Purpose: Run a bounded multi-step UART conversation through one state-preserving port open.
- Budget mode: `flexible`
- Permission mode: `none`
- Safety mode: `fresh-write`
- Timeout: `30` seconds
- Populated plan fields, in order: `board_id`, `hypothesis`, `strategy`, `hypothesis_made`, `strategy_evaluated`, `expected_fail_return`, `expected_success_return`, `max_calls`, `max_calls_buffer`, `action_parameters`
- Exact action-parameter fields, in order: `steps`, `read_seconds`, `baudrate`, `port`, `ready_text`, `ready_seconds`, `ready_probe_text`, `ready_probe_line_ending`, `ready_probe_delay_seconds`, `clear_input`

| Action field | Type | Constraints | Description |
| --- | --- | --- | --- |
| `steps` | `array` | required; min_items=1; max_items=8 | One to eight exact {text, expected_text, line_ending} command/response steps. |
| `read_seconds` | `number` | required; > 0; <= 30 | Positive per-step response window no greater than 30 seconds. |
| `baudrate` | `integer` | nullable; >= 1 | Positive baud rate. |
| `port` | `text` | nullable | Current serial port path. |
| `ready_text` | `text` | nullable | Optional text to await after opening the UART and before sending. |
| `ready_seconds` | `number` | required; >= 0; <= 30 | Bounded pre-send readiness window; zero when ready_text is NULL. |
| `ready_probe_text` | `text` | nullable; empty allowed | Optional exact bounded text sent once to elicit the readiness marker. |
| `ready_probe_line_ending` | `text` | required; choices='none', 'lf', 'cr', 'crlf' | Line ending for the optional readiness probe: none, lf, cr, or crlf. |
| `ready_probe_delay_seconds` | `number` | required; >= 0; <= 30 | Optional bounded observation delay before sending the readiness probe; use it after flash/reset so boot output can arrive first. |
| `clear_input` | `boolean` | required | Discard buffered input after open only when true; false preserves boot/prompt bytes. |

Extra instructions: All steps, readiness input, and the optional pre-probe delay are exact, bounded, and execute through one port open; successful cleanup preserves application state.

## `set_breakpoint-plan`

- Action: `set_breakpoint`
- Purpose: Set a breakpoint at one mapped executable symbol or address.
- Budget mode: `fixed`
- Permission mode: `none`
- Safety mode: `fresh-write`
- Timeout: `30` seconds
- Populated plan fields, in order: `board_id`, `hypothesis`, `strategy`, `hypothesis_made`, `strategy_evaluated`, `expected_fail_return`, `expected_success_return`, `max_calls`, `max_calls_buffer`, `action_parameters`
- Exact action-parameter fields, in order: `symbol_or_address`, `elf_artifact`

| Action field | Type | Constraints | Description |
| --- | --- | --- | --- |
| `symbol_or_address` | `text-or-integer` | required | Exact symbol or address. |
| `elf_artifact` | `text` | required | Current local ELF whose executable sections contain the breakpoint. |

Extra instructions: The resolved location must be in an executable section of the plan-bound current ELF and supported by the connected core.

## `set_execution_state-plan`

- Action: `set_execution_state`
- Purpose: Change a CPU control-flow or execution-mode register.
- Budget mode: `fixed`
- Permission mode: `required`
- Safety mode: `fresh-write`
- Timeout: `30` seconds
- Populated plan fields, in order: `board_id`, `hypothesis`, `strategy`, `hypothesis_made`, `strategy_evaluated`, `expected_fail_return`, `expected_success_return`, `max_calls`, `max_calls_buffer`, `action_parameters`, `user_permission`
- Exact action-parameter fields, in order: `name`, `value`

| Action field | Type | Constraints | Description |
| --- | --- | --- | --- |
| `name` | `text` | required | Supported execution-state register name. |
| `value` | `text-or-integer` | required | Exact non-negative hexadecimal or decimal value; execution-state registers are 32-bit. |

Extra instructions: Permission does not make unsupported or security-related registers writable.

## `target_unlock-plan`

- Action: `target_unlock`
- Purpose: Perform one documented destructive vendor recovery operation.
- Budget mode: `fixed`
- Permission mode: `fresh-one-time`
- Safety mode: `session`
- Timeout: `300` seconds
- Populated plan fields, in order: `board_id`, `hypothesis`, `strategy`, `hypothesis_made`, `strategy_evaluated`, `expected_fail_return`, `expected_success_return`, `max_calls`, `max_calls_buffer`, `action_parameters`, `user_permission`
- Exact action-parameter fields, in order: `recovery_mechanism`

| Action field | Type | Constraints | Description |
| --- | --- | --- | --- |
| `recovery_mechanism` | `text` | nullable | Exact documented vendor recovery mechanism; NULL requests research when unknown. |

Extra instructions: Erase facts are server-derived from the current safety map. First submit with user_permission=NULL to receive the plan-id-bound disclosure, then resubmit every other field unchanged with user_permission=one-time. Full-session permission never applies.

## `write_cpu_register-plan`

- Action: `write_cpu_register`
- Purpose: Write an ordinary general-purpose or floating-point CPU register.
- Budget mode: `fixed`
- Permission mode: `none`
- Safety mode: `fresh-write`
- Timeout: `30` seconds
- Populated plan fields, in order: `board_id`, `hypothesis`, `strategy`, `hypothesis_made`, `strategy_evaluated`, `expected_fail_return`, `expected_success_return`, `max_calls`, `max_calls_buffer`, `action_parameters`
- Exact action-parameter fields, in order: `name`, `value`

| Action field | Type | Constraints | Description |
| --- | --- | --- | --- |
| `name` | `text` | required | Supported ordinary CPU register name. |
| `value` | `text-or-integer` | required | Exact non-negative hexadecimal or decimal value within the selected register width: R/S are 32-bit, D is 64-bit, and Q is 128-bit. |

Extra instructions: Control-flow, security, and provisioning registers are excluded.

## `write_memory-plan`

- Action: `write_memory`
- Purpose: Write one symbol-backed value or an explicitly justified mapped RAM address.
- Budget mode: `fixed`
- Permission mode: `none`
- Safety mode: `fresh-write`
- Timeout: `30` seconds
- Populated plan fields, in order: `board_id`, `hypothesis`, `strategy`, `hypothesis_made`, `strategy_evaluated`, `expected_fail_return`, `expected_success_return`, `max_calls`, `max_calls_buffer`, `action_parameters`
- Exact action-parameter fields, in order: `symbol_or_address`, `value`, `width`, `allow_address_fallback`, `reason`

| Action field | Type | Constraints | Description |
| --- | --- | --- | --- |
| `symbol_or_address` | `text-or-integer` | required | Exact symbol or address. |
| `value` | `json` | required | Exact JSON-representable value. |
| `width` | `integer` | required; choices=8, 16, 32 | Transfer width: 8, 16, or 32 bits. |
| `allow_address_fallback` | `boolean` | required | Explicit raw-address fallback. |
| `reason` | `text` | nullable | Reason symbol access is unsuitable. |

Extra instructions: Prefer symbols; raw addresses require fallback=true and a concrete reason.

## `write_serial-plan`

- Action: `write_serial`
- Purpose: Send bounded UTF-8 text over the selected board's UART.
- Budget mode: `flexible`
- Permission mode: `none`
- Safety mode: `fresh-write`
- Timeout: `30` seconds
- Populated plan fields, in order: `board_id`, `hypothesis`, `strategy`, `hypothesis_made`, `strategy_evaluated`, `expected_fail_return`, `expected_success_return`, `max_calls`, `max_calls_buffer`, `action_parameters`
- Exact action-parameter fields, in order: `text`, `baudrate`, `port`, `append_newline`, `timeout_seconds`, `on_exit`

| Action field | Type | Constraints | Description |
| --- | --- | --- | --- |
| `text` | `text` | required | Exact text to send. |
| `baudrate` | `integer` | nullable; >= 1 | Positive baud rate. |
| `port` | `text` | nullable | Current serial port path. |
| `append_newline` | `boolean` | required | Append one newline when true. |
| `timeout_seconds` | `number` | required; > 0 | Positive bounded write timeout. |
| `on_exit` | `object` | nullable | Optional exact structured uart_write or reset_and_run finalizer. |

Extra instructions: The text and all transport parameters are bound exactly by the plan.
