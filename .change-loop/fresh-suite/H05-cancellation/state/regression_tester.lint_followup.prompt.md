Authorized local firmware-server validation. This is host-only work in the named local
`BYO-Firmware-MCP` repository. No board, remote target, or third-party system is in scope.

Resume the exact persistent H05 regression-tester role. A main-model verification pass found one
new-file lint defect owned by this role:

```text
F401 `time` imported but unused
tests/test_regression_h05_wait_cancellation.py:5
```

Remove only that unused import from your owned regression test. Do not edit production source,
the spec tester's file, another test, dependencies, or change-loop scripts. Preserve the existing
regression command and manifest unless a truthful correction is required. Then run:

```text
ruff check tests/test_regression_h05_wait_cancellation.py
./.h01-venv-batchstrict/Scripts/python.exe -m unittest -q tests.test_regression_h05_wait_cancellation
```

Before analysis, immediately before the edit, before verification, and before the final report,
reread the complete `../.codex/design_charter.md` and append a dated checkpoint to
`.change-loop/fresh-suite/H05-cancellation/DESIGN_CHARTER_CHECKS.md`. Each checkpoint must state
the contemplated test-only diff, applied charter properties, assumption/tie-breaker, rejected
board/OS/toolchain-specific alternatives, and scope exclusions.

Report exact command results. Do not claim the neutral gate; the main orchestrator will rerun it.
