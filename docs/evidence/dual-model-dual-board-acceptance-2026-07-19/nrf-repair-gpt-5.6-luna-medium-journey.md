# nRF52840 acceptance journey

## Session and provenance

- Model/effort: GPT-5.6-luna, medium effort.
- pyOCD server run: `run-20260720T103003Z-8216c734` (`2026-07-20T10:30:03.314097Z`).
- Board: nRF52840-DK, package marking `nRF52840-QIAA`, probe UID `683377322`, UART COM11 at 115200.
- Accepted provenance conflict: a GPT-5.6-luna medium session authored the canonical frozen fixture, and this GPT-5.6-luna medium session sat the exam. The fixture was frozen and independently audited against an external `bugs.yaml` that is not present in this repository.
- No module was rewritten or regenerated. The RTS/CTS red-herring line was intentionally unchanged:

  `REG32(UART_PSELRTS)=0xFFFFFFFFu; REG32(UART_PSELCTS)=0xFFFFFFFFu;`

## Setup and validation

1. Performed `initialization_handshake` first. The server required setup routing, local-first dependency discovery, the provider-neutral native-build helper, and guarded plan flows.
2. `setup_overview` initially found the requested nRF52840-DK profile incomplete and asked for the friendly probe assignment. Bound the board to the J-Link ending `377322` and COM11.
3. Loaded `board_setup-plan`; all-NULL teaching call completed. Accepted setup plan: `plan-7b83d53c34714e18`.
4. Used the exact local facts: `PARTNUMBER` = `nRF52840-QIAA`; `datasheet.pdf`; UART required at 115200; J-Link `probe:683377322`; serial identity `000683377322`.
5. Executed setup through the unchanged `action_batch` fallback because the dynamically exposed `board_setup` binding was not present. Setup completed with target `nrf52840`, exact live MCU identity, verified safety map, and automatic validation. Setup attempt: `setup-attempt-917655d402571cab`; setup continuation: `setup-continuation-623568a59eb0772a`.
6. Setup evidence included datasheet SHA-256 `c619e336b9c0610663273041f057f2537a65fd408ce0c5b8214a26de2aa88422`, safety-map digest `9f4453fbffea5f5e7a6557a070d55328d11338b05ce3dac25acfb30382385187`, and exact detected target `nrf52840`.
7. `get_setup_status` reported `configuration_ready=true`, `live_session_ready=true`, `ready_for_code=true`, `ready_for_uart_work=true`, and returned the general native-build helper guidance.
8. After later reconnects, setup routing/validation was repeated. Validation observed `FICR INFO.PART exact part identifier 0x00052840` and passed. Reconnect session IDs included `20260720T104113Z-8dae274e` (initial diagnosis session), `20260720T104113Z-122a3ef7`, `20260720T104429Z-509dfe93`, `20260720T104634Z-ec8a2c68`, `20260720T104738Z-009ef2e1`, and `20260720T105019Z-3cc37ab1`.

## Build workflow and failures

The project declared a Windows Makefile, but bounded local-first discovery found the compatible ARM GCC and objcopy under STM32CubeIDE and no `make.exe`; `ninja.exe` existed but was not the project's build provider. The provider-neutral helper was used directly with exact argv after `--`; no Zephyr-specific helper or backup was used.

General helper executable:

`C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\.venv\Scripts\python.exe -m pyocd_debug_mcp.native_build`

Unchanged baseline command (compiler evidence):

`...native_build --project-dir C:\firmcli-acceptance-20260720\nrf-repair-luna-r1 --build-dir C:\firmcli-acceptance-20260720\nrf-repair-luna-r1\baseline-build --cwd C:\firmcli-acceptance-20260720\nrf-repair-luna-r1 --artifact-elf ...\baseline-build\firmware.elf --artifact-map ...\baseline-build\firmware.map -- arm-none-eabi-gcc.exe -mcpu=cortex-m4 -mthumb -mfloat-abi=soft -ffreestanding -fno-builtin -fdata-sections -ffunction-sections -Os -g3 -Wall -Wextra -c main.c -o baseline-build\main.o`

Raw baseline compiler failure:

`main.c:115:61: error: called object is not a function or function pointer`

The cause was the missing semicolon in `uart_init` between `REG32(UART_CONFIG)=0u` and `REG32(UART_ENABLE)=...`.

Intermediate helper builds compiled `startup.s` and `main.c`, linked with the declared linker flags, and generated HEX through the same general helper. One attempted helper invocation used `cmd.exe /d /c` with an inline compound command; the helper returned exit code 0 but reported the declared ELF absent and no files were produced. This was diagnosed as an unreliable inline command wrapper and replaced for the final build by one truthful helper invocation running a temporary exact build script. The temporary script was removed after artifact collection.

Final truthful build invocation (the only final-artifact build invocation):

`C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\.venv\Scripts\python.exe -m pyocd_debug_mcp.native_build --project-dir C:\firmcli-acceptance-20260720\nrf-repair-luna-r1 --build-dir C:\firmcli-acceptance-20260720\nrf-repair-luna-r1\build --cwd C:\firmcli-acceptance-20260720\nrf-repair-luna-r1 --artifact-elf C:\firmcli-acceptance-20260720\nrf-repair-luna-r1\build\firmware.elf --artifact-map C:\firmcli-acceptance-20260720\nrf-repair-luna-r1\build\firmware.map -- cmd.exe /d /c build_final.cmd`

That invocation compiled `startup.s` and `main.c` with the Makefile flags, linked `linker.ld`, and generated `firmware.elf`, `firmware.hex`, and `firmware.bin`. It returned exit code 0 with loadable-ELF verification. `build_final.cmd` was then removed; firmware source and module structure were not rewritten.

## Confirmed defects and minimal edits

1. Syntax: added the missing semicolon after `REG32(UART_CONFIG)=0u`.
2. Peripheral configuration: changed `REG32(GPIO_PIN_CNF(RX_PIN))=1u<<1` to `REG32(GPIO_PIN_CNF(RX_PIN))=0u`. Live GPIO evidence showed TX P0.6 `PIN_CNF=0x1` and RX P0.8 `PIN_CNF=0x2` (input disconnected). Before the edit, `status` wrote 8 bytes but produced no response; `uart_rx_count=0`.
3. Memory corruption: changed `used<=sizeof(line)-1u` to `used<sizeof(line)-1u`. The ELF placed `line.0` at `0x20000018`, size 48, immediately followed by `used.1` at `0x20000048`; the old terminator path could write `line[48]` into adjacent state. A 48-character boundary command was delivered and produced no response but no fault; the symbol layout and source path proved the out-of-bounds write.
4. Synchronization/counter ownership: after the RX repair, a known-length `status\r\n` exchange reported 8 bytes written. Before input, live `uart_rx_count=0`; after immediate halt, it was `0x00000002`. Source audit found increments in both `UARTE0_UART0_IRQHandler` and `console_task`, with the consumer increment outside the dequeue IRQ exclusion. The minimal edit removed only the consumer-side `uart_rx_count++`; the ISR became the sole byte-count owner.

No other suspicious scheduler/queue code was changed. Queue head/tail observations were consistent at the relevant checkpoints, queue drops remained zero, and Cortex-M fault status was clear. The counter exchange also showed the raw symptom rather than being treated as green: the 8-byte write did not produce the expected 8-count delta, which triggered the ownership audit and final counter fix.

## Guarded flash history

Every application flash used `flash_application-plan` with an all-NULL teaching call, then an exact populated plan, then `flash_application` through the unchanged single-child `action_batch` fallback when the direct action binding was not exposed. No unlock, mass erase, bootloader flash, or bypass was used.

### Baseline after syntax repair

- Plan: `plan-76f2460c239c2a76`.
- Canonical ELF: `collected-baseline\firmware.elf`.
- Flash result: application image flashed within mapped application partition; target left running.

### RX-pin repair image

- Plan: `plan-cdbd048b6a3a8865`.
- Canonical ELF: `collected-rxfix\firmware.elf`.
- UART proof after flash: `status` exchange matched and returned `status=127577`, `led_ms=1000 print_ms=3000`.

### Final pre-counter image

- Plan: `plan-057d0b3d47fb0339`.
- Canonical ELF: `collected-final\firmware.elf`.
- Flash result: application image flashed within mapped application partition; target left running.

### Counter-fix image

- Plan: `plan-bb6c146c51bb1d99`.
- Canonical ELF: `collected-counterfix\firmware.elf`.
- Flash result: application image flashed within mapped application partition; target left running.

## Artifact hashes

| Collection | Artifact | SHA-256 |
|---|---|---|
| `collected-baseline` | `firmware.elf` | `d3f220f60999f6edb88ce9409415d28aaa7dc9e6536de18d174a3451b2321517` |
| `collected-baseline` | `firmware.map` | `8bad6e9b88a19bfeef72db4ac2488edb645815ee4f1ce33b3c7daab0c896aa39` |
| `collected-rxfix` | `firmware.elf` | `2e2ab06e5510a224327ab00eb6b79a457e8088d8debb811bedaaee1bdf56712c` |
| `collected-rxfix` | `firmware.hex` | `dec664c135f6e2d23bb5c47a2c34cb909a35b85d945f0bf050b22c5b7f01ec19` |
| `collected-rxfix` | `firmware.map` | `9f10b20a5a704b49319e8817214a2c4e92d23c492fbb3ea9d876aa4db8eb18b0` |
| `collected-final` | `firmware.elf` | `84a2e2c10367d27e2d60473cc1492c3db716b11063d33527404f2ffcdc6c82f6` |
| `collected-final` | `firmware.hex` | `ee264e5f80716b349966686130ee64fa8c1ebfe3dc41b25f36fe4573a50df008` |
| `collected-final` | `firmware.map` | `2118756b4738cf388612eaad430dcf5363d77fcc89ca7fe43a8921d223dec10b` |
| `collected-counterfix` | `firmware.elf` | `b8462c3a1116454eb0900985bbdb0098f18902f470f91f2674f561ae46ac7aa2` |
| `collected-counterfix` | `firmware.hex` | `e88f6251db4e81ff969b594a36cc80d854772d2419f00fbbeaeb3089bbd4dbc1` |
| `collected-counterfix` | `firmware.bin` | `0431f5a468edc9bb13c789cbc32c9a7ce3919582d7631dea5d1f1b31bf41e025` |
| `collected-counterfix` | `firmware.map` | `2990aa9d64d8b6b2cce92ae2621fd2e1c7ec30a49be45d19f2185aabbeb6cec0` |

## UART evidence and failures

- First baseline capture after flash included:

  `[2 ms] LED_TOGGLE=1`, `[2 ms] PERIODIC_PRINT=1`, `[1002 ms] LED_TOGGLE=2`, `[2002 ms] LED_TOGGLE=3`.

- Baseline status before RX repair: `UART exchange did not match on COM11 at 115200 baud; wrote 8 byte(s); duration=3.08s; ready=matched; ready_probe_bytes=0; steps=1 [1:status==did not match]; excerpt=(none)`.
- After RX repair: `UART exchange matched on COM11 at 115200 baud; wrote 8 byte(s); duration=0.22s; ready=matched; ready_probe_bytes=0; steps=1 [1:status==matched]; excerpt=[16143 ms] status=127577\\r\\nled_ms=1000 print_ms=3000`.
- Boundary test: 48 `x` characters plus CRLF wrote 50 bytes and returned no matching response; the core remained healthy with `PC=0x00000754`, `SP=0x200012B0`, and `xPSR=0x61000000`.
- Fresh-session counter test: initial connection was absent once (`Board 'nrf52840dk' is not connected`), then reconnect/validation succeeded. The core was halted, resumed, and the 8-byte status exchange produced the counter evidence documented above.
- Fresh-session passive capture initially returned no bytes because the core was halted/not connected. A later corrected observation checked state first, found `HALTED`, resumed, and captured:

  `UART matched on COM11 at 115200 baud via pyocd-native; expected=(none); reopen_count=0; duration=5.02s; excerpt=[73002 ms] LED_TOGGLE=74\\r\\n[74002 ms] LED_TOGGLE=75\\r\\n[75002 ms] LED_TOGGLE=76\\r\\n[75002 ms] PERIODIC_PRINT=26\\r\\n[76002 ms] LED_TOGGLE=77\\r\\n[77002 ms] LED_TOGGLE=78`

## Live SWD/debug evidence

During diagnosis, halted baseline reads included `PC=0x00000756`, `SP=0x200012B0`, `xPSR=0x61000000`, and `CFSR/HFSR` clear; the SCB read block was `00 00 00 00 00 00 00 00 01 00 00 00 F8 ED 00 E0`, with the nonzero DFSR debug indication rather than a fault.

The final repaired image was later connected, validated, halted, and read by symbol name from `collected-counterfix\firmware.elf`:

- `PC=0x00000748`, `SP=0x200012B0`.
- `scheduler_tick=0x000428D9`, `scheduler_runs=0x0008853B`.
- `queue_command_pushes=0x00000007`, `queue_command_pops=0x0000000E`, `queue_command_drops=0x00000000`.
- `queue_output_pushes=0x0000045B`, `queue_output_pops=0x000002D9`, `queue_output_drops=0x00000000`.
- `uart_rx_count=0x00000032`, `uart_tx_count=0x00007BED`.
- `console_commands=0x00000007`, `console_errors=0x00000006`.
- `command_head=0x0E`, `command_tail=0x0E`, `output_head=0x19`, `output_tail=0x19`.
- `scheduler_task_count` and the static `led_task`/`periodic_task` function names were not present as symbols in the optimized ELF; `console_task` was present and executable at `0x000002AD`.

Breakpoint sequence:

1. All-NULL `set_breakpoint-plan` teaching call completed.
2. Breakpoint plan: `plan-37affc8de289a5e0`; bound symbol `console_task` and current collected ELF.
3. Raw placement evidence: `Breakpoint set in executable space at 0x000002AD.`
4. Resumed, waited 50 ms, then observed `HALTED` with `PC=0x000002AC`, the Thumb breakpoint location for the `console_task` entry at `0x000002AD`.
5. Raw cleanup evidence: `Breakpoint removed at 0x000002AD.`
6. Resumed and confirmed final state `RUNNING`.

## Console exercise

Each implemented command was exercised one at a time through `serial_exchange`; the core remained running and background output continued.

- `help` matched: `help | status | led <ms> | print <ms>`.
- Baseline `status` matched: `status=426428\\r\\nled_ms=1000 print_ms=3000`.
- `led 250` matched: `led_interval=250`. Passive output showed 250 ms LED spacing: `182502`, `182752`, `183002`, `183252`, `183502`, `183752`, `184002`, `184252`.
- Follow-up `status` matched: `status=479673\\r\\nled_ms=250 print_ms=3000`.
- `print 700` matched: `print_interval=700`. Passive output showed `PERIODIC_PRINT=80` at `211702 ms` and `PERIODIC_PRINT=81` at `212402 ms`, exactly 700 ms apart; LED output continued at about 250 ms.
- Final `status` matched: `status=509449\\r\\nled_ms=250 print_ms=700`.
- Final SWD state after the console exercise was `SLEEPING`, i.e. running in WFI.

This document was written after the hardware run. No firmware file was edited, no build was run, and no hardware operation was performed during this documentation turn.
