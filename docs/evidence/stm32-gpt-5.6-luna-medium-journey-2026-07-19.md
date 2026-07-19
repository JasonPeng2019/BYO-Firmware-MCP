# STM32 GPT Bootloader Acceptance R5

- Model: `gpt-5.6-luna`
- Effort: `medium`
- Server run: `run-20260719T062506Z-9864e098`
- Setup target: `STM32L476RGT6` / `stm32l476rgtx`; ST-Link board; COM12 at 115200.
- Setup: passed. Validation: passed initially during setup and again after reconnect (`validation/passed`).
- Final hardware state: firmware running; MCP disconnect completed. Nordic inventory was named only for routing and never loaded, connected, flashed, or executed.
- CLI version: not exposed by the native-build helper.

## Firmware

Freestanding Cortex-M4F code, ordinary Makefile, no generated IDE project:

- Bootloader: `0x08000000`, 32 KiB linker region; validates application MSP/reset vector, sets VTOR/MSP, jumps to `0x08008000`; invalid image loops with UART `FAIL` and LED PA5 activity.
- Application: `0x08008000`, remaining flash; USART2 PA2/PA3, PA5 LED, explicit HSI16 clock, `GOOD <counter>` loop; exports `acceptance_counter` and non-inlined `acceptance_tick`.
- Linker assertions enforce bootloader size, application origin, flash bounds, and reviewed 96 KiB SRAM envelope.

## Build evidence

Exact helper form returned by `get_setup_status` (local GNU Make/Arm GCC, offline):

`C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\.venv\Scripts\python.exe -m pyocd_debug_mcp.native_build --project-dir C:\stm32-gpt56-boot-r5 --build-dir <new-build-dir> --target <target>`

Final successful invocations:

```text
C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\.venv\Scripts\python.exe -m pyocd_debug_mcp.native_build --project-dir C:\stm32-gpt56-boot-r5 --build-dir C:\stm32-gpt56-boot-r5\build-bootloader-r5d --target bootloader
C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\.venv\Scripts\python.exe -m pyocd_debug_mcp.native_build --project-dir C:\stm32-gpt56-boot-r5 --build-dir C:\stm32-gpt56-boot-r5\build-application-r5e --target application
```

Both returned exit code 0, exactly one ELF, one map, and an optional same-stem HEX. Earlier failure/retests: the first build failed because the Makefile used Windows shell syntax and ignored helper `BUILD_DIR`; fixed to POSIX `mkdir -p` and `BUILD ?= $(BUILD_DIR)`. Flash rejected two intermediate bootloader artifacts: first non-Thumb ELF entry; then MSP at the generic 128 KiB boundary. Fixed with explicit Thumb ELF entry and reviewed 96 KiB SRAM link region. UART retest after explicit HSI16 still produced no bytes.

Final collected artifacts:

| Image | Canonical ELF | SHA-256 | Map SHA-256 |
|---|---|---|---|
| Bootloader | `C:\stm32-gpt56-boot-r5\collected-bootloader-r5d\firmware.elf` | `66e0120eae14370d373779a0e26d39745ced834da47ee904dbb1f8af1eb075b4` | `287cf5e96db9bf08d4fe6caaaf16817df0ea0e4bc886249ef0de6e44799ba811` |
| Application | `C:\stm32-gpt56-boot-r5\collected-application-r5e\firmware.elf` | `fc8630d313465f686469bdc70b6b69f6c369bc07d981a92e60a279b0d95c332e` | `3cb889fe1ecf4ec7ae4914da0636c8aae6e38ee98ea91808794fbe99b63a1ddc` |

## Flash plans/results

- Bootloader was intentionally submitted through the accepted full-flash `flash_application` envelope: final plan `plan-28c342c31fdabf6f`; result: flashed and target left running.
- Application final plan: `plan-a3275e788373258f`; result: flashed within mapped partition and target left running.
- Earlier accepted-but-safe refusals: `plan-3fd31441757cc85d` (non-Thumb entry), `plan-933fb6eb6d76dda5` (MSP outside reviewed RAM), `plan-eb6bc3e54eec8272` (same RAM diagnosis). No erase/unlock/option-byte/raw-write route was used.

## UART transcript

Expected: at least five ordered `GOOD <counter>` lines on COM12. Actual MCP captures:

```text
UART did not match on COM12 at 115200 baud via pyocd-native; excerpt=(none)
UART did not match on COM12 at 115200 baud via pyocd-native; excerpt=(none)
UART did not match on COM12 at 115200 baud via pyocd-native; excerpt=(none)
UART exchange did not match on COM12 at 115200 baud; wrote 1 byte(s); excerpt=(none)
```

The first three reads used the original application, including a reset/re-run. After the HSI16 rebuild and flash, reset-on-open was attempted; repeated misses caused the server’s UART-miss guard, so the board was disconnected and reconnected, then validated exactly as instructed. A fresh reset-on-open read and a state-preserving serial exchange still produced no bytes. This required acceptance check is unresolved.

## Debug evidence

- Application control flow confirmed: halted `PC=0x080081FE`, `MSP=0x20017FD8`.
- `find_symbol`: `acceptance_counter@0x20000000`, `acceptance_tick@0x080080D9`.
- `set_breakpoint` plan `plan-c8626ad4efadb218` accepted; breakpoint hit with `PC=0x080080D8`; removed at `0x080080D9`; resumed; final state was RUNNING before disconnect.
- Symbol read returned server value `0xDEADF025`; recorded as inconclusive rather than treating it as a counter observation.
- Peripheral read of USART2 block was inconclusive (zero-filled response).

## Files

Created source/build files: `Makefile`, `startup_boot.S`, `startup_app.S`, `bootloader.c`, `application.c`, `bootloader.ld`, `application.ld`, and this `journey.md`. Server-created setup/safety evidence is under `.firm/`. The acceptance run is not fully green because COM12 yielded no `GOOD` transcript despite green setup, flashing, execution, and breakpoint checks.

## Orchestrator failure-loop closure (green)

After the subagent stopped on the empty COM12 transcript, the top-level orchestrator independently diagnosed the runtime registers through a new validated MCP run. RCC clock selection and GPIO configuration were live, but `RCC_APB1ENR1` and all USART2 configuration registers remained zero. Disassembly and source review identified the root cause: the application used `0x40005858` instead of `0x40021058`, so USART2 was never clocked. The orchestrator's written plan was to correct that address, give the invalid-image bootloader path the same explicit HSI16/APB1 setup, add standard `.data` copy/`.bss` zero startup, constrain the bootloader's MSP check to reviewed SRAM1, rebuild in new helper-owned roots, and guarded-flash exact collected artifacts.

The corrected bootloader/application builds passed through `pyocd_debug_mcp.native_build`, collection, and accepted plans `plan-673b5bc2c9369024` / `plan-75d3c35b0d86a196` (with final boot boundary correction plan `plan-80a25ea28e14f148`). Canonical final application SHA-256 was `42a787d309f665df7219cf1a26b09b15c626c2299c0f2eea531c7fd9e0cd9c3f`; final bootloader SHA-256 was `8890ba8e805024bdf47bc15b787fbc9b696de71ec61f41479b7fa7f09972face`.

Root COM12 retest passed with 31 parsed, strictly increasing `GOOD <counter>` lines (including `GOOD 2` through `GOOD 19` and `GOOD 32` through `GOOD 44`). The malformed fragment observed while the host opened mid-transmission was ignored; all complete parsed lines were ordered. The custom bootloader transferred to the offset app and the corrected application left the target running. No unlock, mass erase, manual erase, option/security write, raw write, or bootloader-specific backend route was used; all programming used guarded `flash_application` plans and artifact-derived addresses.
