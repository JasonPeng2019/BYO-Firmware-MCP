Simplified [`processes.py`](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\src\pyocd_debug_mcp\kernel\processes.py) to one `primary_failure` flag.

It now treats any body `ProcessIdentityUnavailable`, `AttributeError`, or `OSError` identically for cleanup: `CloseHandle` is always attempted, but a secondary close failure cannot replace the primary error. Close-only failures still propagate to the outer normalization boundary.

Checks with `UV_PROJECT_ENVIRONMENT=.h01-venv`:

- Three focused identity tests: `3 passed, 4 subtests passed`
- Ruff, Pyright, and `git diff --check`: passed

Charter reread completed both before the edit and after verification.
