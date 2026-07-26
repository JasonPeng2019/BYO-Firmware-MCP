Added [test_h03_manifest_regressions.py](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\tests\test_h03_manifest_regressions.py).

Coverage:
- MCP artifact-collection caller preserves payload, canonical copies, UTF-8 manifest bytes, LF-only output, and no BOM.
- Nonempty-destination refusal preserves contents and leaves no staging residue.

Recorded harness command:

`uv run --locked --no-sync pytest tests/test_h03_manifest_regressions.py`

Result: 2 passed.

Charter checkpoints completed: before analysis, immediately before editing, before verification, and before final verdict.

Residual risk: platform-specific newline translation itself is not directly simulated on this Windows host; the byte-level assertions would catch any regression back to text-mode serialization.
