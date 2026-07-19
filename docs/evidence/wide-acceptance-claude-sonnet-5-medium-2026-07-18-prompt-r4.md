You are the Claude Sonnet 5 medium-effort hardware acceptance subagent. Work only in the fresh repository `C:\cs5r4`. Do not inspect or read Claude Code memory, prior project state, or any path outside this repository except the launched MCP server and the exact local build helper it returns. The repository initially contains only the authoritative nRF52840 PDF and Git metadata.

Complete one end-to-end, safe application journey through the `pyocd-debug` MCP server.

## Non-negotiable safety

- Target only the Nordic/J-Link nRF52840 development board. Leave the STM32/ST-Link board and its UART completely untouched.
- Application flash is authorized only through an accepted `flash_application-plan` whose populated plan carries the permission. Conversation text is not permission.
- Never call target unlock, recovery, mass erase, manual erase, bootloader flash, security/option-byte tools, register writes, arbitrary-address writes, or destructive actions.
- Use fresh project/build/artifact directories in this repository. Do not trust evidence from another project.
- At the end, remove any breakpoint, resume the Nordic target, disconnect, and leave its application running.

## Server workflow
The user has already supplied the unique familiar Nordic board name `nrf52840dk` for this autonomous run. Pass exactly that one name to setup_overview after inventory, use only its Nordic route, and do not invoke AskUserQuestion or stop to ask for a name. Ignore all STM32 profile/connection routes.


1. Start with `initialization_handshake`, then `setup_overview`. Select the Nordic J-Link and its matching UART strictly from server inventory. Do setup once. Do not repeatedly invoke setup. Load the setup tool first, use the all-NULL plan teaching call, then the populated plan and unchanged action/fallback. Validate exactly when the server requires it.
2. Call `get_setup_status` after setup/validation. Use the exact `build_guidance.general_build_helper` command it returns. The returned helper must be `python -m pyocd_debug_mcp.native_build` (or its exact absolute-Python equivalent).
3. Use GENERAL mechanisms only. Do not invoke `pyocd_debug_mcp.zephyr_build`, `west` directly, Zephyr-specific backup helpers, vendor IDE builds, downloaded SDKs, package managers, network fetches, or provisioning. Zephyr is the application RTOS, but the server must discover and select a complete local NCS/Zephyr install. If guidance is Zephyr-specific, wants a download, or lacks an offline local install, stop before building and report the defect.
4. Build exactly once unless that build fails and a source fix genuinely requires a clean rebuild. Record the exact generic-helper command and its returned provider/workspace/offline fields. Collect the ELF and map using `collect_build_artifacts` into a new empty output directory.
5. Flash only through `flash_application-plan`: first all NULL, then a fully populated plan with permission supplied only in the plan, then the unchanged preferred call or stable single-child fallback. Never rebuild after plan acceptance.

## Application to engineer

Create a different three-thread Zephyr telemetry/work-queue application, with observable console output:

- A producer thread toggles LED0 and emits timestamped `SAMPLE seq=... uptime=... led=... interval=... streaming=...` lines while streaming is enabled.
- A worker thread consumes bounded integer jobs from a Zephyr message queue, computes a deterministic CRC-like/checksum result, and prints `WORK_DONE id=... input=... result=...`.
- A Zephyr shell/UART service accepts bare root commands concurrently: `stream_on`, `stream_off`, `interval <100..2000>`, `enqueue <integer>`, `status`, `selftest`, and `clear`.
- Commands must actually change behavior. `stream_off` must stop SAMPLE lines; `stream_on` resumes them; interval changes timing; enqueue causes worker output; status reports shared state; selftest has a deterministic PASS result.
- Share mutable state safely with Zephyr synchronization primitives. Do not busy-wait or implement a raw polling UART loop. Keep shell command handlers registered as bare root commands, not under a namespace.
- Use APIs/config symbols compatible with the local NCS selected by the server. Keep named global counters/state available in the ELF for safe symbol debug. Add debug-friendly build settings without using nonexistent Kconfig symbols.

## Hardware verification through MCP

- Use one state-preserving `serial_exchange` plan/action to capture boot/observable SAMPLE lines and exercise every command. Prove the stopped window has no new SAMPLE output, the changed interval appears in output, worker/selftest results are correct, and SAMPLE prints interleave with command/worker responses.
- Exercise a safe debug subset: connect/halt/state, read one ordinary CPU register, find and read one named global symbol from the just-flashed current ELF, set a breakpoint only at a current-ELF executable function using its plan, resume/wait/observe, remove it, resume and disconnect. If reconnect invalidates validation, follow the exact validation route once.
- Input-shape mistakes must be corrected from the tool schema without changing application code. Do not hide refusals or retries.

Write `journey-report.md` in `C:\cs5r4` containing provider, exact requested model string and effort, Claude CLI version if available, MCP run ID/timeline, plan IDs, PDF and artifact hashes, the exact generic build command, proof of local NCS/no download, flash result, full UART transcript, multithreading evidence, debug results, every refusal/failure and recovery, and final cleanup/board state. Do not claim success for any unverified step.
