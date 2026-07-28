Implemented the accepted post-gate corrections in [memory.py](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\src\pyocd_debug_mcp\tools\memory.py):

- Cleanup now attempts exactly one `resume` after any `BaseException` raised during a non-halted scalar read, preserving the primary failure or reporting both failures.
- `read_memory_symbol` MCP help now explicitly documents `board_id`, `symbol`, `width`, optional `elf_artifact`, and an invocation example.

Checks passed:

- A20 focused suites: 10 passed, 6 subtests passed
- Ruff
- Lockfile-managed Pyright: 0 errors
- `git diff --check`

Charter SHA-256 was verified at every required stage and checkpoints recorded in [DESIGN_CHARTER_CHECKS.md](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\.change-loop\fresh-suite\A20-sleeping-symbol-read\DESIGN_CHARTER_CHECKS.md).
