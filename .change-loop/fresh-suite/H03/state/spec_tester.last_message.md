Added [test_h03_manifest_canonical.py](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\tests\test_h03_manifest_canonical.py).

It attacks CL-001 by checking:

- Exact sorted compact UTF-8 manifest bytes with one LF only; no CR, CRLF, or BOM.
- Unicode producer/source metadata, lexical role ordering, schema/provenance, copied hashes and sizes, and result payload.
- Missing-role and nonempty-destination refusals preserve destination state and leave no staging residue.

Recorded command:

`uv run pytest -q tests/test_h03_manifest_canonical.py`

Verification passed: 2 tests.

No remaining untestable ambiguity. Charter checkpoints completed: before analysis, immediately before editing, between test features/before verification, and before final verdict.
