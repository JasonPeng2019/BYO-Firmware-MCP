You are a fresh Claude Sonnet 5 medium-effort retry subagent for only the failed safe-breakpoint phase of an already completed hardware journey. Work only with `C:\cs5r4` and the `pyocd-debug` MCP server. Do not inspect Claude memory or any other project.

The user has already named the two visible connections `nrf52840dk` (Nordic/J-Link) and `nucleo_l476rg` (STM32/ST-Link). `setup_overview` requires both names and server-owned connection assignments, but every subsequent action must target only the established Nordic profile `nrf52840dk_2`. Leave STM32 completely untouched.

Do not set up, build, collect, flash, erase, reset, recover, unlock, write memory/registers, or use UART. The existing current ELF is `C:\cs5r4\artifacts\firmware.elf`. The top-level orchestrator has already left its telemetry streaming enabled, so the producer calls `gpio_pin_set_raw` periodically.

Run this bounded retry:

1. `initialization_handshake`.
2. `setup_overview` with both supplied familiar names and exact server inventory assignments.
3. Load `board_validate`, then validate `nrf52840dk_2` with the assigned Nordic probe.
4. Halt the Nordic target. Initialize `set_breakpoint-plan` with the all-NULL teaching call, then accept one current-ELF symbol breakpoint at `gpio_pin_set_raw` with a concrete hypothesis and one-call budget.
5. Execute the unchanged preferred call or stable single-child action-batch fallback, resume, wait 1000 ms, and observe state plus PC. Success requires `HALTED` at aligned PC `0x000006F4`.
6. Whether success or failure, remove the breakpoint at aligned address `0x000006F4`, resume, confirm the target is RUNNING or SLEEPING, disconnect, and leave the application running.

Write `C:\cs5r4\claude-debug-retry-report.md` with exact model/effort, CLI version, MCP run and plan IDs, calls/results, diagnosis that the earlier UART-trigger test never proved its command reached the shell while SWD was attached, this periodic-call retry result, and cleanup state. Do not claim green unless the core actually halted at PC `0x000006F4` and cleanup passed.
