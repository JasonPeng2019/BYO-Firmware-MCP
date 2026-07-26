No source change this iteration.

Neutral gate remains blocked before test collection: `.venv` was removed and `uv run --locked --no-sync pytest …` cannot find `pytest` (exit 2 for both suites). This is an environment bootstrap issue, not a production assertion failure.

The only production changes remain [registry.py](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\src\pyocd_debug_mcp\kernel\registry.py) and [batch.py](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\src\pyocd_debug_mcp\tools\batch.py). I did not modify tests, manifests, commands, dependencies, lock state, or hardware.

Checks: `git diff --check` passed. Charter checkpoints: before analysis/diagnosis, before verification, and before final verdict.
