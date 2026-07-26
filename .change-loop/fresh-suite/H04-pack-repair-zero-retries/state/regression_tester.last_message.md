Added regression coverage in [test_regression_h04_pack_index_repair.py](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\tests\test_regression_h04_pack_index_repair.py).

Covered blast radius:

- Failed refresh descriptor download preserves prior retained PIDX and enables offline recovery.
- Failed rebuild never publishes newly fetched master evidence.

Verification: 2 tests passed; Ruff passed. Recorded command and manifest in the required state files.

Residual risk: real cmsis-pack-manager rebuild byte stability remains covered by the spec suite, not this focused failure-boundary suite. Repository-wide `git diff --check` reports pre-existing unrelated whitespace issues.

Design-charter checkpoints completed: before analysis, immediately before editing, between test features, before verification, and before final verdict.
