# M8 hardware recovery acceptance — 2026-07-17

## Outcome

**Blocked safely; no recovery was executed.** Prompt 15.3 designates the recoverable
`nrf52833dk` bench board and forbids substitution. The attached Nordic probe is
J-Link `683377322` with UART `COM11`, but a bounded read of
`FICR.INFO.PART @ 0x10000100` returned `0x00052840`. The attached target is therefore
an nRF52840, not the required nRF52833.

Because the designated board identity did not match, the run stopped before
`target_unlock-plan`, before any one-time permission request, and before any typed
recovery backend call. No mass erase, sector erase, flash, memory write, register
write, reset, profile mutation, safety-map mutation, gate stamp, or permission grant
occurred.

## Read-only inventory evidence

- STM32 probe: ST-Link `066FFF514988525067233337`, UART `COM12` with the same USB
  serial.
- Nordic probe: J-Link `683377322`, UART `COM11`, USB serial `000683377322`.
- Live Nordic part register: `0x00052840`.
- pyOCD: `0.45.0`.
- Python: `3.12.13`.

The ordinary `pyocd list --probes` command listed the ST-Link and then encountered a
Windows console encoding error while rendering the J-Link description. The stable
J-Link identifier and live part value were therefore confirmed through the same
repository target-control adapter used by the server.

## Exact commands

```powershell
uv run pyocd list --probes
uv run python -c "from serial.tools import list_ports; import json; print(json.dumps([{'device':p.device,'description':p.description,'serial_number':p.serial_number,'vid':p.vid,'pid':p.pid} for p in list_ports.comports()], indent=2))"
uv run pyocd --version
```

The live identity read used this bounded body through `uv run python -`:

```python
from pyocd_debug_mcp.services import target_control

handle = target_control.open_session(
    board=None,
    unique_id="683377322",
    target="nrf52833",
)
try:
    print(hex(target_control.read_memory(handle, 0x10000100, 32)))
finally:
    target_control.close_session(handle)
```

Observed output: `0x52840`.

## Completion checklist

1. Disconnect the nRF52840 DK; do not treat it as the designated recovery board.
2. Attach the recoverable nRF52833 DK and its onboard J-Link/UART.
3. Positively match its stable probe identity and require
   `FICR.INFO.PART == 0x00052833` before any recovery planning.
4. Preserve any needed application/nonvolatile image and its exact restoration
   command.
5. Build/load the current authoritative nRF52833 safety map and validate the live
   connection.
6. Call `target_unlock-plan` through MCP to obtain the exact live identity,
   vendor mechanism, complete map-derived erased ranges/banks/sectors,
   all-nonvolatile warning, expected losses, fingerprint, and `plan_id`.
7. Obtain fresh explicit one-time approval for that unchanged disclosure, execute
   exactly once through the typed vendor primitive, and retain the immutable attempt
   report.
8. Prove the gate remains closed, run `board_validate`, and confirm guarded operation
   returns.
9. Request a second recovery only far enough to prove a fresh permission is required;
   do not execute a needless second erase.

Until those conditions are met, the destructive hardware criterion remains blocked,
not passed.

## Repeat attempt at 2026-07-17T15:01:45.6213430+00:00

The destructive acceptance prompt was run again after rereading the M8 plan and
Design Proto Spec §3.15. The read-only inventory was unchanged:

- J-Link probe identity: `683377322`.
- UART: `COM11`, USB serial `000683377322`.
- `FICR.INFO.PART @ 0x10000100 = 0x00052840`.
- `FICR.INFO.VARIANT @ 0x10000104 = 0x41414430` (`AAD0`).
- Session target override used only to make the identity read: `nrf52833`.
- pyOCD `0.45.0`; MCP SDK `1.28.1`; Python `3.12.13`;
  `pyocd-debug-mcp` `0.1.0`.

The exact identity command was:

```powershell
@'
from pyocd_debug_mcp.services import target_control
handle = target_control.open_session(board=None, unique_id='683377322', target='nrf52833')
try:
    part = target_control.read_memory(handle, 0x10000100, 32)
    variant = target_control.read_memory(handle, 0x10000104, 32)
    print({'probe_uid': handle.probe_uid, 'part': f'0x{part:08X}', 'variant': f'0x{variant:08X}', 'target_override': handle.session.options.get('target_override')})
finally:
    target_control.close_session(handle)
'@ | uv run python -
```

The target override cannot change physical silicon and was not treated as identity
evidence. Because the immutable part register still identifies nRF52840 silicon,
the run again stopped before firmware preservation, `target_unlock-plan`, permission
capture, or recovery. Backend recovery calls and erase operations remained zero.
