# Live STM32 acceptance subagent B ? Claude Sonnet 5 medium

You are an external acceptance subagent running in a brand-new repository. At launch it contains only `stm32l476je (2).pdf`. The attached physical target is an ST NUCLEO-L476RG with ST-Link UID `066FFF514988525067233337` and ST-Link VCP `COM12`. A previously accepted test bootloader occupies `0x08000000..0x08007FFF` and jumps to an application at `0x08008000`. The user authorizes replacing only the offset application through accepted MCP plans.

Use the `pyocd-debug` MCP server from start to finish. Begin with `initialization_handshake`, follow server routing, perform fresh setup from the PDF, validate, and use the build guidance returned by `get_setup_status`.

For setup naming, use the new unique familiar name `STM32 Claude RTOS Acceptance R3` for the ST-Link board. Inventory may also show the already-known Nordic/J-Link companion; name it `Nordic companion inventory only` solely to satisfy one-to-one startup routing, but never load or execute its route. Obtain current connection IDs from `setup_overview` and copy them exactly.

HARD RULES:
- Use the GENERAL local-only build helper `python -m pyocd_debug_mcp.native_build` and generalized MCP mechanisms. Never use `pyocd_debug_mcp.zephyr_build`, west, Zephyr/NCS, OpenOCD, STM32CubeProgrammer, direct pyOCD CLI, direct serial libraries, or backup routes.
- No downloads or package installs. Use server-discovered local STM32CubeIDE Make + Arm GCC.
- Preserve `0x08000000..0x08007FFF`. Never unlock, mass erase, manual erase, flash a bootloader, write option/security bytes, write raw registers, or touch the Nordic/J-Link device/COM11.
- Mutation only through all-NULL teaching then accepted `flash_application-plan` with `user_permission=true`, followed by the exact execution/fallback. Build once unless a diagnosed code defect forces a rebuild.
- Do not edit the BYO server. Report genuine server defects to the orchestrator before unsafe action.

ENGINEERING TEST:
Build a non-Zephyr, freestanding custom real-time bare-metal OS/application for Cortex-M4F, linked at `0x08008000`. It must include:
- correct vector/startup code and linker ASSERTs keeping every load segment at/above `0x08008000` and inside STM32L476RG flash/RAM;
- use only reviewed SRAM1 0x20000000..0x20017FFF, place the initial MSP strictly below 0x20018000, and explicitly establish the application's own clock before deriving USART/SysTick divisors: the resident bootloader selects HSI16 and branches without a core reset, so reset-clock assumptions are invalid;
- a 1 ms SysTick timebase and an explicit scheduler with multiple independently scheduled tasks;
- a bounded, interrupt-safe command queue: USART2 RX on PA2/PA3 at 115200 assembles console lines and queues parsed/validated command messages; scheduled task context dequeues and applies them (do not execute substantive commands in the ISR);
- a PA5 LED task and periodic UART publisher task that continue concurrently;
- console commands `status`, `led <ms>`, `print <ms>`, `pause`, `resume`, and `enqueue <text-or-number>` (or a similarly clear queued job), with bounds and error responses;
- observable timestamped `LED` and `PRINT` lines, `ACK` responses, queue/job completion output, and exported volatile scheduler/queue counters plus non-inlined task functions for debugging.

Use one ordinary Makefile target `application` and the exact general-helper command form returned by setup status, in one new build directory producing exactly one ELF/map. Collect artifacts, then guarded-flash only the offset ELF through `flash_application` so sector programming cannot touch the bootloader.

VERIFY THROUGH MCP:
- Over COM12 prove startup through the resident bootloader and collect timestamped output.
- Exercise every console command. Prove `led` changes LED-event spacing, `print` changes publisher spacing, pause stops scheduled observable events while the console still responds, resume restarts them, and queued jobs complete. Capture interleaved output proving scheduler/console concurrency.
- Debug successfully: connect/halt, inspect PC/SP and exported scheduler/queue symbols, set a planned breakpoint on a current-ELF task function, observe it hit, remove it, resume, disconnect, and leave firmware running. Revalidate after reconnect when instructed.

Record `journey.md` in this repository with exact model (`claude-sonnet-5`), effort (`medium`), CLI version if observable, run ID, setup/validation, exact general build command/result, artifact hashes, plan IDs, flash containment result, complete UART transcript, debug evidence, and every failure/diagnosis/retest. Finish only when every check is green. Final response: concise evidence summary and exact files created.
