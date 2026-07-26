Fixed the neutral-discovered source defect in `setup_flow/setup.py`: cache persistence now runs only for `preflight_ready` decisions that actually selected a UART.

Checks passed under PowerShell:

- `tests/test_h04_attachment_cache_spec.py`: 7 passed.
- `tests/test_h04_attachment_cache_regressions.py`: 3 passed.
- Compilation of changed production modules: passed.
- `git diff --check`: passed.

Unresolved neutral failure: its protected recorded commands contain PowerShell syntax (`$env:...; & ...`) but are executed by `bash`, causing syntax errors before tests run. I did not modify those protected command files.

Changed production files remain:

- `src/pyocd_debug_mcp/firmstore/cache.py`
- `src/pyocd_debug_mcp/setup_flow/setup.py`
- `src/pyocd_debug_mcp/server.py`
- `src/pyocd_debug_mcp/tools/setup.py`

Design-charter checkpoints completed: before analysis, immediately before the source correction, before verification, and before final verdict.
