# nRF52833 DK Reference Firmware

This board package is the repo-owned baseline for `nrf52833dk`, the official
Nordic board in the current scoped Phase A / Phase B path.

This file is the single canonical runbook for the official Nordic closure
packet. The baseline source/build/artifacts exist in-repo, and the full Stage 0
truth path plus the tracked Stage 1 smoke harness are now bench-proven on live
`nrf52833dk` hardware.

## What Exists

- App root: `firmware/nrf52833dk/reference/src/`
- Canonical symbol-bearing artifact:
  `firmware/nrf52833dk/reference/build/firmware.elf`
- Preferred flash artifact:
  `firmware/nrf52833dk/reference/build/firmware.hex` if present, else
  `firmware.elf`
- Reference app behavior:
  - prints deterministic `boot ok`
  - stays alive
  - exposes the fixed Stage 1 symbol contract
    `stage1_known_value = 0x1234ABCD`

## Build Recipe

```bash
uv run pyocd-zephyr-build --app-dir firmware/nrf52833dk/reference/src --build-dir firmware/nrf52833dk/reference/build --board nrf52833dk/nrf52833
```

What that script does:

- uses the repo-owned `pyocd-zephyr-build` helper
- reuses an existing Zephyr workspace or SDK when one is already present
- otherwise bootstraps a managed upstream Zephyr workspace plus SDK in the
  local cache
- builds the app for `nrf52833dk/nrf52833`
- preserves the live Zephyr build tree inside `reference/build/` so repeated
  agent rebuilds stay fast and remain under the no-hang watchdog
- copies `zephyr/zephyr.elf` to the canonical `firmware.elf` name
- copies `zephyr/zephyr.hex` to `firmware.hex` when present

Validated toolchain note for this bench:

- this Intel macOS machine was validated with `zephyr-sdk-0.17.4`
- the current official Zephyr SDK line no longer ships macOS `x86_64` bundles,
  so Intel Macs need an older supported SDK release or another supported
  toolchain path

The legacy `build_reference.sh` / `build_bug.sh` wrappers still exist for bash
users, but they now delegate to the same `pyocd-zephyr-build` helper. `NCS` is
optional for this board package.

## Frozen Board Facts

- `board_id = nrf52833dk`
- `display_name = nRF52833 DK`
- `mcu_family = nrf52833`
- `probe_family = jlink`
- `pyocd_target = nrf52833`
- `pack_name = nrf52833`
- `serial_baudrate = 115200`
- `test_read_address = 0x10000000`
- `silicon_id_address = 0x10000100`
- `silicon_id_expected = 0x00052833`
- `silicon_id_label = FICR.INFO.PART`
- `reference_uart_patterns[0] = "boot ok"`
- `requires_recover_validation = true`
- `recover_mode = nrf_pyocd_unlock`
- symbol artifact =
  `firmware/nrf52833dk/reference/build/firmware.elf`
- flash artifact preference =
  `firmware/nrf52833dk/reference/build/firmware.hex` if present, else
  `firmware.elf`

Bench-proven host heuristics on the current Mac host:

- `probe_hint_terms = ["j-link", "jlink", "segger", "nrf", "52833"]`
- `serial_hint_terms = ["j-link", "jlink", "segger", "nrf", "52833", "virtual com"]`

## Official Nordic Closure Checklist

Run these in this exact order on the official `nrf52833dk` bench:

1. `./firmware/nrf52833dk/reference/build_reference.sh`
2. `uv run python host_bootstrap.py --board-id nrf52833dk`
3. `uv run python stage0_check.py --board-id nrf52833dk --reference-firmware nrf52833dk=firmware/nrf52833dk/reference/build/firmware.elf --recover-test nrf52833dk --confirm-shared-usb nrf52833dk`
4. `uv run python -m tests.harness.stage1_smoke --board-id nrf52833dk`

If serial auto-resolution is ambiguous, the rerun path is:

- `--port nrf52833dk=<port>`

The board config already declares the expected UART substring, so `--expect` is
not required for the normal reference-firmware path.

## Official Nordic Pass / Fail Checklist

Treat the official Nordic run as successful only if all of these are true:

- J-Link probe is visible.
- Target `nrf52833` is available.
- Silicon identity matches `0x00052833` at `0x10000100`.
- Stage 0 flashes the canonical reference artifact successfully.
- Stage 0 UART output contains `boot ok`.
- Stage 0 recover/unlock succeeds.
- The operator confirms the visible probe and COM port are the same physical
  board.
- The tracked Stage 1 smoke harness passes:
  - flash
  - `reset_and_halt`
  - `pc` read
  - symbol resolution
  - `0x1234ABCD` value readback
  - UART `boot ok`

## Recorded Bench Facts

Current official Mac bench facts:

- probe UID: `685400693`
- serial port: `/dev/cu.usbmodem0006854006931`
- pyOCD target: `nrf52833`
- pack token: `nrf52833`

Live proof results:

- `host_bootstrap.py --board-id nrf52833dk`
  - pass
- `stage0_check.py --board-id nrf52833dk --reference-firmware ... --recover-test nrf52833dk --confirm-shared-usb nrf52833dk`
  - pass
  - exact silicon identity matched `0x00052833`
  - flash passed with the canonical `firmware.elf`
  - UART matched `boot ok`
  - recover/unlock passed
- `uv run python -m tests.harness.stage1_smoke --board-id nrf52833dk`
  - pass
  - symbol-resolution proof passed for `stage1_known_value = 0x1234ABCD`

## Current Status

Verified on live hardware:

- the repo contains one canonical reference-firmware source tree and one
  repeatable build entrypoint for `nrf52833dk`
- the repo-owned reference app mirrors the intended minimal Nordic baseline
  pattern: deterministic `boot ok` UART plus the fixed Stage 1 symbol contract
  `stage1_known_value = 0x1234ABCD`
- the build helper completed on this Mac host and produced
  `firmware/nrf52833dk/reference/build/firmware.elf` plus `firmware.hex`
- the full official Nordic Stage 0 truth path is now green on the current Mac
  bench:
  - J-Link visibility
  - exact silicon identity
  - flash
  - UART
  - recover
  - shared USB confirmation
- the tracked Stage 1 smoke harness now passes on the official Nordic board
- the full current MCP tool surface has now been live-validated on the official
  Nordic board through `server.py`

Still useful future hardening:

- confirm the same official-board flow on a Windows host as a board-specific
  follow-up, even though repo-level Windows `R0` is already considered proven
