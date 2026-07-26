# Main acceptance review — REJECTED after third resumed neutral gate

The behavioral gate and independent real-artifact oracle are green. The focused static acceptance
check required by the existing plan is not yet clean for the lines introduced by this H04 repair.
This is a narrow implementation/test-neatness issue, not a reason for a repository-wide cleanup.

## Newly introduced focused Ruff findings

Running the repository's normal Ruff configuration against the H04 production and tester-owned
files, then restricting the result to this repair's added lines, produced:

```text
src/pyocd_debug_mcp/server.py:3354 RUF023 _ResolvedGenericSetupSupport.__slots__ is not sorted
src/pyocd_debug_mcp/setup_flow/datasheet_evidence.py:137 I001 local pypdf import is not formatted
tests/test_h04_datasheet_binding_spec.py:16 I001 import block is not formatted
tests/test_h04_datasheet_binding_spec.py:286 SIM117 nested context managers
tests/test_h04_datasheet_binding_spec.py:325 SIM117 nested context managers
tests/test_h04_datasheet_regressions.py:14 I001 import block is not formatted
```

The newly added `DatasheetIdentityTerm` import also leaves
`setup_flow/device_support.py`'s import block nonconforming. Fix that import placement while
touching only the H04-owned import addition; do not sweep unrelated baseline findings.

The new comment above the verified-family placeholder branch still says the convention is for a
family/subfamily “ending in a digit,” although the accepted generic behavior now intentionally
supports verified letter-ending families such as `LPC55S`. Correct that stale statement.

Finally, `DeviceSupportCandidate.from_verified_pack()` parses the same PDSC ancestry twice—once for
the candidate-ID material and again for the stored tuple. Compute it once and reuse it. This keeps
the exact same authority semantics while satisfying the charter's simplicity/neatness standard.

## Focused type-check classification

A dependency-complete `basedpyright --level error` run reports one H04-test-only error at
`test_h04_datasheet_binding_spec.py:292`: its structural fake `Candidate` is not assignable to the
concrete `DeviceSupportAuthority` parameter. Repair the test typing without weakening or bypassing
the assertion. The production diagnostic at `device_support.py:528` predates this H04 repair and is
outside scope.

## Required next iteration

- The persistent doer may edit only the three narrow production points above.
- The persistent spec/regression testers may edit only their own manifested tests.
- Every role must reread the complete `../.codex/design_charter.md` at the checkpoints required by
  its prompt.
- Run the ordinary focused Ruff command and the dependency-complete focused BasedPyright command;
  classify rather than sweep unrelated pre-existing findings.
- Preserve every behavioral contract and keep the real STM32/nRF controls green.

This is implementation/test cleanup under the existing CL-001/CL-003 plan, not a plan mistake. Do
not amend or re-review the plan. Resume the same persistent roles sequentially and rerun the neutral
gate.
