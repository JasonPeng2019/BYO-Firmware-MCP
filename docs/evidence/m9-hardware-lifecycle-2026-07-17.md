# M9 lifecycle hardware bench — 2026-07-17

## Outcome

The Nucleo lifecycle path passed on ST-Link/COM12 identity
`066FFF514988525067233337`. The official Python MCP SDK 1.28.1 sent explicit
`notifications/cancelled` messages for a bounded UART read and a real disposable
application flash. UART cancellation returned promptly and the same client
reconnected. The application flash finished before debug, reset-line, and board
cleanup, and full v2 readback matched all 18,104 application bytes.

The flash cancellation exposed one client interoperability gap: after backend
completion and cleanup, this production server session did not deliver a terminal
response to the SDK client until the client process was terminated. The same SDK
client receives `McpError: Request cancelled` from the fake non-interruptible flash
integration test. Server-side flash safety and cleanup passed; the production
hardware terminal-response row is partial.

No `target_unlock`, bootloader flash, mass erase, or out-of-range write was called.

Machine-readable results are in
[`m9-hardware-lifecycle-2026-07-17.json`](m9-hardware-lifecycle-2026-07-17.json).

## Positive hardware identity and artifacts

- Probe: STM32 ST-Link `066FFF514988525067233337`.
- UART: COM12, USB serial `066FFF514988525067233337`, VID/PID `0483:374B`.
- Target: `stm32l476rgtx`; this is the same pair accepted by the M7 Nucleo run.
- Isolated root:
  `C:\Users\Jason\Documents\Jason\FirmCLI\M9-Hardware-Lifecycle\2026-07-17_m9_run5`.
- Safety map SHA-256:
  `36f1fcbca5cfb152fe9312c3fa775c93f47ab1f6d3e124409dc55fc14a47d10d`.
- v2 ELF SHA-256:
  `02b381d77b5a41e314b09d5035883dc38eded619db9148b0d4f3babb35191ab2`.
- v2 HEX SHA-256:
  `6d92f9c0ec551758c546d80cd9dcb15eb65cb99f54a6228a1eeeb8c58b8c1463`.
- Preserved application backup SHA-256:
  `7a25cef9f1319213beb90238727c14abfde0d61f7b1b5287901ebf368ff286ae`.

The attached J-Link/UART pair was excluded. Prior live identity evidence says it is
an nRF52840 rather than the required `nrf52833dk`.

## Observed lifecycle

Serial request 7 opened COM12 at `16:34:17.768117Z`; cancellation was sent at
`16:34:17.795123Z`; UART closed at `16:34:17.974025Z`; debug closed at
`16:34:17.987026Z`; reset was deasserted and reset-to-run followed. The same MCP
client began reconnecting at `16:34:18.020100Z` and succeeded.

Flash request 13 created a sector-only programmer and entered the real programmer
at `16:34:49.484097Z`. Cancellation was sent 11 ms later. Programming completed at
`16:34:50.894284Z`; debug was released at `16:34:51.015668Z`; reset was deasserted
and the target was reset to run afterward. The ordering proves non-interruptible
flash completion preceded release.

After the client wait was terminated, pyOCD reacquired the probe without physical
intervention, COM12 reopened, and complete halted readback of
`[0x08000000, 0x080046B8)` matched the v2 HEX. Reset then left the firmware in its
normal `SLEEPING` state. A fresh production MCP session also connected successfully;
a running-core PC read was refused because the core was not halted, then abnormal
cleanup closed that session as designed.

Raw, argument-free traces:

- `protocol-trace.jsonl` SHA-256
  `81ed5016152276d5f0a9f65810593df039ea105ae2cdbedf9f4e4c19ee96d8fd`.
- `backend-lifecycle.jsonl` SHA-256
  `c46c56476251f548d581a237f60a28243b99d1bac84e04c385ccbca65ded9f79`.
- `reconnect-backend.jsonl` SHA-256
  `dd767bcaf3d7236cfcb99a508e648fbc2cd3e609779ab87480d330a611431143`.

The protocol trace contains request IDs, method/tool names, timestamps, and error
types only. It records neither tool arguments nor UART data.

## Q-1 client matrix

| Client | Version | Serial interrupt | Flash interrupt |
| --- | --- | --- | --- |
| Official Python MCP SDK bench client | 1.28.1 | Verified notification and terminal `McpError` | Notification and safe cleanup verified; terminal response delayed until client termination |
| Codex CLI | 0.142.2 | Unverified: UI Stop is unavailable to this noninteractive bench | Unverified for the same reason |
| Claude Code | 2.1.76 | Unverified: UI interrupt unavailable and project MCP server not configured | Unverified for the same reason |
| VS Code | 1.129.0 | Unverified: UI Stop is unavailable to this noninteractive bench | Unverified for the same reason |
| Positively identified non-sending client | Not present | Unverified | Unverified |

For every unverified client row, the guaranteed fallback remains the passing bounded
timeout cleanup path. Hardware cancellation of a deliberately slowed raw memory read
also remains unverified because a live read completes too quickly and no test-only
delay was inserted into the production backend. The bounded UART read is the live
interruptible-read proof.

## Defect found and fixed

The first stdio attempt showed `board_validate` completing all nine steps in 30.4
seconds while inheriting the generic 30-second operation ceiling. It therefore
returned a timeout despite a successful immutable report. `board_validate` now has
a dedicated finite 120-second ceiling, with a regression assertion. This is long
enough for bounded probe/UART inventory and validation while retaining A-11.

The bench harness also now bounds its own wait for a post-cancellation terminal
response to five seconds, so a client/server interoperability gap cannot strand the
bench helper.

## Exact primary command

```powershell
uv run python scripts/m9_hardware_lifecycle.py --source-root 'C:\Users\Jason\Documents\Jason\FirmCLI\M7-Hardware-Acceptance\2026-07-17_m7_run4' --artifact-root 'C:\Users\Jason\Documents\Jason\FirmCLI\M9-Hardware-Lifecycle\2026-07-17_m9_run5' --v1-elf 'C:\Users\Jason\Documents\Jason\FirmCLI\M7-Hardware-Acceptance\2026-07-17_m7_run1\builds\v1\reference\build\firmware.elf' --v1-hex 'C:\Users\Jason\Documents\Jason\FirmCLI\M7-Hardware-Acceptance\2026-07-17_m7_run1\builds\v1\reference\build\firmware.hex' --v2-elf 'C:\Users\Jason\Documents\Jason\FirmCLI\M7-Hardware-Acceptance\2026-07-17_m7_run1\builds\v2\reference\build\firmware.elf' --v2-hex 'C:\Users\Jason\Documents\Jason\FirmCLI\M7-Hardware-Acceptance\2026-07-17_m7_run1\builds\v2\reference\build\firmware.hex' --backup-hex 'C:\Users\Jason\Documents\Jason\FirmCLI\M7-Hardware-Acceptance\2026-07-17_m7_run1\backups\nucleo_l476rg_application_before.hex' --probe-id 066FFF514988525067233337 --serial-id COM12 --board-id nucleo_l476rg --target stm32l476rgtx --baudrate 115200
```

One preparation attempt failed on the first application page. It was immediately
restored with the pinned pack and sector erase only:

```powershell
uv run pyocd load -u 066FFF514988525067233337 -t stm32l476rgtx --pack packs\Keil.STM32L4xx_DFP.3.1.0.pack -f 1000000 -M under-reset -e sector C:\Users\Jason\Documents\Jason\FirmCLI\M7-Hardware-Acceptance\2026-07-17_m7_run1\builds\v2\reference\build\firmware.hex
```

The restore erased 9 application sectors, programmed 18,432 bytes, used no mass
erase, and full readback passed before the final acceptance attempt.

## Software checks

```text
uv run pytest tests/test_kernel_operations.py tests/test_lifecycle_stdio_integration.py tests/test_m9_audit.py -q
19 passed in 11.16s

uv run ruff check src/pyocd_debug_mcp/kernel/operations.py tests/test_kernel_operations.py scripts/m9_instrumented_server.py scripts/m9_hardware_lifecycle.py
All checks passed!

uv run pyright src/pyocd_debug_mcp/kernel/operations.py tests/test_kernel_operations.py scripts/m9_instrumented_server.py scripts/m9_hardware_lifecycle.py
0 errors, 0 warnings, 0 informations
```
