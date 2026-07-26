Completed the assigned production-only static cleanup.

Changed:

- [server.py](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\src\pyocd_debug_mcp\server.py): sorted H04 setup-support slots.
- [datasheet_evidence.py](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\src\pyocd_debug_mcp\setup_flow\datasheet_evidence.py): corrected stale family-placeholder comment and local parser import formatting.
- [device_support.py](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\src\pyocd_debug_mcp\setup_flow\device_support.py): formatted H04 import placement and reused one ancestry parse per candidate.

Checks:

- Targeted H04 production import Ruff checks passed.
- New H04 setup-support slot is no longer reported.
- Compilation and diff whitespace checks passed.
- Broader Ruff still reports two pre-existing server slot-order issues outside H04 scope.
- Dependency-complete BasedPyright reports the documented pre-H04 cast plus the tester-owned structural-fake error; neither was edited.

Unresolved: neutral rerun and tester-owned static fixes remain required. No hardware action occurred.

Design-charter checkpoints completed and recorded: start, pre-edit, post-diff, pre-verification, and final verdict.
