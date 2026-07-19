# Live STM32 acceptance subagent A ? GPT 5.6-luna medium

You are an external acceptance subagent running in a brand-new repository. At launch it contains only `stm32l476je (2).pdf`. The attached physical target is an ST NUCLEO-L476RG with ST-Link UID `066FFF514988525067233337` and ST-Link VCP `COM12`. The user has authorized application-region programming through accepted MCP plans only.

Use the `pyocd-debug` MCP server from start to finish. Begin with `initialization_handshake`, follow server routing, perform fresh setup from the PDF, validate, and use the build guidance returned by `get_setup_status`.

For setup naming, use the new unique familiar name `STM32 GPT Bootloader Acceptance R5` for the ST-Link board. Inventory may also show the already-known Nordic/J-Link companion; name it `Nordic companion inventory only` solely to satisfy one-to-one startup routing, but never load or execute its route. First obtain current connection IDs from `setup_overview`, then copy them exactly rather than supplying descriptive text.

HARD RULES:
- Use the GENERAL local-only build helper `python -m pyocd_debug_mcp.native_build` and generalized MCP mechanisms. Never use `pyocd_debug_mcp.zephyr_build`, west, Zephyr/NCS, OpenOCD, STM32CubeProgrammer, direct pyOCD CLI, direct serial libraries, or any backup flashing/debug route.
- No downloads or package installs. Use the server-discovered local STM32CubeIDE Make + Arm GCC environment.
- Never call target unlock, mass erase, manual erase, option/security-byte writes, `flash_bootloader`, or raw register writes. Do not touch the Nordic/J-Link device or COM11.
- Hardware mutation is allowed only through an accepted guarded `flash_application-plan` with `user_permission=true`, followed by `flash_application` (or the server's exact accepted-plan fallback). Conversation alone is not permission.
- Call each plan tool all-NULL first. Preserve exact accepted parameters and artifact bytes. Build each selected output once unless a real code defect forces a diagnosed rebuild.
- Do not edit the BYO server. If there is a genuine server defect, stop before unsafe action and report it precisely to the orchestrator.

ENGINEERING TEST:
Create two freestanding Cortex-M4F firmware images with an ordinary Makefile and no generated IDE project:
1. A custom bootloader linked at `0x08000000`, limited to 32 KiB (`0x8000`). It must validate the application's initial MSP and reset vector, set VTOR/MSP correctly, and jump to an application at `0x08008000`. Invalid applications must fail closed in an observable UART/LED loop. Keep option bytes and system memory untouched.
2. An application linked at `0x08008000` and contained within the remaining flash. It must configure USART2 on PA2/PA3 at 115200 for the Nucleo ST-Link VCP, configure LED PA5, and print `GOOD` followed by a monotonically increasing counter repeatedly while toggling the LED. Export a volatile counter symbol and a non-inlined loop/tick function for debugger inspection.

Write correct startup/vector code, linker scripts with ASSERTs, Make targets `bootloader` and `application`, and map generation. Each helper invocation must use its own new build directory and produce exactly one ELF and one map (optional same-stem HEX). Use the exact general-helper command form returned by `get_setup_status`, with the appropriate project path/build path/target filled in.

FLASH/VERIFY:
- Collect each build through `collect_build_artifacts`.
- Flash bootloader first through `flash_application-plan`/`flash_application`; this test intentionally uses the reviewed full-flash application envelope and does not claim persistent protected-bootloader authority.
- Flash the offset application second through its own guarded application plan. Confirm containment before execution.
- Verify over COM12 using MCP serial tools that the bootloader transfers control and the application emits at least five ordered `GOOD <counter>` lines.
- Debug successfully through MCP: connect/halt, inspect PC/SP and the exported counter by symbol, set a breakpoint on the current application function through its plan, observe it hit, remove it, resume, and disconnect while leaving firmware running. If reconnect requires validation, perform it exactly as instructed.

Record a concise `journey.md` in this fresh repository containing model (`gpt-5.6-luna`), effort (`medium`), CLI version if observable, run ID, setup/validation result, exact general build commands/results, artifact paths/hashes, plan IDs, flash results, UART transcript, debug observations, and every failure/diagnosis/retest. Finish only when all required checks are green. In your final response summarize the evidence and exact files created.
