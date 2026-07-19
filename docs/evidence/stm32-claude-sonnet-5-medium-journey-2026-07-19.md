# STM32 Claude RTOS Acceptance R3

- Provider/model/effort: Anthropic / `claude-sonnet-5` / medium.
- Claude Code: `2.1.76`.
- Server run: `run-20260719T072810Z-cb940338`.
- Fresh root began with only `stm32l476je (2).pdf`.
- Setup and validation passed for `STM32L476RGT6` / `stm32l476rgtx`, ST-Link COM12.
- General helper only: `python -m pyocd_debug_mcp.native_build`; no Zephyr helper, download, install, unlock, mass erase, manual erase, option/security write, or Nordic route.
- Exact build: `C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\.venv\Scripts\python.exe -m pyocd_debug_mcp.native_build --project-dir C:\stm32-claude-rtos-r3\app --build-dir C:\stm32-claude-rtos-r3\build\app-build-1 --target application`.
- ELF SHA-256: `adb248169a037d812127cefbada6cbdedd3be413e23449a3842d9af856389659`; map SHA-256: `4340aacb5d383e07e75811192db8ac12d467161dd39231862b5d21e80a77c9f2`.
- Guarded flash plan `plan-d4ef36d1149f2215` passed and left the target running.
- Firmware: explicit HSI16 ownership, 1 ms SysTick cooperative scheduler, interrupt-safe USART line/command queue, task-context command execution, independent LED/publisher/console/job tasks, and reviewed SRAM1-only linker containment.

Claude hit its five-hour provider usage limit immediately after reset-and-run while beginning UART verification. The provider returned `rateLimitType=five_hour`, reset 04:40 America/Los_Angeles. No Claude completion is claimed after that point.

Top-level root completed independent verification on the exact flashed bytes. One persistent COM12 session passed `status`, `led 100`, `print 300`, `pause`, paused `status`, `enqueue alpha42`, and `resume`. Device timestamps proved 100 ms LED spacing, 300 ms publisher spacing, no LED/publisher events after pause ACK, console response while paused, queued job completion, and resumed concurrent events. A fresh MCP run guarded-flashed the same exact artifact solely to restore run-scoped ELF binding, then proved `led_task@0x0800883D`, halted at PC `0x0800883C`, removed the breakpoint, resumed to RUNNING, and disconnected.

Failure loops preserved in R1/R2 evidence: the bootloader originally rejected the valid SRAM1 upper-bound MSP because its check used `>=`; root changed it to `>` and reflashed under plan `plan-80a25ea28e14f148`. R2 then assumed reset-clock MSI despite a branch-only HSI16 bootloader; root diagnosed that inherited-clock contract and required explicit application clock ownership in the fresh R3 prompt. R3 passed build/flash, and root UART/debug verification closed the provider-limit remainder.