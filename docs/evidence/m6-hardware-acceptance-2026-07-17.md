# M6 hardware acceptance — 2026-07-17

## Outcome

- `nucleo_l476rg`: bounded first-time setup and `board_validate` hardware checks completed. The terminal setup status was `setup_safety_incomplete`; explicit validation returned `validation_incomplete` / `validation/safety-placeholder` with underlying hardware result `validation_passed_uart_not_configured`. This is the required pre-M7 closed state, not a gate-opening claim.
- `nrf52833dk`: **blocked, not passed**. The attached Nordic hardware is an nRF52840 DK. Its `FICR.INFO.PART` value was `0x00052840`, not the required `0x00052833`.
- No flash, erase, recovery, memory write, register write, or fixture reset was performed by this acceptance. Validation used bounded connections and reads. The bounded UART observation did not reset the target.

Full preserved roots:

- Nucleo successful run: `C:\Users\Jason\Documents\Jason\FirmCLI\M6-Hardware-Acceptance\2026-07-17_nucleo_l476rg_run4`
- nRF52833 blocked record: `C:\Users\Jason\Documents\Jason\FirmCLI\M6-Hardware-Acceptance\2026-07-17_nrf52833dk_blocked`
- Superseded Nucleo roots are preserved as `run1` through `run3`; no root was reused. Run1 stopped on FastMCP content decoding. Run2 exposed the missing MCP validation `code` field. Run3 completed but was superseded when the final audit found that the board's general connection policy selected under-reset mode. Run4 explicitly overrides validation sessions to pyOCD `connect_mode=attach` and is the acceptance result.

## Exact commands

Pinned STM32 pack provisioning:

```powershell
uv run python host_bootstrap.py --board-id nucleo_l476rg --install-packs
```

Successful isolated Nucleo acceptance:

```powershell
uv run python scripts/m6_hardware_acceptance.py --artifact-root C:\Users\Jason\Documents\Jason\FirmCLI\M6-Hardware-Acceptance\2026-07-17_nucleo_l476rg_run4 --probe-id 066FFF514988525067233337 --serial-id COM12 --board-id nucleo_l476rg --display-name "Nucleo-L476RG M6 Acceptance" --mcu-part-number STM32L476RGT6 --baudrate 115200 --target stm32l476rgtx --test-read-address 0x08000000 --silicon-id-address 0xE0042000 --silicon-id-expected 0x415 --silicon-id-mask 0xFFF
```

Read-only Nordic identity check (the body was piped to `uv run python -`):

```python
from pyocd_debug_mcp.services import target_control

h = target_control.open_session(board=None, unique_id="683377322", target="nrf52840")
try:
    actual = target_control.read_memory(h, 0x10000100, 32)
    print(f"0x{actual:08X}")
finally:
    target_control.close_session(h)
```

## Nucleo observations

- Probe: ST-Link `066FFF514988525067233337`.
- UART: `COM12`, USB serial `066FFF514988525067233337`, VID/PID `0483:374B`. The stable probe/UART relationship was exact and provable.
- Target: `stm32l476rgtx`, resolved from the official board mapping after exact MCU-part consistency checking against user input `STM32L476RGT6`.
- Pack: `Keil.STM32L4xx_DFP` 3.1.0, pinned file `Keil.STM32L4xx_DFP.3.1.0.pack`. Observed SHA-256 `5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f` matched the manifest.
- Live read: `0x08000000 -> 0x20001180`.
- Silicon read: `DBGMCU_IDCODE` at `0xE0042000 -> 0x10076415`; masked device ID `0x415` matched.
- Every `BoardValidator` connection explicitly used pyOCD `connect_mode=attach`, overriding the Nucleo's general under-reset connection workaround. Validation did not request reset, halt, flash, erase, recovery, or a write.
- UART: bounded to 3 seconds and 64 KiB on COM12 at 115200. It captured zero bytes. No expected UART substring was configured, so this is an observation, **not** a UART-functional pass.
- First attachment-cache resolution: `no_record`. After successful bounded hardware validation, the exact stable pairing was confirmed. Second resolution returned `reused=true`, `reason=exact_match`, `port_path=COM12`.
- Core profile commit: `2026-07-17T12:16:03.002295Z`, SHA-256 `27cb4bdf12a5114c68efa4149e11d63fd88426790495c54a87029702e82b3f6b`.
- Optional facts were committed only after live validation: `2026-07-17T12:16:06.179290Z`. Final restored profile SHA-256 is `918ad504bfdba49e780a6dc54819cd2ecd3fd46b648150058792593554125ee6`.
- A deliberately wrong masked silicon expectation (`0x414`) returned `validation_failed` / `validation/silicon-mismatch`. The validator left the mismatched fixture byte-for-byte unchanged; the harness then restored only that isolated fixture, whose hash again matched the pre-test optional profile.
- Four immutable reports were retained: one setup report and three validation reports. A deliberate overwrite attempt raised `ImmutableArtifactError`, and all report hashes remained unchanged.

The successful `acceptance.json` SHA-256 is `a8fd57225acf327adb82c32d82df478f082af9dff8c185b5789dcd215fd26ec4`.

## nRF52833 blocked checklist

Observed instead: J-Link `683377322`, UART COM11 (USB serial `000683377322`, VID/PID `1366:1025`), and `FICR.INFO.PART=0x00052840`.

To complete the missing acceptance:

1. Attach an actual nRF52833 DK and expose its onboard J-Link probe.
2. Expose the UART belonging to that same board and record its current COM port.
3. Confirm a bounded read of `FICR.INFO.PART` at `0x10000100` returns `0x00052833`.
4. Run `uv run python host_bootstrap.py --board-id nrf52833dk` and confirm built-in target `nrf52833`.
5. Run the acceptance script from a new clean root with target `nrf52833`, test read `0x10000000`, and silicon expectation `0x00052833`/`0xFFFFFFFF`.
6. Preserve `setup_safety_incomplete` and `validation/safety-placeholder`; M7 must still establish the real map and stamp.

The blocked record SHA-256 is `105c77c4c2c8e8d4b3b01c2cbd7ac5bc9e5abb2395c89e4d050285cd97da6a72`.
