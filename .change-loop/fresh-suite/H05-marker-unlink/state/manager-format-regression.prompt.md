Authorized local firmware validation. This is a host-only repository-quality follow-up in the
named local BYO-Firmware-MCP workspace. No board, hardware, remote system, or third-party target is
in scope.

Resume the persistent H05 marker-unlink **regression tester** role. Manager verification found that
your owned `tests/test_regression_h05_marker_unlink.py` passes Ruff lint and its focused tests, but
`ruff format --check` reports that this file would be reformatted.

Do exactly this, sequentially:

1. Reread the complete `../.codex/design_charter.md`.
2. Append a dated **Regression tester — manager format follow-up** entry to
   `.change-loop/fresh-suite/H05-marker-unlink/DESIGN_CHARTER_CHECKS.md`. State that only mechanical
   formatting of your owned test is contemplated; production behavior, assertions, commands,
   manifests, hardware, and unrelated files remain excluded.
3. Run `.venv/Scripts/ruff.exe format tests/test_regression_h05_marker_unlink.py`.
4. Verify:
   - `.venv/Scripts/ruff.exe check tests/test_regression_h05_marker_unlink.py`
   - `.venv/Scripts/ruff.exe format --check tests/test_regression_h05_marker_unlink.py`
   - the exact command recorded in
     `.change-loop/fresh-suite/H05-marker-unlink/state/regression_test_cmd`
5. Confirm the manifest still names only `tests/test_regression_h05_marker_unlink.py` and do not
   alter the recorded command unless it is broken.
6. Reread the complete charter, append a dated pre-final follow-up checkpoint, and report the exact
   results.

Edit only your owned regression test and the named charter checkpoint log. Do not edit production
source, the spec tester's file, any plan, request, manifest snapshot, or unrelated dirty file. Do
not operate hardware. Do not commit.
