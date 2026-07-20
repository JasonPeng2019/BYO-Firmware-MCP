# nRF52840-DK bare-metal acceptance journey

## Run identity

- Model: `gpt-5.6-luna`
- Effort: `medium`
- Repository: `C:\firmcli-acceptance-20260719\nrf-baremetal-luna-r2\repo`
- Final MCP debug run ID: `20260720T083831Z-76b60bd5`
- Hardware: nRF52840-DK, nRF52840-QIAA
- Probe: onboard J-Link UID `683377322`
- UART: J-Link VCP `COM11`, stable USB identity `000683377322`, `115200` baud

## Scope and safety

The application was authored as freestanding C/assembly with no RTOS and no vendor HAL. Hardware access was performed through the `pyocd-debug` MCP server. Flashing used only bounded application-region guarded plans. No mass erase, unlock, protection change, bootloader write, or non-MCP hardware access was used.

## Setup

Observed setup facts:

- `setup_overview` routed `nRF52840-DK` to probe `683377322` and `COM11`.
- `board_safety_refresh` completed after the reviewed map repair. Map digest: `9f4453fbffea5f5e7a6557a070d55328d11338b05ce3dac25acfb30382385187`.
- `board_validate` observed exact part `FICR INFO.PART 0x00052840` and probe `683377322`.
- Later reconnects required repeating `setup_overview`, `load_setup_tool(board_validate)`, and `board_validate`; validation passed each time.

The build guidance returned the general helper:

```text
C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\.venv\Scripts\python.exe -m pyocd_debug_mcp.native_build --project-dir <project-dir> --build-dir <new-empty-build-dir> --target <project-native-target>
```

The selected native environment was GNU Make with `arm-none-eabi-gcc` from STM32CubeIDE 1.18.1. No dependency download or provisioning occurred.

## Implementation and build history

The repository was implemented with custom startup/vector code, linker script, SysTick scheduler, task functions, command/output queues, UARTE EasyDMA UART transport, LED task, console task, and independent periodic-print task. Exported symbols included scheduler, queue, UART, and console counters.

Chronological builds and collected artifacts:

1. `build-rx-event-clear` / `artifacts-rx-event-clear`: ELF `7047ff77c2ea61fc3bfdb72d919cd8f8bdd03691edf708ad10a25dbc4c25391a`; MAP `3cc4298fc457d9a93bbd3ea0b82d633df137251fba821009d5b88dfdb51f2037`; HEX `57e1914caf15900124ddbe950f93a273764759ceefbceb23042a5ddac67b3e97`; BIN `384ba5f4c2e9f4c438a5095544e93b2c324eaecb34591c5c18675e0b5e730820`.
2. `build-rx-buffer8` / `artifacts-rx-buffer8`: ELF `befb4d163c0eafc5d8a20a3c4c98a57887ec3b257523da69ecbd0ad379fbc2d9`; MAP `8f4966f2aadf648c2ed0b81f2398e6ebfa7facedf91d7fa58d12201acfe49874`; HEX `bb49db653f56510e51fb63be943ca78a2de0e1562af76b61e4538cba4df6c229`; BIN `6d4003a88a1a7a99c5ff9ef1da0bb651b3f676b351d01c57e02479e86f5cc5d0`.
3. `build-rx-irq` / `artifacts-rx-irq`: ELF `b0c736810fa5ad0ab466e9b634b4e5789c112f3b5d806340783f6c0dd243d4c6`; MAP `f0b28d73f63fe23e950c7375949b442ac6622db8b12abb8da85e6763da47a27`; HEX `0a10de5ae06e125357c31fc50fe80fe024734ef3dc67f05b8eca236560bdfaca`; BIN `a12fdcc32471d7dc53eed5d3dccd09db76e9e007409838a3977e32cd18b05bb0`.
4. `build-rx-irq-guard` / `artifacts-rx-irq-guard`: ELF `a1a82ffa8d81d86a8116c70cdcf0f36f2f93965465e128cdd34d67baef7f58e2`; MAP `851e43e84644f1432a112f1e4b7ed6ceac5ff3911522f243d61f4aebb2f85810`; HEX `1cd61d0138e31316f7a40776f5e665a69d769fa931f4ea9cef5ab57a223dc29c`; BIN `a8ea9d4f9d9da516ae057d4a2e8e7c36d0ea34a477754b5fd8414e2323687a76`.
5. `build-uarte-bitfields` / `artifacts-uarte-bitfields` was the final image. ELF `2448bd04db174b4c0ca0db73ffaca3b7c6459903823fb9c06c531a6bad1fc1c6`; MAP `3fbab741141b493eb4e25ca9223a9288e3f2900c7ca3b5a04acf2cf0767a848e`; HEX `e6916736538766612cdfcba976f2ed6a208b3e0cebdd164545883f9d4143ba45`; BIN `4370621871ee76e3035a73bb6249d2305adc5ba9f64fe063ca1023e8ca075e89`.

Intermediate `build-rx-irq-guard2`, `build-rx-input-connected`, and `build-rx-pullup` were also built and collected during diagnosis. They were not the final flashed image.

## Flash history

Guarded application flash plans that executed successfully:

- `plan-9228fea93e524eca` — `artifacts-rx-event-clear`
- `plan-56c8820b8d976b33` — `artifacts-rx-buffer8`
- `plan-43dbc479c2618332` — `artifacts-rx-irq`
- `plan-85c17048b7d886c3` — `artifacts-rx-irq-guard`
- `plan-10539b4889fa30ac` — `artifacts-rx-irq-guard2`
- `plan-4ecac8ed3170edbc` — `artifacts-rx-input-connected`
- `plan-22cdd36af2fcf975` — `artifacts-rx-pullup`
- `plan-951374ddf447887b` — final `artifacts-uarte-bitfields`

One `plan-ad07bb2170c23b3a` flash attempt was refused before mutation because the MCP connection had expired; it was replaced after reconnect and validation. The final target was left running after `plan-951374ddf447887b`.

## UARTE diagnosis and fix

Observed live evidence during the red loop included `SHORTS=0x1`, `RXD.AMOUNT=1`, `RXDRDY=1`, `ENDRX=0`, and only one completed RX byte. The datasheet documented `EVENTS_RXDRDY` as data received potentially before RAM transfer, `EVENTS_ENDRX` as the filled receive buffer event, and `RXD.AMOUNT` as the transferred count.

The decisive bitfield evidence was:

- Datasheet SHORTS Bit number row: field `C` is bit 5, not bit 0.
- Local Nordic header `C:\ncs\v3.3.1\modules\hal\nordic\nrfx\bsp\stable\mdk\nrf52840_bitfields.h`: `UARTE_SHORTS_ENDRX_STARTRX_Pos (5UL)`.
- Header: `UARTE_INTENSET_ENDRX_Pos (4UL)`.
- Header: `UARTE_INTENSET_RXDRDY_Pos (2UL)`.

The defective raw writes were `UART_SHORTS=1u` and `UART_INTENSET=1u<<3`; neither selected the intended fields. The final targeted fix changed SHORTS to `1u<<5`, ENDRX interrupt enable to `1u<<4`, restored RX input without the unsubstantiated pull-up, and cleared UART events before enabling ENDRX and starting RX. No RTOS/HAL architecture change was made.

The temporary connected-input and pull-up hypotheses were tested in `artifacts-rx-input-connected` and `artifacts-rx-pullup`, both remained red, and were reverted. They were not flashed as the final image.

## UART proof history

Early UART plans failed or exposed partial receive behavior, including `plan-2dbb8993318a2b97`, `plan-d963cce65825c06b`, `plan-43dbc479c2618332`, and `plan-51f88fa5b80cadbb`. The red transcripts showed monotonic LED output but missing command replies.

The first green complete ordered proof used `plan-cb6b8e15e17f8635` after final bitfield correction:

```text
UART exchange matched on COM11 at 115200 baud; wrote 42 byte(s)
[11002 ms] LED_TOGGLE=12
[12002 ms] LED_TOGGLE=13
[12002 ms] PERIODIC_PRINT=5
help | status | led <ms> | print <ms>
[12237 ms] status=677743
led_ms=1000 print_ms=3000
[12456 ms] led_interval=250
[12675 ms] print_interval=700
[12894 ms] status=678400
led_ms=250 print_ms=700
```

Prompt 3 individual command proof initially hit an expired connection. After reconnect, validation, and `reset_and_run`, all commands passed one at a time:

- `plan-195942b6206b989d` — `help`
- `plan-14ae767c2d212d7f` — baseline `status`
- `plan-ada6b6f73335789e` — `led 400`
- `plan-347558b08f94cd08` — status after LED change
- `plan-973322c8bfadd995` — `print 900`
- `plan-7259a27590b24ab4` — status after print change
- `plan-f0be2de5ba09d3a8` — additional post-change status capture

Observed interval evidence: LED events changed from 1000 ms (`16002, 17002, 18002`) to 400 ms (`22002, 22402, 22802, 23202`). Periodic events were 3000 ms before the print change (`24002` to `27002`) and 900 ms afterward (`30002` to `52502`, 25 intervals over 22500 ms).

## Live debug history

The first post-restart debug session exposed a server symbol-artifact defect: by-name reads silently selected the packaged BYO reference ELF. `find_symbol` and `read_memory_symbol` therefore returned symbol-not-found even though the flashed project ELF contained the symbols. The breakpoint workaround used address `0x000002AC` and plan `plan-24d283b845bf2fb8`; it hit, was removed, and the board resumed.

After the server fix, the explicit current project ELF was supplied to both `find_symbol` and `read_memory_symbol`. Observed symbol discovery:

```text
scheduler_runs@0x200002CC size=4
scheduler_tick@0x200002D0 size=4
queue_command_drops@0x200002C0
queue_command_pops@0x200002C4
queue_command_pushes@0x200002C8
queue_output_drops@0x200002B4
queue_output_pops@0x200002B8
queue_output_pushes@0x200002BC
uart_rx_count@0x200002B0
uart_tx_count@0x200002AC
uart_tx_started@0x200002A8
console_task@0x000002AD size=280 type=STT_FUNC
```

Observed live read values:

```text
PC 0x0000074A
SP 0x200012B0
scheduler_runs = 0x00588CCF
scheduler_tick = 0x0019618D
queue_command_drops = 0x00000000
queue_command_pops = 0x00000019
queue_command_pushes = 0x00000008
queue_output_drops = 0x0005031E
queue_output_pops = 0x0000173F
queue_output_pushes = 0x00006207
uart_rx_count = 0x0000003A
uart_tx_count = 0x200BC0EE
uart_tx_started = 0x00000001
```

The explicit-ELF breakpoint retest used `plan-06f56dd52420b77d`. MCP reported:

```text
Breakpoint set in executable space at 0x000002AD.
HALTED
PC: 0x000002AC
SP: 0x200012B0
Breakpoint removed at 0x000002AD.
Resumed.
SLEEPING
```

The one-byte PC difference is the normal Thumb symbol-address representation. A first replacement breakpoint plan, `plan-5aab104dd198c649`, was refused by the fresh-connection validation gate before hardware action; the same plan was replaced after non-destructive validation.

## Evidence versus inference

Observed evidence is identified by MCP responses, exact register/symbol values, build hashes, UART excerpts, and plan IDs above. Inferences are limited to the UARTE bitfield cause, the Thumb-address normalization, and interpretation of interval changes from timestamp differences. No inference is treated as a substitute for an MCP observation.

## Final state

The final ELF remains the flashed `artifacts-uarte-bitfields` image. The breakpoint was removed, the core was resumed, and the final MCP state was:

```text
SLEEPING
```

This is the expected running `WFI` state. No hardware mutation was performed while writing this document.
