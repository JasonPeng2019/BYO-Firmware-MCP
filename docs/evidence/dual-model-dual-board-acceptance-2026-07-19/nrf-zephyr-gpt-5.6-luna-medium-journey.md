# nRF52840-DK hardware acceptance journey

- Model: `gpt-5.6-luna`
- Effort: `medium`
- Run ID: `019f7d60-cd1e-7643-821b-42a82dd0973e`
- Repository: `C:\firmcli-acceptance-20260719\nrf-zephyr-luna-r1\repo`
- Board: nRF52840-DK, nRF52840-QIAA
- Probe: onboard J-Link UID `683377322`
- UART: J-Link VCP `COM11`, stable USB identity `000683377322`, `115200` baud

## Environment and setup

The authoritative local installation used throughout was:

- NCS/Zephyr workspace: `C:\ncs\v3.3.1`
- Toolchain: `C:\ncs\toolchains\936afb6332`
- Toolchain environment: `C:\ncs\toolchains\936afb6332\environment.json`
- West executable: `C:\ncs\toolchains\936afb6332\opt\bin\Scripts\west.exe`
- Target: `nrf52840dk/nrf52840`
- Provider: `zephyr-west`

The local NCS installation was inspected and validated before build tooling. No `west init`, `west update`, `pip install`, package-manager operation, or HTTP fetch of Zephyr/NCS/RTOS source occurred. No prior firmware project, board profile, manifest, example, evidence, or source from the BYO-Server checkout was inspected or copied. The general native-build helper was used, never the Zephyr-specific helper.

Initial setup routed the existing nRF52840-DK profile, assigned probe `683377322`, and validated the live MCU identity as exact `FICR INFO.PART 0x00052840`. Setup plan: `plan-43dc56b878c9c907`. Later reconnects repeated setup routing and live validation before guarded/debug operations.

## Build and initial flash

The exact provider-neutral helper returned by setup was:

```text
C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\.venv\Scripts\python.exe -m pyocd_debug_mcp.native_build --project-dir <project-dir> --build-dir <new-empty-build-dir> --target <project-native-target>
```

It was resolved for this repository as target `nrf52840dk/nrf52840`, with offline guards enabled and no helper provisioning. The interrupted first build (`build-native-10`) was checked rather than assumed incomplete; its ELF, HEX, and MAP were present and current, so it was not rebuilt.

The application implemented real concurrent Zephyr threads for LED work, console service, periodic printing, and event-queue service. Shared settings use `luna_settings_mutex`; UART output uses `luna_uart_mutex`; LED events use `luna_event_queue`. Exported scheduler/thread/queue/counter symbols include `luna_led_thread`, `luna_console_thread`, `luna_periodic_thread`, `luna_event_thread`, `luna_event_queue`, `luna_shared_settings`, `luna_led_toggle_count`, and `luna_periodic_print_count`.

The first coherent artifact collection was `artifacts-fixed6`:

- ELF `bd526b23abcf23f4ccb6df4fa521bdd6cdbdeda301b5f8321e30d3e8a24b12ef`
- HEX `d488bcd95a1340c76673e395d5179d9af2e9ac9eeb97158294e53dadf4a5ad06`
- MAP `df02a76f35b03c78f9d44dd9338aa7da5cf161e1e57abd7bd37829f189b6e2e2`

Flash plans and outcomes:

- `plan-93d07fc19237778b`: accepted, then safely refused because the MCP board session had expired.
- `plan-90898b6e67585bc5`: accepted, then safely refused because the newly reconnected session required board validation.
- `plan-c3b9f5a719d4be34`: successful bounded application flash; target left running.

## UART proof and firmware corrections

The first UART proof exposed an event-queue design failure. The event queue had no consumer, filled, and emitted persistent `EVT queue-full` output that starved the console. The firmware was corrected with a dedicated event worker, serialized UART output, and higher console priority.

The next `led 200` failures showed the command text arriving but no acknowledgement and LED output remaining at 500 ms. The parser was rejecting the VCP trailing CR/LF controls after the decimal value. The parser was changed to accept a bounded decimal prefix followed by spaces, tabs, CR, or LF.

The interrupted-build correction was already included in `build-native-10`; no rebuild was needed for that check. The failed/retried `led 200` UART plans included `plan-02fd82a284a07747`, `plan-5843cfd688447bfe`, and `plan-e7522426f1250b3d`. The fixed6 `led 200` attempt was `plan-64416bc634a8f4e3`, which still failed because the UART burst was losing its terminator.

The next root cause was one-byte-per-5-ms UART polling. A command burst could overrun before its terminator arrived. The smallest correction drained all available polling bytes; fixed7 was built with the exact general helper and collected:

- ELF `012a449a4040dc525195cead76f0aee9ec7e1c34a441e9a92e0d3f95decc36d0`
- HEX `d22038092c28bdb558516e7db1087595820feaf54cf80d317dde0ab27c159632`
- MAP `f474be2707b45a88f4935bf02d2072cb95d2d40db31dfdd118a0f826cfea86d3`
- Flash plan `plan-d4532e78108be111`

That fixed `led 200` proof passed. Before-change raw LED lines were:

```text
[12397 ms] LED toggle #24 state=OFF interval=500 ms
[12898 ms] LED toggle #25 state=ON interval=500 ms
[13398 ms] LED toggle #26 state=OFF interval=500 ms
[13898 ms] LED toggle #27 state=ON interval=500 ms
```

The deltas were 501, 500, and 500 ms. The command response and post-change evidence were:

```text
led 200[35297 ms] LED interval changed to 200 ms
Commands: help | status | led <ms> | print <ms>
[35440 ms] LED toggle #70 state=OFF interval=200 ms
[60894 ms] LED toggle #197 state=ON interval=200 ms
[61095 ms] LED toggle #198 state=OFF interval=200 ms
[61295 ms] LED toggle #199 state=ON interval=200 ms
[61495 ms] LED toggle #200 state=OFF interval=200 ms
[61695 ms] LED toggle #201 state=ON interval=200 ms
```

Post-change deltas were 201, 200, 200, and 200 ms. Plans for these captures were `plan-ba6ce8dd422524e6`, `plan-7557d169b06297ff`, and `plan-23f96733321d3f6c`.

The first periodic-print proof failed because `print 250` still lost bytes: the exchange wrote 11 bytes but the firmware received only a partial burst. Removing blocking per-byte echo was insufficient. The failed exchange plan was `plan-bbfebfccbc3b8da1`; the earlier red fixed7-era attempt was `plan-c5c4101d4ea2f3a2`.

The final correction replaced polling with Zephyr interrupt-driven UART RX feeding a bounded `luna_console_rx_queue`, and enabled `CONFIG_UART_INTERRUPT_DRIVEN`. This was the only firmware rebuild required for the final periodic proof. The final build was `build-native-13`, using the exact general helper, and final artifacts were collected in `artifacts-fixed9`:

- ELF: `469a8ca6add033a69e5ba97308f4d5ec8197d4d72fbb9d62c403b23278bab223`
- HEX: `3d732dbd3859a72d33bfc9462b65ee07ca2a2b62d423de257ca0ecd5d1b38b1e`
- MAP: `f0e9b66c9a1fca3a70bc3561e62a905014f669c749a95236b80111faba61788f`

Final periodic flash plan: `plan-7062a5c84c60f993`.

Default periodic evidence included:

```text
[12390 ms] PERIODIC #4 led_interval=500 ms print_interval=3000 ms
[12396 ms] LED toggle #24 state=OFF interval=500 ms
[36437 ms] PERIODIC #12 led_interval=500 ms print_interval=3000 ms
[36443 ms] LED toggle #72 state=OFF interval=500 ms
```

Representative default delta: `(36437 - 12390) / 8 = 3005.9 ms`.

The successful complete exchange was plan `plan-0076ed3f60f51284`:

```text
UART exchange matched on COM11 at 115200 baud; wrote 11 byte(s)
[34227 ms] Print interval changed to 250 ms
Commands: help | status | led <ms> | print <ms>
[34438 ms] LED toggle #68 state=OFF interval=500 ms
```

Post-change captures were `plan-2fd68d396bc45f0d` and `plan-e0cad647abe3ec9f`:

```text
[61782 ms] PERIODIC #111 led_interval=500 ms print_interval=250 ms
[61960 ms] LED toggle #123 state=ON interval=500 ms
[62038 ms] PERIODIC #112 led_interval=500 ms print_interval=250 ms
[62294 ms] PERIODIC #113 led_interval=500 ms print_interval=250 ms
[62460 ms] LED toggle #124
```

The periodic deltas were 256 and 256 ms. A later final post-command capture showed:

```text
[37297 ms] PERIODIC #48 led_interval=500 ms print_interval=250 ms
[37322 ms] LED toggle #74 state=OFF interval=500 ms
[37553 ms] PERIODIC #49 led_interval=500 ms print_interval=250 ms
[37809 ms] PERIODIC #50 led_interval=500 ms print_interval=250 ms
[37822 ms] LED toggle #75
```

## Complete console command phase

The documented commands were exercised one at a time through `serial_exchange`:

- `help`: initial halted-session plan `plan-6e6eb7dc0536fe48` failed with no UART; after reset-and-run, `plan-c1797252e1661ff6` passed.
- `status`: `plan-ee584908327fe804` passed with a complete timestamped status line.
- `led 200`: `plan-f41a735863030375` passed.
- `print 250`: `plan-69680a4ac391eca9` passed after the reset restored print to its default.
- Final combined `status`: `plan-95b0153695aa95f5` passed.
- Immediate concurrency capture: `plan-244382cfb260b990`.

Verbatim command evidence:

```text
Commands: help | status | led <ms> | print <ms>
[25059 ms] STATUS led=ON led_interval=500 ms print_interval=3000 ms toggles=49 periodic=8
[35406 ms] LED interval changed to 200 ms
Commands: help | status | led <ms> | print <ms>
[68219 ms] Print interval changed to 250 ms
Commands: help | status | led <ms> | print <ms>
[68254 ms] LED toggle #233 state=ON interval=200 ms
[79157 ms] STATUS led=ON led_interval=200 ms print_interval=250 ms toggles=287 periodic=61
Commands: help | status | led <ms> | print <ms>
[79276 ms] LED toggle #288 state=OFF interval=200 ms
[93701 ms] LED toggle #360 state=OFF interval=200 ms
[93719 ms] PERIODIC #118 led_interval=200 ms print_interval=250 ms
[93901 ms] LED toggle #361 state=ON interval=200 ms
[93975 ms] PERIODIC #119 led_interval=200 ms print_interval=250 ms
[94101 ms] LED toggle #362 state=OFF interval=200 ms
```

One final status attempt after reset initially reported `print_interval=3000 ms` despite LED being 200 ms (`plan-41d91ecfbfd70cb5`). This was diagnosed as expected runtime reset-to-default behavior, not a firmware failure. `print 250` was reapplied and the final combined status passed.

## Live debug

The new MCP debug process initially resolved symbols against an unrelated BYO-Server reference ELF. The current byte-identical fixed9 ELF was rebound through guarded application flashing using `plan-8dcb3913d454d751`; no symbol or safety check was weakened.

After halt, PC/SP were read as:

```text
PC = 0x00006592
SP = 0x20002068
```

Current fixed9 symbol reads included:

```text
luna_shared_settings      @ 0x20000000 value=0x000001F4
luna_event_queue          @ 0x200006A4 value=0x200001C0
luna_led_toggle_count     @ 0x20000674 value=0x0000000E
luna_periodic_print_count @ 0x20000670 value=0x00000002
luna_led_thread           @ 0x200003A0 value=0x00000000
luna_console_thread       @ 0x20000300 value=0x20000678
luna_periodic_thread      @ 0x20000260 value=0x00000000
luna_event_thread         @ 0x200001C0 value=0x200006A4
```

The exported task functions resolved as `luna_led_worker@0x000007C1`, `luna_event_worker@0x0000074D`, and `luna_console_worker@0x00000489`. Early LED-worker and event-worker entry breakpoint attempts did not produce a hit because the worker had already entered a blocking loop or the MCP wait workflow tore down the session. Those retries were diagnosed and cleaned up with plans `plan-b1870c41d49c9ae8`, `plan-6f62008c636772f3`, `plan-f2d9b9be67ee070e`, `plan-669e94a7fe0cd500`, `plan-ede60ea47c854783`, and `plan-b209e2b418aeb50f`.

The reliable final breakpoint was placed inside the verified `luna_event_worker` body at instruction address `0x00000758`, using fixed9 and plan `plan-38c501057df19b76`. After resume and a bounded in-process observation, the target reported:

```text
PC = 0x00000758
state = HALTED
```

This is an actual instruction in `luna_event_worker` (the function entry is `0x74C` in the ELF disassembly). The breakpoint was removed at `0x00000758`, the core was resumed, and the final MCP state was:

```text
RUNNING
```

## Final state

Board RUNNING.
