# M7 hardware acceptance — 2026-07-17

## Outcome

- `nucleo_l476rg`: **passed** in isolated run 4. The run built a real safety map,
  validated the live STM32L476 to open the gate, sector-programmed application v1,
  refreshed application-only fingerprint drift, sector-programmed v2 without
  revalidation, then proved disconnect + refresh remained closed until a second
  `board_validate`.
- `nrf52833dk`: **blocked, not passed, and not written**. The connected Nordic
  hardware reports `FICR.INFO.PART=0x00052840`; it is an nRF52840, not the required
  nRF52833.
- No mass erase, bootloader flash, option-byte operation, recovery, or unsafe
  out-of-range erase was attempted.

Accepted root:

`C:\Users\Jason\Documents\Jason\FirmCLI\M7-Hardware-Acceptance\2026-07-17_m7_run4`

Accepted result:

- `acceptance.json` SHA-256:
  `f7cc6602f6946e44d1bebd9943400abcfad310caf40a3615f2235cd27b668b68`
- Terminal status: `nucleo_m7_hardware_passed`

Disposable sources, builds, and the pre-flash backup are preserved in run 1.
Runs 1 and 2 stopped before any programmer call. Run 3 completed two sector
programs but was superseded because its verifier read flash while the core was
running; a later halted diagnostic proved v2 had zero byte differences. Run 4
repeated the full sequence with asserted halted readback for both images.

## Hardware, pack, and linker evidence

- Probe: ST-Link `066FFF514988525067233337`.
- UART inventory: `COM12`, stable USB serial
  `066FFF514988525067233337`, VID/PID `0483:374B`.
- Live pyOCD target: `STM32L476RGTx` / `stm32l476rgtx`.
- Live `DBGMCU_IDCODE=0x10076415`; masked device ID `0x415` matched the
  STM32L476 family.
- Pinned pack: `Keil.STM32L4xx_DFP` 3.1.0, SHA-256
  `5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f`.
- Live pack/device flash: `[0x08000000, 0x08100000)`, 1 MiB, uniform
  2 KiB erase pages. These facts agree with ST RM0351 and the STM32L476RG
  product/datasheet facts. Direct PDF downloads were attempted twice with
  120-second bounds and returned no bytes, so no local document hash is claimed.
- Both linker artifacts independently derived the application partition
  `[0x08000000, 0x08008000)` and RAM `[0x20000000, 0x200011C0)`.
- v1 ELF SHA-256:
  `110fbe691c414929577d39cf7f7d054561a90101187e61b7d63e8507c8714b2f`.
- v2 ELF SHA-256:
  `02b381d77b5a41e314b09d5035883dc38eded619db9148b0d4f3babb35191ab2`.

The pre-flash application partition was backed up completely as BIN and Intel
HEX before any write:

- Range: `[0x08000000, 0x08008000)` / 32768 bytes.
- BIN SHA-256:
  `e4f7f5b813d36179ffdf141ab428d97d52b0cd7e1848045d56aa77a89213ba0e`.
- HEX SHA-256:
  `7a25cef9f1319213beb90238727c14abfde0d61f7b1b5287901ebf368ff286ae`.

Recovery command (sector erase only):

```powershell
uv run pyocd load -u 066FFF514988525067233337 -t stm32l476rgtx -O chip_erase=sector C:\Users\Jason\Documents\Jason\FirmCLI\M7-Hardware-Acceptance\2026-07-17_m7_run1\backups\nucleo_l476rg_application_before.hex
```

## Gate and refresh sequence

1. Safety setup completed with aggregate
   `5dc19dc27bbcef1fdb747b5e5524c5b7dbb5d4fbc1c5bb203b5e5a6d24d38faa`.
2. Validation steps 1–9 returned
   `validation_passed_uart_not_configured` and stamped the active connection.
3. v1 was checked, sector-programmed, and matched its full HEX content during
   bounded halted readback. The target was restored to running from reset.
4. Relinking changed exactly `application_artifacts`. Before refresh, freshness
   returned `safety/fingerprint-stale` with remedy `board_safety_refresh`.
5. The scoped refresh rebuilt exactly `application_artifacts`, produced aggregate
   `17905d94952ba7a33744328c24666b05e36d18ce061e9c84f2d142845a7606a2`,
   and restamped the still-valid connection without `board_validate`.
6. v2 was sector-programmed without revalidation and matched its full HEX content
   during bounded halted readback. The target was restored to running from reset.
7. Disconnect cleared the stamp. After reconnect, an unchanged successful refresh
   left the gate absent. `require_write` returned
   `gate/validation-required`, remedy `board_validate`.
8. A second validation returned `validation_passed_uart_not_configured` and stamped
   the new connection with the v2 aggregate. Final disconnect closed it again.

The bounded UART observations in accepted run 4 captured no bytes, so UART is not
used as pass evidence. Full application readback is the authoritative firmware
verification. Superseded run 3 did observe both `boot ok` and
`boot ok m7 refresh`, consistent with the two disposable builds.

## AC-14.10 sector containment

- Actual pyOCD version: 0.45.0.
- The adapter was instrumented at runtime. Both programmer constructors received
  exactly `chip_erase="sector"`; exactly two program calls occurred; mass-erase
  calls were zero.
- Safe boundary image: byte at `0x08007FFF`; computed sector
  `[0x08007800, 0x08008000)`; allowed.
- Unsafe boundary image: byte at `0x08008000`; computed sector
  `[0x08008000, 0x08008800)`; rejected as the wrong region kind.
- The unsafe image was never submitted to pyOCD and recorded zero backend
  erase/write calls.

This proves the backend can be forced to sector erase and the required sectors can
be computed and checked before programming. AC-14.10 was not weakened.

## Exact successful command

```powershell
uv run python scripts/m7_hardware_acceptance.py --artifact-root 'C:\Users\Jason\Documents\Jason\FirmCLI\M7-Hardware-Acceptance\2026-07-17_m7_run4' --v1-elf 'C:\Users\Jason\Documents\Jason\FirmCLI\M7-Hardware-Acceptance\2026-07-17_m7_run1\builds\v1\reference\build\firmware.elf' --v1-hex 'C:\Users\Jason\Documents\Jason\FirmCLI\M7-Hardware-Acceptance\2026-07-17_m7_run1\builds\v1\reference\build\firmware.hex' --v2-elf 'C:\Users\Jason\Documents\Jason\FirmCLI\M7-Hardware-Acceptance\2026-07-17_m7_run1\builds\v2\reference\build\firmware.elf' --v2-hex 'C:\Users\Jason\Documents\Jason\FirmCLI\M7-Hardware-Acceptance\2026-07-17_m7_run1\builds\v2\reference\build\firmware.hex' --backup-hex 'C:\Users\Jason\Documents\Jason\FirmCLI\M7-Hardware-Acceptance\2026-07-17_m7_run1\backups\nucleo_l476rg_application_before.hex' --probe-id '066FFF514988525067233337' --serial-id 'COM12' --board-id nucleo_l476rg --display-name 'Nucleo-L476RG M7 Acceptance' --mcu-part-number STM32L476RGT6 --target stm32l476rgtx --baudrate 115200
```

Disposable builds used the existing pinned workspace and SDK:

```powershell
uv run pyocd-zephyr-build --workspace-dir 'C:\Users\Jason\AppData\Local\firmware-cli\zephyr\v4-3-0\workspace' --sdk-dir 'C:\ncs\toolchains\936afb6332\opt\zephyr-sdk' --app-dir '<run1>\builds\v1\reference\src' --build-dir '<run1>\builds\v1\reference\build' --board nucleo_l476rg --pristine always
uv run pyocd-zephyr-build --workspace-dir 'C:\Users\Jason\AppData\Local\firmware-cli\zephyr\v4-3-0\workspace' --sdk-dir 'C:\ncs\toolchains\936afb6332\opt\zephyr-sdk' --app-dir '<run1>\builds\v2\reference\src' --build-dir '<run1>\builds\v2\reference\build' --board nucleo_l476rg --pristine always
```

Focused post-hardware validation:

```text
59 passed
ruff: All checks passed
pyright: 0 errors, 0 warnings, 0 informations
```

## Integration defects found

The bench run exposed and fixed one active-session validation defect: pyOCD's
external inventory omits a probe already claimed by this process. Validation now
merges stable identities from `ConnectionManager`, allowing `board_validate` to
stamp the active connection; a focused regression test covers it.

The audit also found a deferred public-surface gap: the MCP tool list does not
register `board_safety_setup` or `board_safety_refresh`, despite A-20 listing them.
The hardware harness invoked the same in-process `SafetyMapBuilder` and
`SafetyRefresher` engines composed by the server and recorded that deviation. The
underlying refresh/gate behavior passed, but MCP registration remains work before
claiming the entire public M7 surface complete.

### Post-acceptance implementation audit

The later audit through Prompt 15.3 closed this software gap. Both tools are now
registered in the MCP surface, remain visible, and enforce the A-20 per-board/run
`load_setup_tool` redirect before invoking their authoritative server callbacks.
The production freshness callback also re-hashes the setup-selected build and pack
artifact paths on every guarded write, and build-only public refreshes reconstruct
their replacement regions from the tracked ELF/HEX/map evidence. Focused in-process
surface, redirect, freshness, region-rebuild, and refresh tests cover the repaired
path. This does not change the historical nRF52833 hardware blocker below.

## nRF52833 blocked checklist

Observed attached Nordic hardware:

- Probe `683377322` / J-Link.
- UART `COM11`, USB serial `000683377322`, VID/PID `1366:1025`.
- Live `FICR.INFO.PART @ 0x10000100 = 0x00052840`.

To complete the requested nRF52833 acceptance:

1. Attach an actual nRF52833 DK and its onboard J-Link/UART.
2. Confirm the live part register returns `0x00052833` before any write.
3. Build disposable nRF52833 reference v1/v2 artifacts in a new isolated root.
4. Back up the complete linker-derived application partition.
5. Run the same map → validate → v1 flash → relink/refresh → v2 flash →
   disconnect/refresh/validate sequence.
6. Preserve its maps, fingerprints, boundary images, reports, programmer trace,
   readbacks, and recovery command.
