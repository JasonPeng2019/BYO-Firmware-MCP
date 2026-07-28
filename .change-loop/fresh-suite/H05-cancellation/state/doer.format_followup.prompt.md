Authorized local firmware-server validation. This is host-only work in the named local
`BYO-Firmware-MCP` repository. No board, remote target, or third-party system is in scope.

Resume the exact persistent H05 implementation-doer role. Main-model verification found that the
sole production file changed by CL-001 does not satisfy the repository formatter, apparently
because the earlier edit left inconsistent line endings:

```text
uvx ruff format --check src/pyocd_debug_mcp/tools/misc.py
File would be reformatted
```

Run the repository formatter on only `src/pyocd_debug_mcp/tools/misc.py`. Do not make any semantic
change and do not edit tests, manifests, gate commands, dependencies, or another production file.
Inspect the pre/post diff and confirm that CL-001 still has exactly the reviewed behavior.

Then run:

```text
uvx ruff check src/pyocd_debug_mcp/tools/misc.py
uvx ruff format --check src/pyocd_debug_mcp/tools/misc.py
./.h01-venv-batchstrict/Scripts/python.exe -m unittest -q tests.test_server_trust_model_round_1 tests.test_h01_strict_mcp_boundary
```

Before analysis, immediately before formatting, before verification, and before the final report,
reread the complete `../.codex/design_charter.md` and append a dated checkpoint to
`.change-loop/fresh-suite/H05-cancellation/DESIGN_CHARTER_CHECKS.md`. Each checkpoint must state
the contemplated formatting-only diff, applied charter properties, assumption/tie-breaker,
rejected board/OS/toolchain-specific alternatives, and scope exclusions.

Report exact command results. Do not claim the neutral gate; the main orchestrator will rerun it.
