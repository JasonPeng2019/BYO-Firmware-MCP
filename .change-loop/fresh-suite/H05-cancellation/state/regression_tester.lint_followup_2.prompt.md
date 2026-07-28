Authorized local firmware-server validation. This is host-only work in the named local
`BYO-Firmware-MCP` repository. No board, remote target, or third-party system is in scope.

Resume the exact persistent H05 regression-tester role. Main-model verification found two
remaining lint defects in this role's owned new test:

```text
ASYNC115 use a checkpoint rather than anyio.sleep(0)
tests/test_regression_h05_wait_cancellation.py:41
BLE001 do not catch BaseException
tests/test_regression_h05_wait_cancellation.py:54
```

Make only narrow test-quality corrections. Use an ordinary finite scheduler yield that satisfies
the repository lint contract (for example the same `anyio.sleep(0.001)` pattern already used by
the spec tester) and catch `Exception`, preserving the assertion that the captured exception is
`OperationCancelledError`. Do not edit production source, the spec tester's file, another test,
dependencies, or change-loop scripts. Preserve the existing regression command and manifest.

Then run:

```text
uvx ruff check tests/test_regression_h05_wait_cancellation.py
./.h01-venv-batchstrict/Scripts/python.exe -m unittest -q tests.test_regression_h05_wait_cancellation
```

Before analysis, immediately before the edit, before verification, and before the final report,
reread the complete `../.codex/design_charter.md` and append a dated checkpoint to
`.change-loop/fresh-suite/H05-cancellation/DESIGN_CHARTER_CHECKS.md`. Each checkpoint must state
the contemplated test-only diff, applied charter properties, assumption/tie-breaker, rejected
board/OS/toolchain-specific alternatives, and scope exclusions.

Report exact command results. Do not claim the neutral gate; the main orchestrator will rerun it.
