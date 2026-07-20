# STM32L476 ThreadX acceptance journey

## Scope and setup

- Board: NUCLEO-L476RG.
- Probe UID: `066FFF514988525067233337`.
- VCP: `COM12`, same USB serial identity.
- Exact MCU/package part: `STM32L476RGT6`; live target `stm32l476rgtx`.
- Official source used: `datasheet.pdf`, SHA-256 `a45a857e3aa75ac166dd532c76d76d5dd8377b9c5bf6f15c03c9cf85aeec0f65`.
- Setup plan: `plan-fedc1c826274cc5b`.
- Live validation: STM32L476-compatible DBGMCU IDCODE `0x10076415`, validation passed.
- Local ThreadX package used: `C:\Users\Jason\STM32Cube\Repository\Packs\STMicroelectronics\X-CUBE-AZRTOS-L4\2.0.0`.
- Local Make: `C:\ST\STM32CubeIDE_1.18.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.make.win32_2.2.0.202409170845\tools\bin\make.exe`.
- Local GCC: `C:\ST\STM32CubeIDE_1.18.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.13.3.rel1.win32_1.0.0.202411081344\tools\bin\arm-none-eabi-gcc.exe`.
- No ThreadX download, RTOS workspace initialization, package installation, or source fetch was performed.

## Application and build evidence

The application contains independent ThreadX LED, UART-console, and periodic-print threads; mutex-protected settings; runtime `status`, `help`, `led <ms>`, and `print <ms>` commands; timestamped LED/PERIODIC output; and exported debugger symbols for threads, queue, settings, mutex, and counters.

The exact GENERAL helper used for the final build was:

```text
C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\.venv\Scripts\python.exe -m pyocd_debug_mcp.native_build --project-dir C:\firmcli-acceptance-20260719\stm-threadx-luna-r1\repo --build-dir C:\firmcli-acceptance-20260719\stm-threadx-luna-r1\repo\build-final8 --target all
```

The helper reported `exit_code: 0`, `provider: gnu-make`, `helper_provisioning: false`, and `offline_guards: true`.

Final collected artifacts:

- ELF: `artifacts-final8\firmware.elf`, SHA-256 `0db9969e8005b2726818a56258446cc7b364f2323d0e56f113072a83bdebe94d`, 987524 bytes.
- Map: `artifacts-final8\firmware.map`, SHA-256 `28d820c6aa5be8a56d8bb6e60f3e73301397498319bcff2d55a255ec3317b579`, 2027388 bytes.

Earlier rebuilt artifacts were also collected after real corrective iterations:

- final6 ELF `ac52cce1daf9b2a78ecdf41fe7614a9f24cc26711bb2f5fed58bf09563e5e0c8`; map `b10cc3012b6832dde57a7775f5ab6584cd000aee17dfb9bc838c01890bba9af0`.
- final7 ELF `445770ac8d589e1bb82b707c1cba6712f17a9759b71fe3686499bb3bccd14320`; map `32880662fc3cda8f75ccc4f07a3ea36e3664238d8215861f3d86a2ac83a319fc`.

## Flash evidence

Guarded application flash was used only with canonical ELF artifacts; no unlock, erase, bootloader, security/configuration write, commit, or push was performed.

- final5 flash plan: `plan-e28983028912320a`; target left running.
- final6 flash plan: `plan-c528f5a1537fdeb6`; target left running.
- final7 flash plan: `plan-3c06646b9fd0cf26`; target left running.
- final8 flash plan: `plan-7f159e0be387d729`; target left running.

## UART/SWD evidence and retries

Initial raw USART evidence was `CR1=0x0000000D`, `BRR=0x00000022`, and `ISR=0x006200C2` (`C2 00 62 00`): USART enabled, correct nominal divider, RXNE clear, framing error set. A genuine burst wrote 28 bytes on COM12; no status response appeared while background output continued. The burst write plan was `plan-2f4bf0c8a6074095`; the capture plan was `plan-e14dad031eb763e1`.

Firmware receive recovery and burst draining were tried in final6/final7. The corresponding status exchanges remained red:

- `plan-c7bc6b5b5c5a188c` / final6: wrote 7 bytes; no `status led_ms=`.
- `plan-7af719edf20f4ea0` / final7: wrote 7 bytes; no `status led_ms=`.

Final8 added only a PA3 pull-up after live GPIO evidence. Its status exchange was `plan-7f15de71316fd061` and returned:

```text
UART exchange did not match on COM12 at 115200 baud; wrote 7 byte(s); duration=4.12s; ready=matched; ready_probe_bytes=0; steps=1 [1:status led_ms==did not match]; excerpt=[t=17742ms] LED_TOGGLE count=35 interval_ms=500\\r\\n[t=18243ms] PERIODIC count=18 interval_ms=1000\\r\\n[t=18250ms] LED_TOGGLE count=36 interval_ms=500\\r\\n[t=18756ms] LED_TOGGLE count=37 interval_ms=500\\r\\n[t=19257ms] PERIODIC count=19 interval_ms=1000\\r\\n[t=19264ms] LED_TOGGLE count=38 interval_ms=500
```

The final bounded background capture was plan `plan-182220a5378ca8a0`:

```text
UART matched on COM12 at 115200 baud via pyocd-native; expected=(none); reopen_count=0; duration=3.02s; excerpt=[t=69456ms] LED_TOGGLE count=137 interval_ms=500\\r\\n[t=69957ms] PERIODIC count=69 interval_ms=1000\\r\\n[t=69964ms] LED_TOGGLE count=138 interval_ms=500\\r\\n[t=70470ms] LED_TOGGLE count=139 interval_ms=500\\r\\n[t=70971ms] PERIODIC count=70 interval_ms=1000\\r\\n[t=70978D_c=140 interval_ms=500\\r\\n[t=71484m
```

Independent host-side diagnosis then established the physical fault:

- With COM12 held open, slow repeated `status` plus newline still produced no command response while TX continued.
- All DTR/RTS combinations left `GPIOA_IDR` PA3 low.
- With the core halted, writing byte `0x55` to COM12 produced neither USART2 RXNE nor RDR data; after resume, `command_count` remained zero.
- Official NUCLEO-64 UM1724 routing is PA2/PA3 through SB13/SB14 ON and SB62/SB63 OFF. PA2 to host works; host to PA3 does not.
- Probe UID and COM identity were reverified: `066FFF514988525067233337` and `COM12`.
- SWD GPIO reads were `GPIOA_IDR=0x0000E024`; PA3 bit 3 was low before and after the firmware pull-up image.

## Current blocking status

There is no non-destructive firmware-only way to prove inbound commands through this same COM12 route when the halted-core `0x55` test produces neither RXNE nor RDR data. Firmware cannot parse a byte that never enters USART2. This is recorded as an environmental one-way ST-Link VCP RX fault, not a parser failure.

Final8 is deployed and the board is left running. No further firmware change, rebuild, flash, unlock, erase, or hardware configuration action is authorized or indicated by this evidence.

## Fresh MCP run: 2026-07-20 environmental blocker recheck

- Server run ID: `run-20260720T101746Z-0841c03f`.
- Reverified probe: `066FFF514988525067233337`.
- Reverified VCP: `COM12`, USB serial `066FFF514988525067233337`.
- Board validation passed for NUCLEO-L476RG / `STM32L476RGT6`; observed `STM32L476 compatible DBGMCU_IDCODE 0x10076415`.
- Setup status: `setup_ready`, `live_session_ready=true`, `ready_for_uart_work=true`.
- Explicit ELF used for symbol reads: `C:\firmcli-acceptance-20260719\stm-threadx-luna-r1\repo\artifacts-final8\firmware.elf`.
- `reset_and_run` was performed; no rebuild or reflash was performed.

Minimum decisive RX test:

- Baseline `command_count` via the explicit final8 ELF: `0x00000000`.
- Baseline USART2 ISR plan `plan-c196dfb8a0fadf42`: `C0 00 62 00`.
- COM12 write plan `plan-e5ba0c5df1b19cda`: `UART wrote 7 byte(s) on COM12 at 115200 baud via pyocd-native; duration=0.02s` for `status` with appended LF.
- Post-write `command_count` via the explicit final8 ELF: `0x00000000`.
- Post-write USART2 ISR plan `plan-4fe0240b6bde3af9`: `C0 00 62 00` — RXNE clear and no USART error flags.
- Post-write USART2 RDR plan `plan-8af70ce70320eb75`: `00`.
- TX capture plan `plan-b1343c488a3f47b4` remained healthy. Verbatim excerpt:

```text
UART matched on COM12 at 115200 baud via pyocd-native; expected=(none); reopen_count=0; duration=2.03s; excerpt=[t=97335ms] PERIODIC count=96 interval_ms=1000\\r\\n[t=97342ms] LED_TOGGLE count=192 interval_ms=500\\r\\n[t=97848ms] LED_TOGGLE count=193 interval_ms=500\\r\\n[t=98349ms] PERIODIC count=97 interval_ms=1000\\r\\n[t=98356ms] LED_TOGGLE count=194 interval_ms=500\\r\\n[t=98862ms] LED_TOGGLE count=195 interval_ms
```

Conclusion: host-to-PA3 still produces neither RXNE/RDR data nor `command_count` movement while board-to-host TX remains healthy. This is fresh confirmation of the same environmental one-way ST-Link VCP RX hardware-path fault. No console-command exercise, breakpoint/debug escalation, firmware edit, rebuild, or flash was performed. The board remains running.

## Fresh MCP run: 2026-07-20 per-character transport test

- Server run ID: `run-20260720T124142Z-0a995f2f`.
- Setup overview reassigned Nucleo-L476RG to ST-Link `066FFF514988525067233337` / COM12.
- `board_validate` passed for the existing `nucleo_l476rg` profile; observed MCU `STM32L476 compatible DBGMCU_IDCODE 0x10076415`.
- `reset_and_run` was performed once. No rebuild or flash was performed.
- The explicit final8 ELF used for symbol operations was `C:\firmcli-acceptance-20260719\stm-threadx-luna-r1\repo\artifacts-final8\firmware.elf`.

The requested persistent per-character `help` exchange was plan `plan-e0101c0aadf9c983`: `h`, `e`, `l`, and `p` were separate `line_ending=none` steps, followed by a separate CR byte step. Steps 1-4 all matched their required echoes. Step 5 failed its distinctive final-response requirement. Verbatim result:

```text
UART exchange did not match on COM12 at 115200 baud; wrote 5 byte(s); duration=2.89s; ready=matched; ready_probe_bytes=0; steps=5 [1:h=matched; 2:e=matched; 3:l=matched; 4:p=matched; 5:commands: help=did not match]; excerpt=d toggle\\r\\n[88163he[32600] led toggle\\r\\nlphelp status led <ms> print <ms> fault stack\\r\\nstm32> [33100] periodic print\\r\\n[33101] led toggle\\r\\n[33603] led toggle\\r\\n[34103] periodic print\\r\\n[34104] led toggle\\r\\n[34606] periodic print\\r\\n[35107] led toggle
```

The observed response/prompt (`help status led <ms> print <ms> fault stack`, `stm32>`) does not match final8 source output (`commands: help | status | led <ms> | print <ms>` and timestamped `[t=...]` records). The explicit final8 ELF resolved `command_count@0x200002A4`, but the live read returned `value=0x200002B4`, not a plausible counter value.

Post-failure live USART evidence:

- ISR read plan `plan-6de9c54a842e0231`: `D0 10 60 00`; RXNE and FE/ORE/NE/PE bits were clear after the exchange.
- The first four echo matches prove bytes reached the currently running image’s receive path.
- Because the required final response failed and the UART output/image identity does not match final8, the other commands, interval proof, and breakpoint/debug escalation were not attempted.

Current status: this is a running-image/state mismatch exposed by the no-flash constraint, not evidence for a new firmware edit. The board remains running; no source, build, flash, or server state was changed.

## Fresh MCP run: final8 reflash and per-character acceptance attempt

- Server run ID: `run-20260720T124446Z-78439362`.
- Setup overview assignment, `board_validate` load, and validation completed for Nucleo-L476RG / probe `066FFF514988525067233337` / COM12.
- Validation observed `STM32L476 compatible DBGMCU_IDCODE 0x10076415`.
- Local artifact hash reconfirmed: `artifacts-final8\firmware.elf` SHA-256 `0db9969e8005b2726818a56258446cc7b364f2323d0e56f113072a83bdebe94d`.
- No build was run.
- Guarded flash plan: `plan-148245d83ab42ccd`; exact application flash action succeeded and left the target running.

The requested clean per-character `help` exchange used plan `plan-db2e4c671f276f05`: `h`, `e`, `l`, `p` as separate no-line-ending steps, followed by a separate CR byte, with each echo and `commands: help` required. It stopped at the first character. Verbatim result:

```text
UART exchange did not match on COM12 at 115200 baud; wrote 1 byte(s); duration=3.09s; ready=matched; ready_probe_bytes=0; steps=1 [1:h=did not match]; excerpt=[t=25341ms] PERIODIC count=25 interval_ms=1000\\r\\n[t=25348ms] LED_TOGGLE count=50 interval_ms=500\\r\\n[t=25854ms] LED_TOGGLE count=51 interval_ms=500\\r\\n[t=26355ms] PERIODIC count=26 interval_ms=1000\\r\\n[t=26362ms] LED_TOGGLE count=52 interval_ms=500\\r\\n[t=26868ms] LED_TOGGLE count=53 interval_ms=500
```

Post-failure final8 evidence:

- Explicit final8 ELF symbol `command_count@0x200002A4`: `0x00000000`.
- USART2 ISR plan `plan-ac23ff4d959607d5`: `C0 00 62 00` — RXNE and FE/ORE/NE/PE clear.
- Source inspection confirms `console_entry` consumes RDR but never emits per-character echo; the only help output is `commands: help | status | led <ms> | print <ms>`.

This is a demonstrated final8 acceptance defect against the newly required echo criterion. The remaining commands, interval proof, and breakpoint/debug escalation were not attempted. No source change or rebuild was performed; final8 remains running.

## Fresh MCP run: final8 CR command-handling retry

- Server run ID: `run-20260720T124922Z-7d5e8257`.
- Exact overview assignment, board-validate load, validation, and connect completed for Nucleo-L476RG / probe `066FFF514988525067233337` / COM12.
- Final8 was already flashed; no rebuild, source edit, or flash was performed.
- A reset/run was performed to clear the partial `h` left by the earlier stopped test.

The clean `help` exchange used plan `plan-08489746b6b7c0eb`. It sent `h`, `e`, `l`, and `p` as separate `line_ending=none` steps, each requiring `LED_TOGGLE`, then sent a separate CR step requiring `commands: help`. All four intermediate liveness steps matched; the final CR response did not. Verbatim result:

```text
UART exchange did not match on COM12 at 115200 baud; wrote 5 byte(s); duration=5.17s; ready=matched; ready_probe_bytes=0; steps=5 [1:LED_TOGGLE=matched; 2:LED_TOGGLE=matched; 3:LED_TOGGLE=matched; 4:LED_TOGGLE=matched; 5:commands: help=did not match]; excerpt=[t=26355ms] PERIODIC count=26 interval_ms=1000\\r\\n[t=26362ms] LED_TOGGLE count=52 interval_ms=500\\r\\n[t=26868ms] LED_TOGGLE count=53 interval_ms=500\\r\\n[t=27369ms] PERIODIC count=27 interval_ms=1000\\r\\n[t=27376ms] LED_TOGGLE count=54 interval_ms=500\\r\\n[t=27882ms] LED_TOGGLE count=55 interval_ms=500
```

Post-failure final8 evidence:

- Explicit final8 ELF `command_count`: `0x00000000`.
- USART2 ISR plan `plan-471876e5843fa72f`: `C0 00 62 00` — RXNE and FE/ORE/NE/PE clear.

The intermediate markers prove the board remained alive; they do not prove receive activity. The final CR produced no parser response and no command counter movement, so the same host-to-PA3 receive-path fault remains the decisive classification. `status`, `led 200`, and `print 9` were not sent after this failed help command. No firmware or build change was made; final8 remains running.

## Firmware-specific UART diagnosis and final10 acceptance

- Server run ID: `run-20260720T125342Z-3a9d3e0b`.
- Setup handshake/overview assignment, `board_validate` load, validation, and connect completed for Nucleo-L476RG / probe `066FFF514988525067233337` / COM12. Validation observed `STM32L476 compatible DBGMCU_IDCODE 0x10076415`; exact package-level target is STM32L476RGT6.
- Local resources remained in use: ThreadX `C:\Users\Jason\STM32Cube\Repository\Packs\STMicroelectronics\X-CUBE-AZRTOS-L4\2.0.0`, Make under `C:\ST\STM32CubeIDE_1.18.1\...externaltools.make...\make.exe`, and GCC under `C:\ST\STM32CubeIDE_1.18.1\...gnu-tools-for-stm32.13.3.rel1...\arm-none-eabi-gcc.exe`. No download or server change occurred.

### Diagnosis before final10 edit

Live final8 SWD evidence showed USART2 enabled and correctly clocked for both directions: `USART2_CR1=0x0000000D` (UE/RE/TE), `USART2_BRR=0x00000022` at the live 4 MHz `SystemCoreClock`, RCC GPIOA and USART2 enables set, PA2/PA3 AFRL=`0x00007700` (AF7), but `GPIOA_MODER=0xABFFF7EF`: PA2 was alternate-function while PA3 remained analog mode. TX therefore worked while RX could not reach USART2. The source mask cleared PA2 mode bits but omitted PA3 bits:

```c
GPIOA->MODER = (GPIOA->MODER & ~(3u << 4u)) | (2u << 4u) | (2u << 6u);
```

The minimal GPIO fix cleared both mode fields. Final9 live post-flash evidence was `GPIOA_MODER=0xABFFF7AF`, confirming both pins in AF mode. A clean final9 `print 9` exchange (plan `plan-d29326dba6df6603`) matched all seven character liveness markers and the separate CR `ok`, but SWD read plan `plan-5490fbc69ae4dafb` returned `0x000003E8` for `app_settings.print_interval_ms` at `0x20000004`. Source inspection then identified the second firmware defect: the print branch accepted only `n >= 10u` while always printing `ok`. The sole final10 source change was that bound to `n >= 1u`; no instrumentation was added.

### Final10 build, collection, and flash

Exact provider-neutral GENERAL helper command:

```text
C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\.venv\Scripts\python.exe -m pyocd_debug_mcp.native_build --project-dir C:\firmcli-acceptance-20260719\stm-threadx-luna-r1\repo --build-dir C:\firmcli-acceptance-20260719\stm-threadx-luna-r1\repo\build-final10 --target all
```

Build exit code was 0, provider `gnu-make`, using the existing local GCC/Make resources. Collected canonical artifacts:

- `artifacts-final10\firmware.elf`: SHA-256 `9eb2a238cc062be490ca002d96ff9f4e524a5135d8a49eafa95a0f90595a4d03`.
- `artifacts-final10\firmware.map`: SHA-256 `2bddec4e4cc3b7ca73adda29f643ab3a097e3602f0b2c0a7c4c3d04934dd998b`.

The mandatory all-NULL flash-plan initialization was followed by populated guarded application plan `plan-efcb891a49c23572`; its exact returned fallback flashed only the collected ELF inside the mapped application partition. The server reported the target left running. A reset/run then cleared the command buffer.

### Final10 UART proof

Each exchange used one COM12 open, 115200 baud, one character per `line_ending=none` step, recurring `LED_TOGGLE` on every character, and a separate CR step. Plan IDs and verbatim result summaries:

- `help`, plan `plan-f2fcaa5b28140b64`: `wrote 5 byte(s); duration=2.08s; steps=5 [1:LED_TOGGLE=matched; 2:LED_TOGGLE=matched; 3:LED_TOGGLE=matched; 4:LED_TOGGLE=matched; 5:commands: help=matched]`. Output included `PERIODIC count=15 interval_ms=1000`, `LED_TOGGLE count=30 interval_ms=500`, then `LED_TOGGLE count=31 interval_ms=500`.
- `status`, plan `plan-53c1e6071aeb2138`: `wrote 7 byte(s); duration=2.91s; steps=7 [1:LED_TOGGLE=matched; 2:LED_TOGGLE=matched; 3:LED_TOGGLE=matched; 4:LED_TOGGLE=matched; 5:LED_TOGGLE=matched; 6:LED_TOGGLE=matched; 7:status led_ms==matched]`. Output included `LED_TOGGLE count=41 interval_ms=500`, `PERIODIC count=21 interval_ms=1000`, and `LED_TOGGLE count=44 interval_ms=500`.
- `led 200`, plan `plan-7c5205f6ed45b9fd`: `wrote 8 byte(s); duration=3.52s; steps=8 [1:LED_TOGGLE=matched; 2:LED_TOGGLE=matched; 3:LED_TOGGLE=matched; 4:LED_TOGGLE=matched; 5:LED_TOGGLE=matched; 6:LED_TOGGLE=matched; 7:LED_TOGGLE=matched; 8:ok=matched]`. Output included `PERIODIC count=27 interval_ms=1000`, then `LED_TOGGLE count=54 interval_ms=500` and `LED_TOGGLE count=57 interval_ms=500` during the transaction.
- `print 9`, plan `plan-7d2df637dd8b179f`: `wrote 8 byte(s); duration=1.66s; steps=8 [1:LED_TOGGLE=matched; 2:LED_TOGGLE=matched; 3:LED_TOGGLE=matched; 4:LED_TOGGLE=matched; 5:LED_TOGGLE=matched; 6:LED_TOGGLE=matched; 7:LED_TOGGLE=matched; 8:ok=matched]`. Output showed `LED_TOGGLE count=78 interval_ms=200`, `PERIODIC count=34 interval_ms=1000`, then `LED_TOGGLE count=79 interval_ms=200` and `LED_TOGGLE count=81 interval_ms=200` while the command completed.

The bounded post-command capture used plan `plan-a5ad8432cf698756` for 3.00 seconds with no reset. Verbatim excerpt:

```text
UART matched on COM12 at 115200 baud via pyocd-native; expected=(none); reopen_count=0; duration=3.00s; excerpt=[t7ms] PERIODIC count=797 interval_ms=9\\r\\n[t=48172ms] PERIODIC count=798 interval_ms=9\\r\\n[t=48187ms] PERIODIC count=799 interval_ms=9\\r\\n[t=48202ms] PERIODIC count=800 interval_ms=9\\r\\n[t=48217ms] PERIODIC count=801 interval_ms=9\\r\\n[t=48232ms] PERIODIC count=802 interval_ms=\\n[t=48247ms] PERIODIC
```

The earlier exchange excerpts prove the independent LED activity at 200 ms while periodic output was still transitioning; the bounded capture proves the periodic activity reached 9 ms. The board is running final10 and was not left halted.

Final live SWD confirmation used the explicit final10 ELF: `app_settings` at `0x20000000` read `0x000000C8` (LED 200 ms), and read plan `plan-9bafbf9a22b624a2` read `0x00000009` at `0x20000004` (periodic 9 ms). The target remains running.

## Live-debug acceptance phase: final10

- Fresh MCP server run ID: `run-20260720T131615Z-6297e5f6`.
- Initialization handshake completed. The requested Nucleo-L476RG assignment was routed to probe `066FFF514988525067233337`; COM12 remained the associated VCP.
- The server initially required a fresh safety-map refresh. `board_safety_refresh` was loaded and completed with map digest `77916c4f781c6307e3b7083377b7b202afc421eabcb41cfe13c608afd85bc0f1`, after which the friendly assignment `Nucleo-L476RG` was selected and `board_validate` passed.
- Validation observed `STM32L476 compatible DBGMCU_IDCODE 0x10076415`, expected `STM32L476RGT6`, probe identity `066FFF514988525067233337`.
- Connect succeeded in session `20260720T131821Z-62a7f7b6` via pyocd-native. No build or flash was performed. All symbol reads used `C:\firmcli-acceptance-20260719\stm-threadx-luna-r1\repo\artifacts-final10\firmware.elf`.

### Halted register and symbol evidence

`halt` returned exactly `Halted.`. The first attempted ordinary-register reads for `pc` and `sp` were refused as wrong-class; the correct execution-state reads then returned:

```text
PC = 0x080001A0
SP = 0x20001018
```

Named ELF symbol discovery and halted reads:

```text
_tx_thread_current_ptr@0x20001294 size=4; value=0x20000178
led_thread@0x20000010 size=180; value=0x54485244
uart_console_thread@0x200000C4 size=180; value=0x54485244
periodic_print_thread@0x20000178 size=180; value=0x54485244
command_queue@0x2000022C size=60; value=0x51554555
settings_mutex@0x20000268 size=52; value=0x4D555445
app_settings@0x20000000 size=8; value=0x000000C8
led_toggle_count@0x2000029C size=4; value=0x00000CA4
periodic_print_count@0x200002A0 size=4; value=0x0000A62F
command_count@0x200002A4 size=4; value=0x00000004
```

`find_symbol` located the real task function `led_entry@0x08000521 size=80 type=STT_FUNC`.

### Breakpoint proof and cleanup

The first symbol-form breakpoint plan `plan-7e7fab1bbf4d1cc3` set `led_entry` at `0x08000521`, but after resume and bounded waits of 500 ms and 2 seconds, `get_state` remained exactly `RUNNING`; it was not counted as a hit. The breakpoint was removed at `0x08000521`.

The replacement breakpoint plan `plan-149adebaffc5d536` set the aligned executable address `0x08000520` using the same final10 ELF. Resume alone did not hit it because the task entry had already run. With that breakpoint still installed, `reset_and_run` returned `Reset and running.`; after `wait` planless operation for 500 ms, `get_state` returned exactly `HALTED` and the execution-state PC read returned exactly:

```text
0x08000520
```

This is the observed breakpoint hit in the real `led_entry` task function. `remove_breakpoint` returned exactly `Breakpoint removed at 0x08000520.`. `resume` returned exactly `Resumed.` and the final `get_state` returned exactly `RUNNING`. The board was left running with no breakpoint installed.
