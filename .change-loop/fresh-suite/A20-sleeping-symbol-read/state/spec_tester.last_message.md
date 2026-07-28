Extended [test_a20_sleeping_symbol_read_spec.py](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\tests\test_a20_sleeping_symbol_read_spec.py) with:

- `KeyboardInterrupt` read failure: exactly one resume, same primary object re-raised.
- Dual `KeyboardInterrupt`/`SystemExit` failure: both facts reported and exception chained from primary.
- Published help: all four parameters and the concrete `read_memory_symbol(...)` example.

Results:

- Recorded Bash spec command: `10 passed, 6 subtests passed`
- Ruff: passed
- `git diff --check`: passed
- Charter SHA-256 verified at every required stage and checkpoints appended.

Remaining untestable ambiguity: none within the injected host-side lifecycle contract.
