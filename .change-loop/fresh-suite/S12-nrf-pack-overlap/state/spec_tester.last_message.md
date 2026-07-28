Added replay-boundary coverage to [test_s12_nrf_pack_overlap_spec.py](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\tests\test_s12_nrf_pack_overlap_spec.py).

It now verifies CL-002/CL-003 through `resolve_registered_pack_geometry` with a temporary verified manifest/ZIP/PDSC leaf, scalar flash/RAM preservation, canonical physical flash, no fabricated tail, and successful read/debug-only `GenericMapGeometry`.

Recorded command passes under the neutral POSIX shell: 8 tests, 7 subtests; Ruff passes.

Remaining limitation: pyOCD normalizes overlapping raw XML `<memory>` rows before the server adapter receives them. The test validates the archive/PDSC and verified replay, then supplies parser-shaped overlap rows at the resolver seam to test the server’s actual adapter contract.

Charter checkpoints were reread and appended for pre-analysis, pre-edit, parser-seam refinement, verification, and final verdict. Removed the temporary inspection PDSC after use.
