# Autonomous current-tree hardware acceptance

The fresh isolated workspace
`C:\Users\Jason\Documents\Jason\FirmCLI\za726` contained no board YAML or
application source before the autonomous run. A delegated agent used only the
public MCP surface on branch `Jason-MCP-v2`.

## Result

Status: **pass**.

- Setup asked for and bound `NF Board`, `nrf52840dk`, exact package
  `nRF52840-QIAA`, probe `683377322`, UART `COM11` at 115200 baud, and the
  supplied authoritative PDF (SHA-256
  `c619e336b9c0610663273041f057f2537a65fd408ce0c5b8214a26de2aa88422`)
  before application source existed.
- The checkout-local setup-only runner completed its strict readiness barrier
  before the delegated agent created any application source.
- Independent schema-v2 official and device-support evidence agreed on erase
  origin `0` and erase size `4096`; the committed safety map used reconciled
  hardware provenance only.
- The agent executed the exact returned PowerShell command, including `&`, to
  build `nrf52840dk/nrf52840`. The helper found and validated local
  `C:\ncs\v3.3.1` plus its bundled Zephyr SDK 0.17.0 before any managed network
  fallback. The agent then refreshed safety, validated, and flashed only
  through the guarded application action.
- One `serial_exchange` handle performed delayed readiness followed by five
  planned steps. It observed worker-thread ON and OFF logs, command
  acknowledgements, ON/OFF status, and a 1200 ms quiet-after-OFF proof.
- The final worker state was OFF and MCP disconnect released the board.

The exact rendered build command was:

```powershell
& 'C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\.venv\Scripts\python.exe' '-m' 'pyocd_debug_mcp.zephyr_build' '--app-dir' 'C:\Users\Jason\Documents\Jason\FirmCLI\za726' '--build-dir' 'C:\Users\Jason\Documents\Jason\FirmCLI\za726\build' '--board' 'nrf52840dk/nrf52840'
```

## Machine-readable proof

- Evidence: `C:\Users\Jason\Documents\Jason\FirmCLI\za726\acceptance\evidence.json`
- Evidence SHA-256:
  `158fe8737ec5312429e67482c43e2d915d7a3ba59ab375f6361e76ed7bb409f6`
- Final hardware Server Run: `run-20260717T234741Z-b69e7dda`
- Strict validator result: pass; 20 artifacts, 20 causally linked MCP
  operations, and 5 immutable reports.
- Tool versions: MCP SDK 1.28.1, pyOCD 0.45.0, Python 3.12.13, west 1.5.0,
  Zephyr 4.3.99 (NCS v3.3.1), Zephyr SDK 0.17.0.

Two pre-flash build attempts exposed repository bootstrap defects without
mutating hardware: the private extraction interpreter lacked `py7zr`, then the
minimal managed SDK delegated to missing host `wget`. The helper now validates
its actual extraction interpreter, discovers complete local NCS tooling before
pip/network work, rejects incomplete caches, and keeps a checksum-verified
Python-owned managed fallback. The final exact command passed without a manual
SDK path or host-tool workaround.
