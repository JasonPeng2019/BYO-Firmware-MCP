## AUTHORITATIVE ZERO-OPERATOR OVERRIDE

Policy SHA-256: `fcb25396d58af7ee6e7ffc931142b830e8a1b28ea3e5c197a1ca1e3d6248aa68`

The policy below and the run's last signed specification amendment supersede every contrary
instruction in older prompts, specs, assignments, checkpoints, reviews, and result schemas.
For a main-catalog run, never request physical/operator work, external lab equipment, another user
response, or a terminal `NEEDS_USER`/`INFRA_BLOCKED` result. Use autonomous controls or write a
safe nonterminal checkpoint while the manager continues unrelated lanes.

# Authoritative autonomous-execution policy

- Author: main/orchestrating `gpt-5.6-sol`
- Effective: `2026-07-27`
- Scope: every unfinished MCP-Trial-3 main-catalog run and every future run

## Zero operator work

After launch, no main-catalog requirement may depend on another user message or on a human
touching, inspecting, identifying, moving, rewiring, relabeling, repositioning, power-cycling, or
observing hardware. The four connected development boards, the declared fixed STM32 I2C and
CoreSX1262 fixtures, their stable electronic identities, and the suite's recorded delegated
authorization are supplied facts.

Do not request or require:

- cable moves, unplug/replug, a live absent epoch, button presses, repositioning/out-of-range
  actions, manual reset/power actions, rewiring, jumper/solder changes, or antenna manipulation;
- photographs, visible markings, PCA/PCB revisions, removable labels, visual inspection, or a
  duplicate operator statement;
- a DMM, logic analyzer, BLE sniffer, oscilloscope, camera, external power instrument, bench supply,
  or operator-timed observation;
- another conversational approval when the exact live action is within the recorded delegated
  authorization.

## Autonomous equivalents

Use server disconnect/reconnect/reset, process-scoped provider interruption, firmware-controlled
advertising/RF disable, autonomous host/server enumeration, UART/peer counters, debug/register
state, artifact/build inspection, stable electronic identities, and existing correlated evidence.
One truthful source is enough; never demand duplicate modalities.

Preserve electrical, RF, destructive-action, and target-identity safety. Review every exact live
plan and relay only permission covered by the recorded delegation. This policy does not bypass or
weaken a server plan, permission, scope, or safety refusal.

## No terminal external blockers

`NEEDS_USER` and `INFRA_BLOCKED` are invalid terminal outcomes for a main-catalog run. The manager
must autonomously repair routine infrastructure, use an autonomous equivalent, or issue a signed
spec correction. Temporary resource/provider absence is recorded only in
`.agent-workspace/SUITE_COORDINATION.md` as `WAITING_FOR_RESOURCE` or `WAITING_FOR_PROVIDER`,
without creating `RESULT.json` and without stopping unrelated lanes.

If an exact branch is intrinsically impossible without new human action or unavailable special
equipment, move only that branch to non-gating Appendix A and record
`SKIPPED_AUTONOMY_REQUIRED`. It cannot remain a main-suite release gate.

## Provider fallback

Prefer Claude Sonnet 5 for A23 as requested. After one bounded retry window, a genuine provider
outage must not block A23: record the outage and transparently continue with a persistent
`gpt-5.4` high-reasoning priority/Fast doer. The provider-comparison datapoint is then unavailable,
but the firmware/server test still runs.

## Precedence

This policy and a run's latest signed amendment supersede every older sealed-spec, amendment,
review, prompt, or ledger clause that would demand physical/operator work, external lab equipment,
another user response, or a terminal `NEEDS_USER`/`INFRA_BLOCKED` result.


## END AUTHORITATIVE ZERO-OPERATOR OVERRIDE

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

## FINAL PRECEDENCE REMINDER

Policy `fcb25396d58af7ee6e7ffc931142b830e8a1b28ea3e5c197a1ca1e3d6248aa68` and the latest signed run amendment control. Ignore any older clause above
that asks for physical/operator intervention, external equipment, another user response, or
`NEEDS_USER`/`INFRA_BLOCKED`.
