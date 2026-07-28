AUTHORIZED LOCAL FIRMWARE VALIDATION. Resume the exact persistent A23 regression-tester role. This
is a test-ownership correction after the neutral loop; do not edit production source, the plan,
or the spec tester's file.

Read the complete `../.codex/design_charter.md`, the plan, plan review, current neutral report,
your prior messages/logs, and the spec tester's current file read-only.

The main audit found that you violated role ownership by adding
`test_reviewed_partition_accepts_connected_wrapper_within_its_authority` directly to
`tests/test_a23_signed_hex_spec.py` and listing that spec-owned file in your manifest. Correct this
without touching that file:

1. Create a separate tester-owned `tests/test_a23_signed_hex_regression.py`.
2. Put your reviewed-policy success-path regression in that file using self-contained test
   fixtures or stable production imports; do not depend on mutating or owning the spec file.
3. Use the regression to prove connected wrapper evidence survives the complete reviewed-policy
   call only when the exact application and erase authority permit it.
4. Run an isolated Bash/WSL-portable command.
5. Rewrite `state/regression_test_cmd` and `state/regression_tester.manifest` to name only your
   new regression file.

Do not edit `tests/test_a23_signed_hex_spec.py`. Record the required charter checkpoints and
finish with the exact test count and owned-file list.