Authorized local firmware-server validation. This is host-only work in the named local
`BYO-Firmware-MCP` repository. No board, remote target, or third-party system is in scope.

Resume the exact persistent H05 spec-tester role. Main-model verification found that this role's
owned test is Ruff-clean but not formatter-clean:

```text
uvx ruff format --check tests/test_h05_wait_cancellation_spec.py
File would be reformatted
```

Run the repository formatter on only `tests/test_h05_wait_cancellation_spec.py`. Do not change
test meaning, production source, the regression tester's file, another test, dependencies, or
change-loop scripts. Preserve the existing spec command and manifest.

Then run:

```text
uvx ruff check tests/test_h05_wait_cancellation_spec.py
uvx ruff format --check tests/test_h05_wait_cancellation_spec.py
./.h01-venv-batchstrict/Scripts/python.exe -m unittest -q tests.test_h05_wait_cancellation_spec
```

Before analysis, immediately before formatting, before verification, and before the final report,
reread the complete `../.codex/design_charter.md` and append a dated checkpoint to
`.change-loop/fresh-suite/H05-cancellation/DESIGN_CHARTER_CHECKS.md`. Each checkpoint must state
the contemplated formatting-only diff, applied charter properties, assumption/tie-breaker,
rejected board/OS/toolchain-specific alternatives, and scope exclusions.

Report exact command results. Do not claim the neutral gate; the main orchestrator will rerun it.
