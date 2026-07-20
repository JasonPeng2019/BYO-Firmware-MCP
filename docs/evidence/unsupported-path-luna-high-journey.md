# LabRig-Unknown-NRF-A real-hardware journey

## Run metadata

- Model: `gpt-5.6-luna`
- Effort: `high`
- Current MCP run: `run-20260720T161539Z-bd31938c`
- Familiar board name: `LabRig-Unknown-NRF-A`
- Logical board id: `labrig_unknown_nrf_a`
- Probe UID: `683377322`
- UART: `COM11`, 115200 baud
- Final state: `RUNNING`
- Prohibited actions: no unlock, erase, recovery, bootloader flash, application reflash, memory write, register write, CPU write, or connection override.

## Unsupported setup and research evidence

The initial `setup_overview` route for the exact familiar name was an unknown-name setup route; it did not select `nRF52840-DK` or another catalog/profile shortcut. Live preflight initially reported no exact target. The selected physical evidence was J-Link probe UID `683377322` and J-Link CDC UART `COM11`.

The server requested independent target research. The supplied datasheet digest was:

```text
c619e336b9c0610663273041f057f2537a65fd408ce0c5b8214a26de2aa88422
```

Research supplied to `continue_setup`:

- Official pack: `NordicSemiconductor::nRF_DeviceFamilyPack`, version `8.44.1`.
- Official pack URL: `https://developer.nordicsemi.com/nRF5_SDK/pieces/nRF_DeviceFamilyPack/NordicSemiconductor.nRF_DeviceFamilyPack.8.44.1.pack`
- Pack SHA-256: `95136B57A8310BA367AA46CB66C8D149561FC7876C5F3736F323A9DBFB8F559E`
- PDSC device: `nRF52840_xxAA`, Cortex-M4, nRF52840 SVD/startup/linker/flash support.
- Derived pyOCD target accepted by setup: `nrf52840_xxaa`.
- Setup continuation status: `setup_continuation_accepted` / `target_and_pack`.
- Setup completion and validation: `setup_completed`, then `validation_passed`.
- Initial safety-map digest: `bdb4d63d155978791561949c22d8f5e103ffd3c81ac5162db9851011e24855fe`.

The first setup attempt was an orchestrator test-design error: the initial known-name call omitted the required friendly probe assignment and correctly returned `setup_assignment_required`. Retrying with the selected J-Link assignment routed the existing intended physical connection; this was not a hardware or profile failure.

## Build, artifact, and deployment evidence reused

The exact native-build helper invocation used the server-returned helper and literal argv:

```text
C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\.venv\Scripts\python.exe -m pyocd_debug_mcp.native_build --project-dir C:\Users\Jason\AppData\Local\Temp\byo-unsupported-nrf-luna-high-r2-20260720-083217 --build-dir C:\Users\Jason\AppData\Local\Temp\byo-unsupported-nrf-luna-high-r2-20260720-083217 --cwd C:\Users\Jason\AppData\Local\Temp\byo-unsupported-nrf-luna-high-r2-20260720-083217 --artifact elf=build/labrig_unknown_nrf_a.elf --artifact map=build/labrig_unknown_nrf_a.map --artifact hex=build/labrig_unknown_nrf_a.hex -- C:\Users\Jason\AppData\Local\Programs\Python\Python314\Scripts\ninja.exe -f build.ninja
```

Canonical artifact hashes:

```text
ELF  ba172729f49e90388059cb9f923e0822a36400bb8de6102e3108c127fd9d33a4
HEX  574a141b0a73fc717739c2abafb76411fe624c0fa00bc33e041eb68567699096
MAP  55e43da351fddea646eb304b0db707206a716bc1672b54c1867278e9a5c84b92
```

Flash was performed once through application plan `plan-a2914ef95c3b7699`; the server reported the ELF contained within the mapped application partition and left the target running.

## Plan IDs

Accepted guarded plans from the journey:

```text
plan-76c9186f775bd303  initial board setup
plan-a2914ef95c3b7699  guarded application flash
plan-296d2e3daa7a4e44    initial UART boot capture
plan-8fb3bc24c39eb390    five-command serial exchange
plan-ae02bc1c72c002ae    timing serial exchange
plan-16a32ac529c0c928    led_task breakpoint
plan-ae70172e5d30b6ea    mapped led_task entry read
plan-960b8073f89187a7    mapped uart_task entry read
plan-30601e9b88a375de    mapped UARTE0 BAUDRATE read
plan-559d33eee3d125ef    reset-and-halt
plan-1571c74add0c03e3    standalone write_serial
plan-a33865a4a0c6d923    standalone read_serial
```

## UART and debug evidence

Initial boot capture:

```text
@0 BOOT LabRig-Unknown-NRF-A
@2 LED toggle 1
@504 LED toggle 2
@1004 LED toggle 3
@2006 PRINT heartbeat 2006
```

Console exercise matched all five responses:

```text
@188150 STATUS tick 188150
@188150 STATUS led_ms 500
@188153 STATUS print_ms 2000
@188369 LED interval_ms 1000
@188589 PRINT interval_ms 1000
@188808 CMD unknown 5
@189027 LED rejected_ms 10
```

Post-change cadence evidence:

```text
@284576 STATUS led_ms 1000
@284578 STATUS print_ms 1000
@285006 PRINT heartbeat 285006
@285504 LED toggle 475
@286006 PRINT heartbeat 286006
@286504 LED toggle 476
```

The post-change LED and heartbeat intervals were each 1000 ms. Standalone `write_serial` reported `UART wrote 7 byte(s) on COM11 at 115200 baud`; the subsequent standalone `read_serial` succeeded for 3.01 seconds and captured live background events, but not the status response because separate port opens allowed the short response to occur between write and read.

Debug evidence from the collected ELF:

```text
PC before breakpoint: 0x000003F6
MSP/SP:              0x2003FFF0
scheduler_tick @ 0x20000124 = 0x000658BC
scheduler_run_count @ 0x20000120 = 0x00EC0522
led_toggle_count @ 0x2000011C = 0x0000025D
led_interval_ms @ 0x20000004 = 0x000003E8
print_interval_ms @ 0x20000000 = 0x000003E8
led_task_runs @ 0x20000110 = 0x00EC0522
uart_task_runs @ 0x2000010C = 0x00EC0522
UARTE0 BAUDRATE @ 0x40002524: 00 00 D6 01 (0x01D60000)
```

Breakpoint evidence: `led_task` resolved at `0x0000019D`; the breakpoint was hit with PC `0x0000019C`, removed, one step returned `pc=0x0000019E`, and final state was `RUNNING`.

## Failures and diagnoses

1. Initial known-name assignment omission: orchestrator test-design error, corrected by retrying `setup_overview` with the J-Link assignment.
2. First symbol reads used width `4`; exact refusal was `Refused [memory/invalid-width]: width must be one of: 8, 16, 32`. Corrected to width `32`.
3. Prior `read_memory_symbol(led_task)` produced an `AssertionError` and disconnected the session. This was classified as a server function-symbol handling defect. After the server fix, the exact retest returned the actionable policy refusal below, and PC/scheduler reads immediately succeeded:

```text
Refused [memory/symbol-is-function]: Symbol 'led_task' is an executable function, not a data object. Use find_symbol and the breakpoint tools; use the planned mapped-address read only when inspecting code bytes is intentional. session_id=20260720T161407Z-e28e8a0d
```

4. A serial-exchange timing attempt with `expected_text=null` was refused because `steps[0].expected_text must be non-empty text`; it consumed no plan call. The corrected exchange used event text as the expected match and proved the 1000 ms cadence.
5. Direct `board_safety_refresh` initially returned `setup_tool_not_loaded`; loading the server-named setup tool corrected the orchestration. Refresh then completed with `validation_required=false` and map digest `fbe3b420e080f1a6bf8240155a5c82109f88264bcc1aa089af72e55df27d9423`.
6. Function-symbol reads through `read_memory_symbol` are now policy-refused rather than asserted; code-byte inspection used the guarded mapped-read plan only where intentional.

## Complete visible-tool matrix

Status meanings: **live** = executed in this regression run; **NULL-only** = only the all-NULL teaching/safety initializer was called; **earlier** = proven in the prior journey and intentionally reused.

| Visible tool | Matrix result |
|---|---|
| `initialization_handshake` | live |
| `setup_overview` | live; exact profile routed with J-Link assignment |
| `load_setup_tool` | live; `board_validate` and `board_safety_refresh` guidance loaded |
| `board_validate` | live; passed before work and after disconnect/reconnect |
| `get_board_info` | live; returned profile, target, baud, silicon identity |
| `get_setup_status` | earlier; setup ready and UART ready on COM11 |
| `connect` | live; connected/already-connected result and post-revalidation reconnect |
| `disconnect` | live; disconnected successfully |
| `get_state` | live; RUNNING before/after operations and final RUNNING |
| `wait` | live; waited 100 ms |
| `halt` | live; final safe CPU-register read check |
| `resume` | live; final board resume |
| `step` | earlier; one step from breakpoint returned `0x0000019E` |
| `reset_and_halt-plan` | live; all-NULL teaching then populated and executed |
| `reset_and_run` | live; reset and running after reset-halt |
| `read_cpu_register` | live; `r0=0x2003FF10` |
| `read_execution_state` | live; PC/MSP after reset-halt; earlier breakpoint PC too |
| `find_symbol` | live; `led_task@0x0000019D` |
| `read_memory_symbol` | live; data symbols read; executable `led_task` policy-refused |
| `remove_breakpoint` | earlier; removed `0x0000019D` |
| `collect_build_artifacts` | earlier; ELF/HEX/MAP canonical provenance |
| `action_batch` | live; exact server fallbacks executed |
| `read_memory_address-plan` | earlier; task entry bytes and UARTE0 register |
| `set_breakpoint-plan` | earlier; `led_task` breakpoint hit |
| `flash_application-plan` | earlier; one authorized application flash only |
| `read_serial-plan` | live; standalone read and earlier boot/timing captures |
| `write_serial-plan` | live; standalone harmless `status` write |
| `serial_exchange-plan` | earlier; console command and timing proofs |
| `board_safety_refresh` | live; completed, `validation_required=false` |
| `board_setup-plan` | earlier; initial unsupported setup route only |
| `continue_setup` | earlier; official pack/target research accepted |
| `connect_override-plan` | NULL-only; teaching mechanism returned, not populated |
| `connect_under_reset-plan` | NULL-only; teaching mechanism returned, not populated |
| `write_cpu_register-plan` | NULL-only; teaching mechanism returned, not populated |
| `set_execution_state-plan` | NULL-only; teaching mechanism returned, not populated |
| `register_write-plan` | NULL-only; teaching mechanism returned, not populated |
| `write_memory-plan` | NULL-only; teaching mechanism returned, not populated |
| `flash_bootloader-plan` | NULL-only; teaching mechanism returned, not populated |
| `target_unlock-plan` | NULL-only; teaching mechanism returned, not populated |

The all-NULL-only tools each returned `Plan initialization for <tool>-plan` and included the server teaching mechanism; none exposed or executed a real action. `flash_application-plan`, `set_breakpoint-plan`, and all safe read plans were covered by prior live evidence and were not repeated destructively.

## Final state

The board was revalidated, disconnected and revalidated/reconnected, reset-and-halted and reset-and-run, then resumed after the final read-only CPU check. Final `get_state` result:

```text
RUNNING
```
