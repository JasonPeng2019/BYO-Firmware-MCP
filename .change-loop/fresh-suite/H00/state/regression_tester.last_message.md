I read the complete charter after the edits and before this verdict.

I did not find a concrete charter violation in `tests/test_h00_repository_regressions.py`.

Evidence:
- The test is self-contained and uses a temporary candidate tree, not the live repo, at [`tests/test_h00_repository_regressions.py:18-28`](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\tests\test_h00_repository_regressions.py#L18).
- It preserves diagnostic strength by asserting the Pyright run ignores the injected `tests/` sentinel and still fails on an injected `src/` error at [`tests/test_h00_repository_regressions.py:30-56`](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\tests\test_h00_repository_regressions.py#L30).
- It avoids the broken repo venv by using an isolated `uv run --no-project --with pyright pyright` invocation at [`tests/test_h00_repository_regressions.py:17`](C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP\tests\test_h00_repository_regressions.py#L17).
- It does not cross into production ownership or `tests/test_process_cleanup.py`, which stays untouched.

So the checkpoint passes: the regression test independently protects the adjacent Pyright scope without weakening diagnostics, fabricating platform proof, or crossing ownership boundaries.
