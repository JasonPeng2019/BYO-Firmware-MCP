# nRF52840 DK Reference Firmware

This board package is the repo-owned baseline for `nrf52840dk`, retained as an
alternate / future Nordic profile.

It is no longer the official blocker for the current scoped Phase A / Phase B
path. The active scoped Nordic board is `nrf52833dk`. Keep this package because
it already captures a useful alternate Nordic baseline and may become relevant
again if future support for `nrf52840dk` is desired.

## What Exists

- App root: `firmware/nrf52840dk/reference/src/`
- Canonical symbol-bearing artifact:
  `firmware/nrf52840dk/reference/build/firmware.elf`
- Preferred flash artifact:
  `firmware/nrf52840dk/reference/build/firmware.hex` if present, else
  `firmware.elf`
- Reference app behavior:
  - prints deterministic `boot ok`
  - stays alive
  - exposes the fixed Stage 1 symbol contract
    `stage1_known_value = 0x1234ABCD`

## Build Recipe

```bash
uv run pyocd-zephyr-build --app-dir firmware/nrf52840dk/reference/src --build-dir firmware/nrf52840dk/reference/build --board nrf52840dk/nrf52840
```

What that script does:

- uses the repo-owned `pyocd-zephyr-build` helper
- reuses an existing Zephyr workspace or SDK when one is already present
- otherwise bootstraps a managed upstream Zephyr workspace plus SDK in the
  local cache
- builds the app for `nrf52840dk/nrf52840`
- preserves the live Zephyr build tree inside `reference/build/` so repeated
  agent rebuilds stay fast and remain under the no-hang watchdog
- copies `zephyr/zephyr.elf` to the canonical `firmware.elf` name
- copies `zephyr/zephyr.hex` to `firmware.hex` when present

Validated toolchain note for this bench:

- this Intel macOS machine was validated with `zephyr-sdk-0.17.4`
- the current official Zephyr SDK line no longer ships macOS `x86_64` bundles,
  so Intel Macs need an older supported SDK release or another supported
  toolchain path

## Frozen Alternate-Profile Facts

- `board_id = nrf52840dk`
- `display_name = nRF52840-DK`
- `mcu_family = nrf52840`
- `probe_family = jlink`
- `pyocd_target = nrf52840`
- `pack_name = nrf52840`
- `serial_baudrate = 115200`
- `test_read_address = 0x10000000`
- `silicon_id_address = 0x10000100`
- `silicon_id_expected = 0x00052840`
- `silicon_id_label = FICR.INFO.PART`
- `reference_uart_patterns[0] = "boot ok"`
- `requires_recover_validation = true`
- `recover_mode = nrf_pyocd_unlock`

These values are still useful as the retained alternate-board contract, but
they are not part of the current scoped-gate proof story.

## If Future Live Proof Is Wanted

Use this order:

1. `uv run pyocd list`
2. `uv run python host_bootstrap.py --board-id nrf52840dk`
3. `./firmware/nrf52840dk/reference/build_reference.sh`
4. `uv run python stage0_check.py --board-id nrf52840dk --reference-firmware nrf52840dk=firmware/nrf52840dk/reference/build/firmware.elf --recover-test nrf52840dk --confirm-shared-usb nrf52840dk`
5. `uv run python -m tests.harness.stage1_smoke --board-id nrf52840dk`

If serial auto-resolution is ambiguous, rerun with:

- `--port nrf52840dk=<port>`

## Current Status

Verified in repo:

- the repo contains one canonical reference-firmware source tree and one
  repeatable build entrypoint for `nrf52840dk`
- the repo-owned reference app mirrors the minimal Nordic baseline pattern:
  deterministic `boot ok` UART plus the fixed Stage 1 symbol contract
  `stage1_known_value = 0x1234ABCD`
- the build helper completed on this Mac host and produced
  `firmware/nrf52840dk/reference/build/firmware.elf` plus `firmware.hex`

Still pending if future support is desired:

- live Stage 0 proof on an attached `nrf52840dk`
- live Stage 1 smoke-harness proof on an attached `nrf52840dk`
- live MCP tool-surface validation on an attached `nrf52840dk`
