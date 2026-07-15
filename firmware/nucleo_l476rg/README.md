# Nucleo-L476RG Reference Firmware

This board package defines the repo-owned Stage 0 reference firmware for
`nucleo_l476rg`.

The intent is narrow:

- keep one tiny, deterministic baseline app in-repo
- build it into the canonical artifact name `reference/build/firmware.elf`
- use that artifact through the current `stage0_check.py` flow and later shared
  validation/flash logic
- prove that the board prints `boot ok` over the ST-LINK virtual COM port

Source of truth for the app:

- app root: `firmware/nucleo_l476rg/reference/src/`
- canonical symbol-bearing artifact:
  `firmware/nucleo_l476rg/reference/build/firmware.elf`
- optional flashable side artifact:
  `firmware/nucleo_l476rg/reference/build/firmware.hex`

Build recipe:

```bash
uv run pyocd-zephyr-build --app-dir firmware/nucleo_l476rg/reference/src --build-dir firmware/nucleo_l476rg/reference/build --board nucleo_l476rg
```

After changing the tracked UART path or overlay for this board, rebuild the
canonical artifacts before rerunning Stage 0 / Stage 1 / `R11` on another host.
The flashed STM32 behavior only changes when the generated `reference/build/`
artifacts are refreshed.

What that script does:

- uses the repo-owned `pyocd-zephyr-build` helper
- reuses an existing Zephyr workspace or SDK when one is already present
- otherwise bootstraps a managed upstream Zephyr workspace plus SDK in the
  local cache
- builds the app for `nucleo_l476rg`
- preserves the live Zephyr build tree inside `reference/build/` so repeated
  agent rebuilds stay fast and remain under the no-hang watchdog
- copies `zephyr/zephyr.elf` to the canonical `firmware.elf` name
- copies `zephyr/zephyr.hex` to `firmware.hex` when present
- defines one known Stage 1 symbol contract in the reference app:
  `stage1_known_value = 0x1234ABCD`

Stage 0 validation flow once the board is physically attached:

```bash
uv run python host_bootstrap.py --board-id nucleo_l476rg
uv run python stage0_check.py \
  --board-id nucleo_l476rg \
  --reference-firmware nucleo_l476rg=firmware/nucleo_l476rg/reference/build/firmware.elf
```

The board config declares the expected UART substring, so `--expect` is not
required for the normal reference-firmware path.

Stage 1 smoke-harness flow on a live bench:

```bash
uv run python -m tests.harness.stage1_smoke --board-id nucleo_l476rg
```

If the benchmark corpus was changed too, rebuild the STM32 bug artifacts before
running the expanded `R11` corpus:

```bash
uv run pyocd-zephyr-build --app-dir firmware/nucleo_l476rg/bugs/b001__wrong_boot_text/src --build-dir firmware/nucleo_l476rg/bugs/b001__wrong_boot_text/build --board nucleo_l476rg
uv run pyocd-zephyr-build --app-dir firmware/nucleo_l476rg/bugs/b002__wrong_known_value/src --build-dir firmware/nucleo_l476rg/bugs/b002__wrong_known_value/build --board nucleo_l476rg
uv run pyocd-zephyr-build --app-dir firmware/nucleo_l476rg/bugs/b003__silent_uart/src --build-dir firmware/nucleo_l476rg/bugs/b003__silent_uart/build --board nucleo_l476rg
uv run pyocd-zephyr-build --app-dir firmware/nucleo_l476rg/bugs/b004__dual_signal_regression/src --build-dir firmware/nucleo_l476rg/bugs/b004__dual_signal_regression/build --board nucleo_l476rg
```

The legacy `build_reference.sh` / `build_bug.sh` wrappers still exist for bash
users, but they now delegate to the same `pyocd-zephyr-build` helper. `NCS` is
optional: this board can rebuild either by reusing an existing `NCS` workspace
or by letting the helper bootstrap upstream Zephyr itself.

Bench diagnosis note:

- if Stage 0 flash + SWD access pass but UART is still silent on Windows,
  rebuild and flash upstream Zephyr `samples/hello_world` for
  `nucleo_l476rg`
- if that control sample is also silent, treat the failure as a bench UART
  problem on the current host/board pair rather than a repo firmware regression

Verified:

- The repo now contains one canonical reference-firmware source tree and one
  repeatable build entrypoint for `nucleo_l476rg`.
- Zephyr board support on this host binds `zephyr,console` to `usart2`, which
  matches the tracked Stage 0 UART expectation for this board.
- The build helper completed on this Mac host and produced
  `firmware/nucleo_l476rg/reference/build/firmware.elf` plus `firmware.hex`.
- `uv run python host_bootstrap.py --board-id nucleo_l476rg` passed on this Mac
  host with:
  - probe UID `0668FF514988525067213913`
  - serial port `/dev/cu.usbmodem144403`
  - confirmed target `stm32l476rgtx`
  - confirmed pack token `stm32l476`
  - generic serial matching working even without `STM32_Programmer_CLI`
- `uv run python stage0_check.py --board-id nucleo_l476rg --reference-firmware
  nucleo_l476rg=firmware/nucleo_l476rg/reference/build/firmware.elf` passed for
  flash plus UART on this Mac host and captured `boot ok`.
- `uv run python -m tests.harness.stage1_smoke --board-id nucleo_l476rg`
  passed on this Mac host and proved:
  - flash through the shared SWD service
  - `pc` read after `reset_and_halt`
  - shared symbol resolution for `stage1_known_value`
  - target-memory readback of `0x1234ABCD`
  - UART capture of `boot ok` through the shared UART service
- The human operator confirmed that the visible ST-Link debug probe and the
  visible `/dev/cu.usbmodem144403` virtual COM port were exposed by the same
  physical USB-attached `nucleo_l476rg` board during the final STM32 bench run.
- The tracked STM32 reference and bug apps now bind their UART path explicitly
  to `USART2` and also pin Zephyr's chosen console to `usart2` through a shared
  overlay under `firmware/nucleo_l476rg/common/`.
  The app-side UART helper now writes through the standard Zephyr console path
  (`printk`) while keeping the board-level route fixed to `USART2`.

Out of scope for this board package:

- The STM32 recover story is not part of the current Stage 0 contract for this
  board family.
