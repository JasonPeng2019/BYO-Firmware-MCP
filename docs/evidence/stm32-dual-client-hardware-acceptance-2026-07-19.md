# STM32 dual-client generalized-build hardware acceptance — 2026-07-19

## Result

The attached NUCLEO-L476RG passed two independent fresh-root engineering journeys using only the
general `pyocd_debug_mcp.native_build` helper and guarded MCP hardware paths.

| Leg | Exact client | Fresh setup | General build | Guarded flash | Root UART | MCP debug |
| --- | --- | --- | --- | --- | --- | --- |
| Custom bootloader + offset app | Codex `gpt-5.6-luna`, medium | pass | pass | pass | pass | pass in agent run |
| Queued bare-metal RTOS | Claude Code `claude-sonnet-5`, medium | pass | pass | pass | pass | pass by root on exact ELF |

Both roots initially contained only the supplied STM32L476xx PDF. Neither journey used Zephyr,
the Zephyr-specific helper, a download, a package install, an alternate flashing/debugging utility,
unlock, mass erase, manual erase, option/security writes, or the connected Nordic/J-Link route.

## GPT bootloader journey

GPT built a 32 KiB bootloader at `0x08000000` and an application at `0x08008000`. The app toggles
PA5 and repeatedly prints an increasing `GOOD <counter>` on USART2/COM12. Runtime containment
correctly rejected intermediate non-Thumb and out-of-map MSP artifacts before backend mutation.

The first deployed app produced no UART. Root's bounded MCP register reads showed USART2 and its
APB1 clock disabled while clock/GPIO state was live. Source and disassembly proved the application
had mistyped RCC APB1ENR1 as `0x40005858` instead of `0x40021058`. Root corrected that defect,
standardized `.data`/`.bss` startup, gave the bootloader fail-closed UART path explicit HSI16/APB1
setup, and rebuilt in fresh helper roots. Root also corrected the bootloader's valid top-of-SRAM1
MSP comparison from `>= 0x20018000` to `> 0x20018000`.

Final hashes:

- bootloader ELF: `8890ba8e805024bdf47bc15b787fbc9b696de71ec61f41479b7fa7f09972face`;
- application ELF: `42a787d309f665df7219cf1a26b09b15c626c2299c0f2eea531c7fd9e0cd9c3f`.

The final bootloader correction used plan `plan-80a25ea28e14f148`. Root COM12 verification parsed
31 strictly increasing complete `GOOD` lines and left the firmware running.

## Claude queued-RTOS journey

Claude R3 built and guarded-flashed a freestanding offset application with explicit HSI16 clock
ownership, 1 ms SysTick, a cooperative real-time scheduler, independent LED/publisher/console/job
tasks, an interrupt-safe USART command queue, and task-context state changes. It used reviewed
SRAM1 only and placed the MSP strictly below `0x20018000`.

- server run: `run-20260719T072810Z-cb940338`;
- setup plan: `plan-b0d05082afa66b2f`;
- flash plan: `plan-d4ef36d1149f2215`;
- ELF: `adb248169a037d812127cefbada6cbdedd3be413e23449a3842d9af856389659`;
- map: `4340aacb5d383e07e75811192db8ac12d467161dd39231862b5d21e80a77c9f2`.

Claude then hit its provider five-hour usage limit after reset-and-run, before UART/debug
verification. The raw stream recorded `rateLimitType=five_hour` and a 04:40 local reset. Under the
documented Claude Usage Carve-Out, no provider completion after that point is claimed.

Root independently kept COM12 open while issuing `status`, `led 100`, `print 300`, `pause`, a
second `status`, `enqueue alpha42`, and `resume`. The transcript proves 100 ms LED timestamps,
300 ms publisher timestamps, no scheduled LED/publisher events after pause ACK, console response
while paused, queued job completion, and resumed interleaving. Root then used a fresh validated MCP
run on the exact ELF: `led_task@0x0800883D` halted at PC `0x0800883C`; the breakpoint was removed,
execution resumed to RUNNING, and the server disconnected.

## Product gaps and generality

Fresh hardware runs and final hostile review found and closed four server gaps:

- GAP-29: fresh preflight omitted repository-pinned pack authority;
- GAP-30: fresh setup discarded reviewed under-reset/1 MHz connection policy;
- GAP-31: validation ignored the repository-pinned verified pack used by setup.
- GAP-32: preflight accepted a verified target provider without checking its exact reviewed
  catalog filename, digest, target, and board binding before connection.

The fixes reuse general catalog, pack-selection, connection-policy, and validation paths. They add
no STM32-specific user step and do not narrow ordinary firmware, board, OS, linker, or RTOS choices.
Built-in targets keep their existing zero-pack path; non-built-in targets require only the same
single verified local pack already required for connection. The Make provider is project-neutral
and leaves the existing Zephyr provider unchanged.

Final software closure after GAP-32: focused pack/setup/evidence/target checks passed (52 tests),
the complete locked suite passed with 1054 tests and 3 skips, repository-wide Ruff and Pyright were
clean, package build/import passed, and a fresh-root bounded stdio handshake passed with 39 tools.

## Evidence files

- `stm32-gpt-5.6-luna-medium-journey-2026-07-19.md`
- `stm32-gpt-5.6-luna-medium-root-flash-2026-07-19.txt`
- `stm32-gpt-5.6-luna-medium-boot-boundary-fix-2026-07-19.txt`
- `stm32-gpt-5.6-luna-medium-root-uart-2026-07-19.txt`
- `stm32-claude-sonnet-5-medium-journey-2026-07-19.md`
- `stm32-claude-sonnet-5-medium-root-uart-2026-07-19.json`
- `stm32-claude-sonnet-5-medium-root-debug-2026-07-19.txt`
- provider prompts, MCP configuration, and sanitized launch records in this directory.
