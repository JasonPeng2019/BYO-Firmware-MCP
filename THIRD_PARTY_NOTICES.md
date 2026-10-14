# Third-party notices

## CPython Windows worker startup

`services/can_windows_spawn.py` adapts CPython's Windows multiprocessing startup
to isolate CAN worker standard handles from the MCP protocol. CPython is
copyright the Python Software Foundation and its contributors; its license
and historical notices are retained in `LICENSES/Python.txt` (installed as
`pyocd_debug_mcp/licenses/Python.txt`).

## Firmware-CLI CAN support

The CAN transport implementation under `src/pyocd_debug_mcp/` contains code
copied or adapted from the user's Firmware-CLI repository at commit
`0e8a45d` (https://github.com/JasonPeng2019/Firmware-CLI).

That derived code is available under the Apache License, Version 2.0. A copy is
included at `LICENSES/Apache-2.0.txt`. BYO-specific modifications add the
multi-board session manager, startup contract, plan/validation integration,
and MCP response/lifecycle behavior described in this repository.

This notice applies to:

- `adapters/can_interface.py`
- `adapters/can_pythoncan.py`
- `adapters/can_socketcan.py`
- `can_capabilities.py`
- `can_config.py`
- `can_errors.py`
- `can_secure_files.py`
- `can_startup.py`
- `guardrails/can_gate.py`
- `services/can_worker.py`
- `services/can_session.py`
