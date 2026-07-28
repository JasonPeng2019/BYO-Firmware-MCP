Authorized local firmware-server validation. This is host-only work in the named local
`BYO-Firmware-MCP` repository. No board, remote target, or third-party system is in scope.

Resume the exact persistent H05 spec-tester role. A main-model verification pass found one
new-file lint defect owned by this role:

```text
BLE001 Do not catch blind exception: BaseException
tests/test_h05_wait_cancellation_spec.py:85
```

Make the narrowest correct test-only cleanup. The test expects the worker's
`OperationCancelledError`, which is an ordinary exception; catch `Exception` rather than
`BaseException` and preserve the existing assertion that the captured exception is exactly the
expected cancellation type. Do not edit production source, the regression tester's file, another
test, dependencies, or change-loop scripts. Preserve the existing spec command and manifest.

Then run:

```text
uvx ruff check tests/test_h05_wait_cancellation_spec.py
./.h01-venv-batchstrict/Scripts/python.exe -m unittest -q tests.test_h05_wait_cancellation_spec
```

Before analysis, immediately before the edit, before verification, and before the final report,
reread the complete `../.codex/design_charter.md` and append a dated checkpoint to
`.change-loop/fresh-suite/H05-cancellation/DESIGN_CHARTER_CHECKS.md`. Each checkpoint must state
the contemplated test-only diff, applied charter properties, assumption/tie-breaker, rejected
board/OS/toolchain-specific alternatives, and scope exclusions.

Report exact command results. Do not claim the neutral gate; the main orchestrator will rerun it.
