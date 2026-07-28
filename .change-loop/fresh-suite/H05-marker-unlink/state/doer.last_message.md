Completed the manager cleanup follow-up.

- Removed only the redundant `pass` after the retained-marker conditional raise in [swd_process.py](/C:/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP/src/pyocd_debug_mcp/adapters/swd_process.py:439).
- Added manager cleanup and pre-final checkpoint entries to `DESIGN_CHARTER_CHECKS.md`.
- No tests, commands, manifests, hardware, or unrelated files were modified.

Verification:

- `.venv/Scripts/ruff.exe check --select PIE790 src/pyocd_debug_mcp/adapters/swd_process.py` — passed.
- `git diff --check -- src/pyocd_debug_mcp/adapters/swd_process.py` — passed (only existing CRLF advisory).
- PowerShell equivalent of `PYTHONPATH=src ./.venv/Scripts/python.exe -m unittest tests/test_h05_marker_unlink_spec.py` — 5 passed.
- `./.h01-venv-batchstrict/Scripts/python.exe -m unittest -q tests.test_regression_h05_marker_unlink` — 3 passed.

Charter rereads/checkpoints: pre-cleanup and pre-final follow-up. No hardware action or commit performed.
