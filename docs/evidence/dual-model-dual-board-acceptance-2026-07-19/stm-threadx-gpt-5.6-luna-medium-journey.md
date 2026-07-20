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
