Implemented CL-001 in [artifact_collector.py](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\src\pyocd_debug_mcp\artifact_collector.py:178).

`build-manifest.json` now writes canonical UTF-8 bytes with exactly one `b"\n"` terminator, preventing Windows CRLF translation while preserving the existing serialization, schema, staging, atomic replacement, and refusals.

Checks run:

- Direct collector verification passed: exact canonical byte equality; no CR/BOM; Unicode producer/source metadata; role ordering; hashes; missing-role and nonempty-destination refusals with no staging residue.
- `git diff --check` passed for the changed source file.
- Focused pytest could not run: neither the default Python nor available project virtual environments provide pytest. Neutral harness remains the acceptance gate.

Unresolved: neutral spec/regression commands and report are not yet present, so green status is not claimed. Existing unrelated working-tree changes were preserved.

Design-charter checkpoints completed: before analysis, immediately before edit, before verification, before verification retry, and before final verdict.
