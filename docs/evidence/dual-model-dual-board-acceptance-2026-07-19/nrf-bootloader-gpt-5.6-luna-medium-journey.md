# nRF52840-DK acceptance journey

## Run identity

- Model: `gpt-5.6-luna`.
- Reasoning effort: `medium`.
- Server run ID: `run-20260720T011749Z-f7b89c90`
- Board: nRF52840-DK, nRF52840-QIAA.
- Probe: onboard J-Link UID `683377322`.
- UART: J-Link VCP COM11, stable USB identity `000683377322`, 115200 baud.
- Repository input at start: `datasheet.pdf` and `.git` only.
- No prior firmware project, board profile, manifest, example, evidence, or source was copied from the BYO-Server checkout.

## Initial setup

1. Performed the pyocd-debug initialization handshake.
2. Routed familiar board name `nRF52840-DK` to probe `683377322` and COM11 through setup overview.
3. Extracted the exact package-level part from the official local datasheet: `nRF52840-QIAA`.
4. Repaired the incomplete profile with setup plan `plan-4ede50b0b077ecb3`.
5. Executed the server-provided setup action through the exact action-batch fallback because the dynamic action was not exposed.
6. Setup completed with exact detected target `nrf52840`, datasheet SHA-256 `c619e336b9c0610663273041f057f2537a65fd408ce0c5b8214a26de2aa88422`, and a passed non-destructive validation.
7. `get_setup_status` reported `ready_for_code=true`, `ready_for_flash_planning=true`, and UART ready.

## Firmware implementation and first build

Authored a freestanding image consisting of:

- `startup.S`: vector table, reset handler, data copy, BSS clear, and default handler.
- `main.c`: direct-register UART0 at 115200 baud, active-low DK LED1 blink, and repeated `BOOT OK`.
- `nrf52840.ld`: flash origin `0x00000000`, 1 MiB flash, 256 KiB RAM, vectors/text/data/BSS placement.
- `Makefile`: provider-neutral native GNU Make target.

The exact general helper returned by `get_setup_status.build_guidance` was used:

`C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\.venv\Scripts\python.exe -m pyocd_debug_mcp.native_build --project-dir <repo> --build-dir <new-empty-build-dir> --target all`

The first build completed successfully in `native-build`. Initial collected artifacts were placed in `artifacts`:

- ELF `fb92f932429ae4e0f887093154c309f4056c1f6913036f11b2b475c7cdf24af3`
- HEX `c64ca8c54ab6c1bebc3c41bbd1f66638839ea5092dfd8bac5fac913dbc76b5fe`
- BIN `969328fd0ae1bebc1b852c2e26e8a5843fef4d0c87877775b684f901209a9f45`
- MAP `f4924e3a7e8cee4bdbd24e8d5c3e2204b4ca2db2b1abe195c673263d233d98f9`

## Initial flash and UART attempts

- Flash plan `plan-83de7cbb1633f551` was accepted and executed successfully through the application-region guard. The target was left running.
- UART plan `plan-ba9897ff531f8164` was accepted, but execution failed before capture because the board had no active MCP connection.
- Reconnected and revalidated the board. The first post-reconnect capture plan `plan-0682a97f0cd7f42f` returned repeated `BOOT OK`, but the server measured only `0.22s`.
- The core was found halted; `reset_and_run` was issued.
- UART plan `plan-191fa9244008c19e` again returned `BOOT OK` but measured only `0.22s`.
- UART plan `plan-46bcd712e6dfc9bb` used an absent sentinel to avoid early matching; it returned repeated `BOOT OK` but measured only `0.89s`.

The short durations were diagnosed as a server capture-duration defect, not a firmware failure.

- After the server correction, plan `plan-bfbd59a35334ed12` was accepted but failed pre-execution because the connection had expired.
- Reconnected, ran setup overview, loaded validation, and passed live validation again.
- Reset-and-ran the target, then plan `plan-73a1dcb2c7305676` captured a complete `15.03s` window with repeated `BOOT OK`.

## First live-debug attempt

- Reconnected and revalidated after connection expiry.
- Halted and read PC `0x0000014C` and SP `0x2003FFF0`.
- The MCP symbol lookup initially resolved an unrelated legacy ELF context and found no suitable `boot_count` or `blink_count`; by-name reads returned symbol-not-found.
- The original firmware source also contained no exported runtime counters. This was diagnosed as a firmware implementation omission.
- Breakpoint plan `plan-37852458fcfc77d6` set `main` at `0x00000164`; resume did not hit because `main` is entered only once. It was removed.
- Breakpoint plan `plan-19a606540b6111b3` set the repeatedly executed `delay` function at `0x00000144`. Reset-and-run hit it with PC `0x00000144`; the breakpoint was removed and resume left the core `RUNNING`.

## Counter implementation and second build

Added exported globals to `main.c`:

```c
volatile uint32_t boot_count = 0u;
volatile uint32_t blink_count = 0u;
```

`boot_count` increments once in `main`; `blink_count` increments once per blink loop.

The exact general helper was run once more in new directory `native-build-2`. Collection produced `artifacts-2`:

- ELF `84de638b0bb8e37fe6a6a1840d9005bb5bcba51f39edaf355a503fbf40c1abe2`
- HEX `0dda6308ee2878fadac8baf885a5224d53cd2a01584ac3595bf76a6651823bdf`
- BIN `8094bb449516ababc55b6447a01536e29040a631cab6372c4db229cd71ab9f08`
- MAP `e599df4014d2af7a63f15dc4b012caf5dd7c0768dc7644dcd3ba7a87ea5f7e9a`

Flash plan `plan-688d4eb89e33f718` was accepted but failed before execution because the MCP connection expired. After reconnect and validation, replacement flash plan `plan-6820e1f1a1d88c8f` executed successfully.

The updated image produced a full UART proof with plan `plan-a5640496308ff1b1`, measured duration `15.00s`, and repeated `BOOT OK`. The first numeric breakpoint plan `plan-7ee4733135a5069d` did not hit on the initial reset/resume attempt; it was removed. A named delay breakpoint plan `plan-10bf8a7756a88437` later hit successfully and was removed, leaving the core running.

Live reads from this image resolved the symbols, but exposed a new correctness failure: `boot_count` retained stale RAM while the adjacent `blink_count` was cleared.

## BSS initialization diagnosis and final fix

The collected map showed both counters in adjacent BSS locations:

- `blink_count` at `0x20000000`
- `boot_count` at `0x20000004`

The reset handler contained:

```asm
str r3, [r1], #4
adds r1, #4
```

The post-indexed store already advanced `r1` by four bytes; the extra `adds` advanced it by another four. Therefore the BSS loop cleared every other word, clearing `blink_count` but skipping `boot_count`. This exactly explained the observed stale values `0x062D780E`/`0x000001D0` and then `0x062D780F`/`0x00000001`.

Changed file: `startup.S`. Removed the extra `adds r1, #4`, leaving one four-byte increment per cleared word.

## Final rebuild, flash, UART, and debug

The exact general helper was run once in new directory `native-build-3`. Collection produced `artifacts-3`:

- ELF `7067e780d23a11adf742d091543f37bbe8ea48169b175ab588e753ce1a36e2ee`
- HEX `18cfac805e6eea4f50910fecc2a973d87b5d6fdb422e9af7c2e2239debb4a26e`
- BIN `4315610cbdfd2a0aed6a3cbd0b2985985807e65ecb33369b0c214b934067f1bb`
- MAP `50037ce3dc0372377fcf4a4ad135c1e7119ec4bdc2ac54d8776c42c8ebab7741`

Flash plan `plan-bb942bd2cbb298ed` was accepted but failed pre-execution because the connection expired. After reconnect and live validation, replacement plan `plan-1c04ef6d5c6add1d` flashed the final ELF successfully within the application partition.

Issued explicit `reset_and_run` through the reset vector.

UART plan `plan-cd05969ea18592f4` captured the full requested window:

- MCP-measured duration: `15.03s`
- Repeated output: `BOOT OK\r\n`

After halting the reset-and-run image:

- PC: `0x0000014E`
- SP: `0x2003FFF0`
- `boot_count`: resolved by name from `artifacts-3/firmware.elf`, address `0x20000004`, value `0x00000001`
- `blink_count`: resolved by name from `artifacts-3/firmware.elf`, address `0x20000000`, value `0x000001AA`

Final breakpoint plan `plan-10bf8a7756a88437` set the real loop function `delay` by name. MCP resolved it to Thumb address `0x00000145`; resume halted with PC `0x00000144`. The breakpoint was removed at `0x00000145`, the core was resumed, and final state was `RUNNING`.

## Final status

The BSS initialization defect is fixed. Final UART, counter, PC/SP, breakpoint, and running-state evidence passed. The board is left running.
