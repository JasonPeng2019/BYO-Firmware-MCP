# Journey: nRF52840-DK freestanding bootloader-style firmware — build, flash, UART proof, live debug

## Agent / run identity
- Model: claude-sonnet-5
- Reasoning effort: medium (40)
- MCP server run identity at start of this journey: `run_id=run-20260720T020328Z-e3456f57`,
  `started_at=2026-07-20T02:03:28.660660Z` (in-memory server run identity; changes on
  server restart; grants no authority by itself — every hardware action still went
  through the guarded `*-plan` → accept → `action_batch` fallback pattern).
- Board: nRF52840-DK, onboard J-Link, probe UID `683377322`, stable USB identity
  `000683377322`, UART on COM11 @ 115200 baud. MCU: nRF52840-QIAA (identified from
  datasheet.pdf ordering-info pages 613-616). pyOCD target: `nrf52840_xxaa`.
  Server-assigned `board_id`: `nrf52840dk_2` (display name `nrf52840dk`).

---

## 1. Setup

1. `initialization_handshake` → `setup_overview` (no board_names) → asked which probe
   belonged to `nrf52840dk` → re-called `setup_overview` with
   `connection_assignments={"nrf52840dk": "probe:683377322"}`.
2. `board_setup-plan` (all-NULL → populated, **plan `plan-9fe0124162900073`**) for an
   unknown MCU part (`nRF52840-QIAA`) returned `setup_research_required`: pyOCD needed
   verified official CMSIS-Pack evidence, not just a built-in target-string match.
   - **Local-first check**: `%LOCALAPPDATA%\cmsis-pack-manager\cmsis-pack-manager\`
     had a cached `.pdsc` descriptor confirming the exact device name
     (`nRF52840_xxAA`, vendor `Nordic Semiconductor:54`), but **no full `.pack` file**.
   - **First continue_setup attempt failed**: submitted the cached `.pdsc` as evidence
     → rejected: *"Package filename must be a plain .pack filename."*
   - Explicitly excluded `/tmp/byo-*/` and the BYO-Server checkout from the search
     even though matching files might exist there — out of scope for this task.
   - Fell back to a network download of the official pack from Nordic's own
     redirect target (developer.nordicsemi.com → files.nordicsemi.com/artifactory),
     saved to `.firm/packs/NordicSemiconductor.nRF_DeviceFamilyPack.8.44.1.pack`
     (sha256 `95136b57a8310ba367aa46cb66c8d149561fc7876c5f3736f323a9dbfb8f559e`,
     2,218,801 bytes).
   - Resubmitted `continue_setup` with the real `.pack` → accepted
     (`"accepted": "target_and_pack"`).
   - Called `board_fix_setup` (via its `action_batch` fallback) to complete setup
     under the paired allowance.
3. `board_validate` (after `load_setup_tool(board_id, "board_validate")`, since the
   first direct call returned `setup_tool_not_loaded`) → `validation_passed`.
   Note: `probe_id` format is bare `"683377322"`, not `"probe:683377322"`, when
   calling `board_validate` directly (differs from the `connection_assignments` form).

---

## 2. Firmware

Freestanding C, no RTOS/SDK/HAL, direct register access only:
- `src/startup.c` — 16 core + 48 external-IRQ vector table in `.isr_vector`;
  `Reset_Handler` copies `.data`, zeroes `.bss`, calls `main()`; weak-aliased fault
  handlers; shared `Default_Handler`/`IRQ_Handler`.
- `src/main.c` — direct UART0 (base `0x40002000`) and GPIO P0 (base `0x50000000`)
  register macros. `uart_init` (PSEL.TXD=P0.06, PSEL.RXD disconnected, 115200 baud,
  CONFIG=0, ENABLE=4). `led_init/on/off` on P0.13 (active-low, LED1). Main loop:
  print `"BOOT OK\r\n"`, `boot_count++`, LED on, delay, LED off, delay,
  `blink_count++`. Exported non-static globals for live-debug: `boot_count`
  (RAM 0x20000004) and `blink_count` (RAM 0x20000000).
- `linker/linker.ld` — FLASH (rx) 0x00000000/1024K, RAM (rwx) 0x20000000/256K;
  `.isr_vector`→FLASH, `.text`/`.rodata`→FLASH, `.data` RAM AT>FLASH, `.bss` RAM.
- `Makefile` — GNU Make + `arm-none-eabi-gcc`, provider `gnu-make` for the general
  native-build helper.

---

## 3. Build

Helper: `C:\...\BYO-Server\.venv\Scripts\python.exe -m pyocd_debug_mcp.native_build
--project-dir <dir> --build-dir <dir> --target <target>` (the general,
provider-neutral helper — explicitly **not** the Zephyr-specific helper that the
environment's `resolved_local_environments` listed as default). It auto-detects
`gnu-make` from the Makefile and invokes `make -C <project-dir> BUILD_DIR=<build-dir>
<target>`.

**Failure 1 — wrong compiler invoked.** First build run invoked literal `cc`, not
`arm-none-eabi-gcc`: `make: *** [Makefile:35: build/startup.o] Error -1`.
Diagnosis: `CC ?= arm-none-eabi-gcc` does **not** override GNU Make's built-in
default `CC=cc`, because that built-in has origin `"default"` and `?=` only sets a
variable that is completely unset. **Fix:** changed to unconditional
`CC := arm-none-eabi-gcc` (same for `OBJCOPY`/`SIZE`).

**Failure 2 — wrong build-dir variable name.** Inspecting the helper's actual argv
showed it passes `BUILD_DIR=<path>` (underscore), not the `BUILDDIR` the Makefile
originally defined. **Fix:** `BUILD_DIR ?= build` with `BUILDDIR := $(BUILD_DIR)`
so the helper's variable is consumed correctly.

Build then succeeded (provider `gnu-make`, exit_code 0). Toolchain reused from an
existing local STM32CubeIDE ARM GCC install (local-first policy — no download).
Size: text=548, data=0, bss=8, dec=556.

Symbol export verified with `arm-none-eabi-nm`: `blink_count @ 0x20000000`,
`boot_count @ 0x20000004`.

---

## 4. Artifact collection

**Failure — refused output_dir.** First `collect_build_artifacts` call used
`output_dir=".firm/..."` → refused: *"Artifact bundles must not be written inside
the FirmStore tree."* **Fix:** used a plain repo subdirectory, `repo/artifacts`.

First collection (`repo/artifacts/`):
| artifact | sha256 | size |
|---|---|---|
| firmware.elf | `d5ed6625cf2eb7f0065a251f2a098673579de6ce3a578155ccb31d82378d32d7` | 9964 B |
| firmware.hex | `d98672387979eb0575b83e605ee7259cf648dcbe61335ea7ca3d19e931804a60` | 1585 B |
| firmware.bin | `7dd11012dca944f8e64e92dd8775f806ea5efca1e629ab4bbe6b640a312df0dd` | 548 B |
| firmware.map | `9c6e6063f3152ec7f7560a3d8bf801d2270084fb82515bd525fa88a966c238f1` | 8362 B |

(`build-manifest.json` written alongside.)

---

## 5. Flash (first, application-region only)

`flash_application-plan` (all-NULL → populated, **plan `plan-75f6bd125c507ae7`**).
`action_parameters` = `{"artifact": "artifacts/firmware.elf"}` only — no target
address (server derives placement from the artifact's own load addresses against
the application-partition safety map), no `user_permission` field (omitted
entirely — including it even as null makes the plan malformed).
Real action not in visible tool bindings → used the exact
`stable_client_fallback` `action_batch` call unchanged.
Result: *"Flashed ... as flash_application within its mapped partition; target
left running."* Session ended there per the original task (build+flash only, no
UART/live-debug proof yet).

---

## 6. UART proof (next journey turn)

Attempted `read_serial-plan` (all-NULL → populated, **plan `plan-99d348bb93b592db`**,
`read_seconds=16`, `reset_on_open=false`). Execution via `action_batch` **failed**:
*"Board 'nrf52840dk_2' has no active connection; reconnect and submit a new plan."*

**Diagnosis:** the debug/UART session does not persist indefinitely between MCP
tool-call turns; this is expected server behavior across process/session
boundaries, not a firmware fault. **Fix / expected pattern established here:**
`connect(board_id)` → re-run `setup_overview` with the connection assignment →
`load_setup_tool(board_id, "board_validate")` → `board_validate` (category-2
validation trigger: "connection identity change after ... reconnect") →
`validation_passed` → submit a **fresh** `read_serial-plan` (plans are
single-shot/immutable; a stale plan cannot be reused after the underlying
connection was invalidated).

Retried with a new plan (**plan `plan-18ca1a6042db490a`**, same parameters) →
succeeded:

```
UART matched on COM11 at 115200 baud via pyocd-native; expected=(none);
reopen_count=0; duration=16.02s
```

Captured excerpt (verbatim, 28 complete lines + one truncated tail line):
```
BOOT OK\r\nBOOT OK\r\nBOOT OK\r\nBOOT OK\r\nBOOT OK\r\nBOOT OK\r\nBOOT OK\r\nBOOT OK\r\n
BOOT OK\r\nBOOT OK\r\nBOOT OK\r\nBOOT OK\r\nBOOT OK\r\nBOOT OK\r\nBOOT OK\r\nBOOT OK\r\n
BOOT OK\r\nBOOT OK\r\nBOOT OK\r\nBOOT OK\r\nBOOT OK\r\nBOOT OK\r\nBOOT OK\r\nBOOT OK\r\n
BOOT OK\r\nBOOT OK\r\nBOOT OK\r\nBOO
```
MCP-measured duration: **16.02 s**. `reopen_count=0` — no reset was triggered
during capture. Board left connected/running, no disconnect issued.

---

## 7. Live debug (this journey turn)

### 7a. Reconnect / re-validate (expected, not a bug)
`halt` failed: *"Board 'nrf52840dk_2' is not connected."* Same expected pattern as
§6: `connect` → `setup_overview` (re-assign `probe:683377322`) →
`load_setup_tool`/`board_validate` → `validation_passed`. This reconnect/validate
cycle happened **twice** more later in this turn for the same reason (each gap in
active tool calls appears to let the underlying debug session lapse) — each time
resolved the same way.

### 7b. Missing canonical symbol artifact
`read_cpu_register` for `pc`/`sp` was refused: *"Register 'pc'/'sp' is not in the
ordinary CPU/FP register class"* — fixed by using `read_execution_state` instead
(the correct tool for PC/SP/control-flow registers).

`read_memory_symbol`/`find_symbol` were refused: *"Missing canonical symbol
artifact for nrf52840dk_2:
C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\firmware\nrf52840dk_2\reference\build\firmware.elf"*.

**Diagnosis:** the server's canonical ELF binding used for symbol resolution had
been lost (consistent with a server-side session/connection reset between turns,
same underlying cause as needing to reconnect). Collecting build artifacts again
alone was **not** sufficient to restore it — a fresh `collect_build_artifacts`
call (`output_dir=artifacts2`, byte-identical outputs, same sha256 as §4:
`firmware.elf d5ed6625...32d7`, `firmware.bin 7dd11012...df0dd`,
`firmware.hex d98672387...4a60`, `firmware.map 9c6e6063...38f1`) still left
`read_memory_symbol` refused with the same error.

**Fix:** re-ran the guarded `flash_application-plan` (all-NULL → populated,
**plan `plan-8917cf20c277b889`**, `action_parameters={"artifact":
"artifacts2/firmware.elf"}`) — a byte-identical reflash of the running firmware,
purely to re-bind the canonical ELF for symbol resolution. Result: *"Flashed ...
target left running."* After this, `read_memory_symbol`/`find_symbol` worked.

### 7c. Register and counter reads
After the rebind reflash, halted and read:
- `PC = 0x000001CE` — inside `main()` (`main@0x00000161`, size 184 → range
  0x161–0x219). **Plausible**: core executing inside the firmware's own main loop.
- `SP = 0x2003FFE8` — 0x18 bytes below the RAM top (`_estack = 0x20040000`).
  **Plausible**: consistent with a small amount of stacked context at halt.
- `boot_count = 18` (0x12), `blink_count = 17` (0x11) — differ by exactly 1,
  consistent with halting between the `boot_count++` and `blink_count++`
  statements in the same loop iteration. **Plausible, no anomaly** — treated as
  live proof of correct execution rather than quoted blindly.

`find_symbol` for `led`/`uart`/`delay` all returned no matches — expected, since
`-O2` inlined `uart_init`/`uart_put_string`/`led_on`/`led_off`/`delay_cycles`
into `main()`; only `main` and `Reset_Handler` remain as standalone function
symbols. This is why `main()` itself (the function containing the entire
boot-print/blink loop) was chosen as the breakpoint target.

### 7d. Breakpoint — failed then successful ordering

**Attempt 1 (failed):** `set_breakpoint-plan` (**plan `plan-466db6377742b978`**,
address `main`/0x161) accepted and armed while core was halted mid-loop, then
`reset_and_run`. After reconnect, `get_state` reported **RUNNING**, never HALTED
at 0x161.

**Attempt 2 (failed, tighter timing):** new plan (**plan `plan-ffeed3ec90aff3e9`**)
combined `set_breakpoint` + `reset_and_run` in a single `action_batch` call (to
rule out an inter-call session-teardown race) — still resumed straight to
**RUNNING**, breakpoint never hit.

**Diagnosis:** a system reset (`reset_and_run`) clears the Cortex-M4 FPB
comparator registers. A breakpoint programmed *before* a reset is wiped by the
reset itself, so it can never trap code that only starts executing *after* that
reset. Plain `resume` from mid-loop also could not work as a fix, since `main()`'s
entry is only reached once (at boot) and the loop never branches back to the
function entry.

**Fix — correct ordering:** `reset_and_halt-plan` (all-NULL → populated,
**plan `plan-2a70db7accc5712b`**, halts the core at the reset vector *before*
`Reset_Handler` runs) → core halted → **then** `set_breakpoint-plan`
(**plan `plan-08c0ca3f83b15496`**, `main`/0x161) programmed while halted →
`resume`. Comparator now survives (no further reset intervenes) and traps
execution the moment `Reset_Handler` calls `main()`.

**Result:** `get_state` → **HALTED**; `read_execution_state("pc")` →
`0x00000160` — exact match for `main`'s Thumb entry (symbol address 0x161 with
the Thumb mode bit stripped). Breakpoint hit confirmed.

### 7e. Cleanup
`remove_breakpoint(address=0x161)` → *"Breakpoint removed at 0x00000161."*
`resume` → *"Resumed."*
`get_state` → **RUNNING** (final confirmed state; board left connected and
running, no disconnect issued).

---

## 8. Plan-ID index

| Step | Plan ID |
|---|---|
| board_setup | `plan-9fe0124162900073` |
| flash_application (initial) | `plan-75f6bd125c507ae7` |
| read_serial (failed, stale connection) | `plan-99d348bb93b592db` |
| read_serial (succeeded, 16.02s) | `plan-18ca1a6042db490a` |
| flash_application (rebind, byte-identical) | `plan-8917cf20c277b889` |
| set_breakpoint (failed, armed pre-reset) | `plan-466db6377742b978` |
| set_breakpoint (failed, armed pre-reset, batched) | `plan-ffeed3ec90aff3e9` |
| reset_and_halt (fix) | `plan-2a70db7accc5712b` |
| set_breakpoint (succeeded, armed post-reset-halt) | `plan-08c0ca3f83b15496` |

## 9. Artifact hash index (identical bytes both collections)

| artifact | sha256 | size |
|---|---|---|
| firmware.elf | `d5ed6625cf2eb7f0065a251f2a098673579de6ce3a578155ccb31d82378d32d7` | 9964 B |
| firmware.hex | `d98672387979eb0575b83e605ee7259cf648dcbe61335ea7ca3d19e931804a60` | 1585 B |
| firmware.bin | `7dd11012dca944f8e64e92dd8775f806ea5efca1e629ab4bbe6b640a312df0dd` | 548 B |
| firmware.map | `9c6e6063f3152ec7f7560a3d8bf801d2270084fb82515bd525fa88a966c238f1` | 8362 B |
| CMSIS pack | `95136b57a8310ba367aa46cb66c8d149561fc7876c5f3736f323a9dbfb8f559e` | 2218801 B |

## 10. Final state
Board `nrf52840dk_2` connected, validated, **RUNNING** the flashed application
firmware (`artifacts2/firmware.elf`), no breakpoints armed, no disconnect issued.
