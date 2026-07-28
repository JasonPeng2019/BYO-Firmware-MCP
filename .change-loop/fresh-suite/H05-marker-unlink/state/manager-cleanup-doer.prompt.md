Authorized local firmware validation. This is a host-only repository-quality follow-up in the
named local BYO-Firmware-MCP workspace. No board, hardware, remote system, or third-party target is
in scope.

Resume the persistent H05 marker-unlink **repair doer** role. The neutral behavior gate is green,
but manager verification found one newly introduced targeted lint issue in your production edit:
`ruff check --select PIE790 src/pyocd_debug_mcp/adapters/swd_process.py` reports the now-unnecessary
`pass` immediately after the new conditional bare `raise` in `_WorkerClient.close`.

Do exactly this, sequentially:

1. Reread the complete `../.codex/design_charter.md`.
2. Append a dated **Doer — manager cleanup follow-up** entry to
   `.change-loop/fresh-suite/H05-marker-unlink/DESIGN_CHARTER_CHECKS.md`. State the exact one-line
   cleanup contemplated and how it preserves honest marker-cleanup failure propagation, ordinary
   graceful-close diagnostic suppression, portability, and the plan's strict scope.
3. Remove only that unnecessary `pass`. Do not reformat the production file or change any other
   production line.
4. Verify:
   - `.venv/Scripts/ruff.exe check --select PIE790 src/pyocd_debug_mcp/adapters/swd_process.py`
   - `git diff --check -- src/pyocd_debug_mcp/adapters/swd_process.py`
   - `PYTHONPATH=src ./.venv/Scripts/python.exe -m unittest tests/test_h05_marker_unlink_spec.py`
   - `./.h01-venv-batchstrict/Scripts/python.exe -m unittest -q tests.test_regression_h05_marker_unlink`
5. Reread the complete charter, append a dated pre-final follow-up checkpoint, and report exact
   commands/results.

Edit only `src/pyocd_debug_mcp/adapters/swd_process.py` and the named charter checkpoint log. Do
not edit tests, commands, manifests, plans, requests, docs, dependencies, or unrelated dirty files.
Do not operate hardware. Do not commit.
