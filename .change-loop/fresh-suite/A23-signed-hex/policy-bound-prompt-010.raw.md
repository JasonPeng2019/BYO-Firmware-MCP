AUTHORIZED LOCAL FIRMWARE VALIDATION. Resume the exact persistent A23 spec-tester role. This is a
test-ownership correction after the neutral loop; do not edit production source, the plan, or any
other tester's files.

Read the complete `../.codex/design_charter.md`, the plan, plan review, current neutral report,
your prior messages/logs, and the current
`tests/test_a23_signed_hex_spec.py`.

The main audit found that the regression tester violated role ownership by adding
`test_reviewed_partition_accepts_connected_wrapper_within_its_authority` directly to your
spec-owned file and then listing your file in its manifest. Reclaim your spec-owned file:

1. Remove only the regression-authored
   `test_reviewed_partition_accepts_connected_wrapper_within_its_authority` method from
   `tests/test_a23_signed_hex_spec.py`.
2. Preserve all tests you authored, including your later generic in-physical acceptance and exact
   allocation coverage.
3. Run your isolated suite with a Bash/WSL-portable command.
4. Rewrite `state/spec_test_cmd` and `state/spec_tester.manifest` for only your owned file.
5. Do not edit the regression manifest/command or create the regression tester's replacement file.

Record the required charter checkpoints and finish with the exact test count and owned-file list.