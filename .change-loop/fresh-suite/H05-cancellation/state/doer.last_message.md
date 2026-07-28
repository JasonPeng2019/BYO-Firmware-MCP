Formatter-only repair completed in [misc.py](/C:/Users/Jason/Documents/Jason/FirmCLI_Tester/Firmware-Test-Manual/MCP-Trial-3/BYO-Firmware-MCP/src/pyocd_debug_mcp/tools/misc.py). CL-001 behavior is unchanged.

Exact results:

- `uvx ruff format src/pyocd_debug_mcp/tools/misc.py` — `1 file reformatted`
- `uvx ruff check src/pyocd_debug_mcp/tools/misc.py` — passed
- `uvx ruff format --check src/pyocd_debug_mcp/tools/misc.py` — `1 file already formatted`
- specified unittest command — passed, `Ran 18 tests`, `OK`

The pre/post diff confirms only formatting normalization; managed waits still use the cancellation event, direct calls retain injected sleep, and success event/text stay inside `run_if_not_cancelled`.

Charter checkpoints were reread and appended: pre-analysis, pre-formatting, pre-verification, and pre-final report. No neutral-gate claim made.
