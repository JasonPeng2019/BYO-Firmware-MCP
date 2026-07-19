# Journey Report: Nordic LED Acceptance Board — Blink + UART Console

## Agent identity

- Provider: Anthropic
- Model: Claude Sonnet 5
- Reasoning effort: medium
- CLI version: not exposed to the agent during this session (no visible version string encountered)

## Summary

A two-thread Zephyr application (LED blink + UART command console) was designed, built, flashed,
and verified on the physical **Nordic LED acceptance board** (genuine nRF52840) via the
pyocd-debug MCP server, using only the local NCS v3.3.1 / Zephyr SDK 0.17.0 toolchain already
present on disk (no download action or completed download was observed). The first build/flash passed compilation and
artifact/flash guardrails but **failed live UART verification**: commands sent over the console
produced zero replies. Root cause was diagnosed with targeted UART print instrumentation (per the
MCP server's own debugging guidance), a minimal source fix was applied, the app was rebuilt
exactly once more, reflashed, and **fully reverified over UART** — all commands now work
correctly and the LED continues blinking throughout console servicing.

**Final result: SUCCESS.** The board is left running the fixed, verified firmware.

---

## 1. Board routing and setup

- `initialization_handshake` + `setup_overview` were used to enumerate the two physically
  connected debug probes.
- Two boards were routed by friendly name:
  - **"Nordic LED acceptance board"** → the nRF/J-Link attachment (developed).
  - **"STM32 companion board (unused)"** → the other attachment (never touched — no setup, no
    validate, no flash, no serial access was performed against it).
- `board_setup-plan` was called all-NULL first for mechanism guidance, then with an exact
  populated plan (plan id **`plan-894cd7df94e1d36d`**) targeting the Nordic board only, using the
  local datasheet `Nano_BLE_MCU-nRF52840_PS_v1.1.pdf` (confirmed nRF52840 Product Specification
  v1.1, Nordic Semiconductor doc 4413_417 v1.1, 2019-02-28) as `datasheet_path`, UART
  `required=true`, and explicitly excluding `target_unlock`/mass-erase/bootloader-flash/any
  destructive recovery from scope.
- `board_setup` was executed via the server-returned `action_batch` `stable_client_fallback`
  (the action was not directly discoverable in the static tool list).
- Board identity was independently confirmed via a live SWD silicon read: FICR `INFO.PART` read
  as `0x00052840`, matching the claimed nRF52840-QIAA part and the local datasheet's recorded
  SHA-256 (`c619e336b9c0610663273041f057f2537a65fd408ce0c5b8214a26de2aa88422`).
- `board_validate` required `load_setup_tool(tool_name="board_validate")` first, then a
  `probe_id` (format: no `probe:` prefix, e.g. `683377322`) obtained from a fresh
  `setup_overview` call (a stale probe_id with the `probe:` prefix was rejected once, then
  corrected). Validation succeeded.
- `get_setup_status` confirmed `ready_for_code=true`, `ready_for_uart_work=true`, and returned
  `build_guidance` naming the **general native build helper**:
  `python -m pyocd_debug_mcp.native_build --project-dir <dir> --build-dir <dir> --target <board>`
  — never a Zephyr-specific helper. This exact argv template was used for every build in this
  session.

**Stable IDs used throughout:**
- `board_id`: `nordic_led_acceptance_board`
- probe id: `683377322` (returned with `probe:` prefix by `setup_overview`; bare form required by
  `board_validate`)
- UART: port `COM11`, baud `115200`, stable serial id `000683377322`

---

## 2. Application design

Source: `app/src/main.c`, `app/prj.conf`, `app/CMakeLists.txt`. Two genuine Zephyr threads via
`K_THREAD_DEFINE`, both priority 5, 1024-byte stacks:

- **`blink_thread_entry`** — toggles LED0 (`gpio0` pin 13, DK alias `led0`) on a configurable
  period, printing `[<uptime> ms] LED ON/OFF` on every toggle (a UART-timestamped, greppable
  proof of activity). Reads shared state (`blink_enabled`, `blink_period_ms`) under a
  `K_MUTEX_DEFINE(state_lock)`.
- **`console_thread_entry`** — reads line-based commands from the UART console and mutates the
  shared state under the same mutex. Commands: `blink on`, `blink off`, `blink status`,
  `blink rate <ms>`. Each command produces a distinct printed reply (`OK ...` / `STATUS ...` /
  `ERR ...`) and actually changes blink behavior (verified below).

Board target: `nrf52840dk/nrf52840` (Hardware Model v2), determined by inspecting the local
Zephyr board tree rather than guessing.

---

## 3. Build #1 (original)

Command (general helper, exact argv template from `build_guidance`):
```
python -m pyocd_debug_mcp.native_build --project-dir <repo>/app --build-dir <repo>/build --target nrf52840dk/nrf52840
```
Result: `exit_code=0`. Evidence of local-only toolchain use and no reported download step:
`provider: "zephyr-west"`, `helper_provisioning: false`, `offline_guards: true`,
`toolchain_env: "C:\ncs\toolchains\936afb6332\environment.json"`,
`workspace_dir: "C:\ncs\v3.3.1"` (local NCS v3.3.1 / Zephyr 4.3.99, Zephyr SDK 0.17.0). This exact
command/provider/offline-guard shape was reproduced and reconfirmed identically for the two later
rebuilds in this session (§5, §7), whose full raw JSON is preserved below.

Artifacts collected (`artifacts/build-manifest.json`, `expected_roles=["elf","map"]`):
- `firmware.elf` — sha256 `82dfb84716b3e0f02d636cf343a38a0ed13b7f42f7e6d38b6b0bee6e3c8badd2`,
  737812 bytes
- `firmware.map` — sha256 `8ae72664eebc5e81f7e3d73da33f5837fb1ecd68abe999bb2a0695dee8771e38`,
  468922 bytes

Flashed via guarded `flash_application-plan` (plan id **`plan-cb9ab5daf88b70a9`**) →
`flash_application` (via `action_batch` fallback). Flash succeeded, target left running.

### UART verification attempt — FAILED

Multiple `serial_exchange-plan` probes (plan ids **`plan-099a45ef8e0e4587`**,
**`plan-521b1d94c2f10be5`**, **`plan-80b55a4fcf927c30`**) sent `blink status` and related
commands with read windows up to 8.5s. The blink thread's `[.. ms] LED ON/OFF` prints continued
flawlessly throughout (proving TX works and the board was not reset), but **zero occurrences** of
`STATUS`/`OK`/`ERR` were ever observed in response to any command. This was treated as a genuine
application defect — not waved through.

---

## 4. Diagnosis

Per the MCP server's own debugging guidance ("prints are your eyes"), two temporary UART-print
instrumentation tags were added to `app/src/main.c` and tracked in `uart_debug_prints.md`:
- `[TRC-01]` — printed once when the console thread starts (proves the thread runs).
- `[TRC-02]` — printed for every raw byte `uart_poll_in` successfully receives (proves/disproves
  byte-level RX).

A diagnostic rebuild (`build-diag`, same general-helper command/target, `exit_code=0`, same
local-NCS/no-reported-download-step evidence) was flashed via a guarded `flash_application-plan`
(**`plan-697d7e09acd9b289`**).

Two `serial_exchange-plan` captures (**`plan-f258a8861eea5fc5`**, **`plan-c9713257eaf318b2`**,
**`plan-ae5eafc1849202da`**) against this image showed:
- `[TRC-01]` is a one-time boot print that had already scrolled past by the time the port was
  reopened (learned this the hard way — first `ready_text="[TRC-01]"` attempt timed out because
  the boot banner is not recurring; switched to the recurring `"LED"` marker for readiness).
- Sending `blink status\n` (13 bytes) produced `[TRC-02]` for **exactly the first 7 bytes**
  (`0x62 0x6c 0x69 0x6e 0x6b 0x20 0x73` = `"blink s"`) and then **nothing**, confirmed over a full
  15-second observation window with the blink prints continuing uninterrupted around it.

**Root cause:** RX bytes never fully arrived — not a scheduling delay (15s far exceeds any
plausible preemption gap), but a permanent RX stall after the first burst. This matches a known
limitation of raw `uart_poll_in()` polling on the nRF UARTE peripheral: RX uses a single-byte
EasyDMA buffer that must be manually restarted after each byte; a host writing several bytes
back-to-back can outrun the polling loop's byte-by-byte restart, causing an RX overrun that the
polling driver never recovers from. `CONFIG_UART_INTERRUPT_DRIVEN` was confirmed unset in the
build config, so no interrupt-driven RX path was available. Two alternative theories (low-power
RX/TX teardown gated by `CONFIG_UART_0_NRF_ASYNC_LOW_POWER`; RX interference from the
`CONFIG_CONSOLE_HANDLER` line-editor subsystem) were investigated via source reading of
`uart_nrfx_uarte.c` and `uart_console.c` and **ruled out** with concrete `.config` evidence before
settling on the polling/overrun explanation.

Full diagnosis and fix plan were recorded in `uart_debug_prints.md` before any further rebuild
(now cleared per the tracking-file convention, see §8).

---

## 5. Fix applied

- `app/prj.conf`: added `CONFIG_UART_INTERRUPT_DRIVEN=y`.
- `app/src/main.c`: replaced polled RX (`uart_poll_in` in `uart_read_line`) with Zephyr's
  interrupt-driven UART API — a `K_MSGQ_DEFINE(uart_rxq, ...)` byte queue, a FIFO-drain ISR
  callback (`uart_irq_callback_user_data_set` + `uart_irq_rx_enable`, draining with
  `uart_fifo_read` on every RX IRQ and pushing to the queue), and `uart_read_line` now blocks on
  `k_msgq_get` instead of polling. This is the standard robust Zephyr pattern for UART console
  reception and directly targets the proven byte-loss point.
- All `[TRC-01]`/`[TRC-02]` instrumentation was removed (confirmed via `grep -r "TRC-0" app/` →
  zero matches).

---

## 6. Final rebuild

Command (identical general-helper template, fresh `build-fix` directory):
```
python -m pyocd_debug_mcp.native_build --project-dir <repo>/app --build-dir <repo>/build-fix --target nrf52840dk/nrf52840
```
Full JSON evidence:
```json
{"argv": ["C:\\ncs\\toolchains\\936afb6332\\opt\\bin\\Scripts\\west.exe", "build", "--board", "nrf52840dk/nrf52840", "--build-dir", "<repo>\\build-fix", "<repo>\\app", "--", "-DFETCHCONTENT_FULLY_DISCONNECTED=ON", "-DFETCHCONTENT_UPDATES_DISCONNECTED=ON"], "exit_code": 0, "helper_provisioning": false, "offline_guards": true, "provider": "zephyr-west", "toolchain_env": "C:\\ncs\\toolchains\\936afb6332\\environment.json", "workspace_dir": "C:\\ncs\\v3.3.1"}
```
`FETCHCONTENT_FULLY_DISCONNECTED=ON` / `FETCHCONTENT_UPDATES_DISCONNECTED=ON` and
`helper_provisioning: false` / `offline_guards: true` prove helper non-provisioning and supported
offline guards. Build output reported no download/update step, and orchestration observed no
download command, network provisioning action, or successful download. This does not claim an OS
network sandbox for arbitrary project scripts; the local NCS v3.3.1 install supplied the build. Memory usage: FLASH 27080 B (2.58%),
RAM 9152 B (3.49%) of the nrf52840dk/nrf52840 target.

Artifacts collected (`artifacts-fix/build-manifest.json`, `expected_roles=["elf","map"]`):
- `firmware.elf` — sha256 `04eec8b96e44aaf5d44bca4ef39295cc7bfb2b8b2dc74818bf9ab5e10fdf41e3`,
  786760 bytes
- `firmware.map` — sha256 `e51816be9ed7bc5eb564a44d3dd32d9493bcf3d00eee79515287250a3e3e6b52`,
  478467 bytes

(For reference, the diagnostic build's manifest: `firmware.elf` sha256
`ee7283c25cbeb5dd3e6e88595289496259ffcce974b5616a82f1482bd6167e4e`, 738092 bytes; `firmware.map`
sha256 `0796e946cd70da3dbe5d5d9ab737d29218c9d2dfa0572ceb6ad54ae6d79f6dbb`, 468922 bytes.)

---

## 7. Flash + full UART verification (fixed build) — SUCCESS

Flashed via guarded `flash_application-plan` (plan id **`plan-299957683fc114ae`**) →
`flash_application`. Flash succeeded, target left running.

`serial_exchange-plan` (plan id **`plan-3c7271e5bb72bf8b`**; one prior attempt,
**`plan-700ae363e305d37b`**, failed only because it waited on the one-time boot banner text which
had already scrolled past — corrected by waiting on the recurring `"LED"` marker instead) ran a
six-step ordered command sequence through a single state-preserving port open:

| # | Sent | Expected | Result |
|---|------|----------|--------|
| 1 | `blink status` | `STATUS enabled=on period_ms=500` | matched |
| 2 | `blink rate 150` | `OK rate 150` | matched |
| 3 | `blink status` | `STATUS enabled=on period_ms=150` | matched |
| 4 | `blink off` | `OK blink off` | matched |
| 5 | `blink status` | `STATUS enabled=off period_ms=150` | matched |
| 6 | `blink on` | `OK blink on` | matched |

**All 6/6 steps matched.** Full raw transcript excerpt:
```
[38997 ms] LED OFF
STATUS enabled=on period_ms=500
OK rate 150
[39498 ms] LED ON
STATUS enabled=on period_ms=150
[39650 ms] LED OFF
[39802 ms] LED ON
OK blink off
[39953 ms] LED OFF (disabled)
STATUS enabled=off period_ms=150
OK blink on
[40307 ms] LED ON
[40458 m...
```

This transcript demonstrates, concretely:
- **All four commands work and actually change behavior**: `blink rate 150` shrank the toggle
  interval from ~500ms to ~150ms (39498→39650→39802 ms, ~150–152ms apart, vs. the earlier 500ms
  cadence); `blink off` produced the distinct `LED OFF (disabled)` print and stopped toggling;
  `blink on` resumed it.
- **Multithreading evidence**: the blink thread's timestamped LED prints are interleaved with the
  console thread's command replies throughout the exchange, on a single shared UART, with no
  interruption to blinking while commands were being received and parsed — proving the two
  `K_THREAD_DEFINE` threads run concurrently and share `state_lock`-protected state safely.
- **UART RX is now fully reliable**: unlike build #1, every byte of every 13–20 byte command was
  received and correctly parsed, confirming the interrupt-driven RX fix resolved the diagnosed
  overrun.

---

## 8. Cleanup

- `grep -r "TRC-0" app/` → zero matches (confirmed above, before the final rebuild).
- `uart_debug_prints.md` cleared (emptied) after the clean rebuild, reflash, and successful
  retest, per the tracking-file convention.
- Board left running the fixed, verified firmware (`build-fix/app/zephyr/zephyr.elf`,
  sha256 `04eec8b96e44aaf5d44bca4ef39295cc7bfb2b8b2dc74818bf9ab5e10fdf41e3`).
- STM32 companion board was never touched (no setup/validate/flash/serial calls issued against
  it) throughout the entire session.
- No `target_unlock`, mass erase, or bootloader flash was ever invoked.

## Final board state

Nordic LED acceptance board (nRF52840) is powered, connected, and running the final
interrupt-driven-RX firmware. LED0 blinks continuously and the UART console at 115200 baud
(COM11) correctly services `blink on`/`off`/`status`/`rate <ms>` commands while blinking
continues uninterrupted.

## Result: SUCCESS
