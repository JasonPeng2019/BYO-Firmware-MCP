Authorized local firmware validation. This is a host-only repository-quality follow-up in the
named local BYO-Firmware-MCP workspace. No board, hardware, remote system, or third-party target is
in scope.

Resume the persistent H05 marker-unlink **spec tester** role. Manager verification found that your
owned `tests/test_h05_marker_unlink_spec.py` passes Ruff lint and its focused tests, but
`ruff format --check` reports that this file would be reformatted.

Do exactly this, sequentially:

1. Reread the complete `../.codex/design_charter.md`.
2. Append a dated **Spec tester — manager format follow-up** entry to
   `.change-loop/fresh-suite/H05-marker-unlink/DESIGN_CHARTER_CHECKS.md`. State that only mechanical
   formatting of your owned test is contemplated; production behavior, assertions, commands,
   manifests, hardware, and unrelated files remain excluded.
3. Run `.venv/Scripts/ruff.exe format tests/test_h05_marker_unlink_spec.py`.
4. Verify:
   - `.venv/Scripts/ruff.exe check tests/test_h05_marker_unlink_spec.py`
   - `.venv/Scripts/ruff.exe format --check tests/test_h05_marker_unlink_spec.py`
   - the exact command recorded in
     `.change-loop/fresh-suite/H05-marker-unlink/state/spec_test_cmd`
5. Confirm the manifest still names only `tests/test_h05_marker_unlink_spec.py` and do not alter
   the recorded command unless it is broken.
6. Reread the complete charter, append a dated pre-final follow-up checkpoint, and report the exact
   results.

Edit only your owned spec test and the named charter checkpoint log. Do not edit production source,
the regression tester's file, any plan, request, manifest snapshot, or unrelated dirty file. Do
not operate hardware. Do not commit.
