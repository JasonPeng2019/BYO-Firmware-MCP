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

AUTHORIZED LOCAL FIRMWARE VALIDATION: This is a read-only review of a local production-server
repair plan. Do not operate hardware, access external systems, edit files, commit, push, deploy,
flash, or broaden scope.

You are the one independent adversarial plan reviewer. Work only inside the supplied
BYO-Firmware-MCP repository and its parent MCP-Trial-3 policy files.

Before reviewing:

1. Read `../.codex/design_charter.md` in full.
2. Read `.change-loop/fresh-suite/A23-signed-hex/changes.md`.
3. Read `.change-loop/fresh-suite/A23-signed-hex/plan.md`.
4. Inspect the narrow build-evidence parser, both runtime safety-policy callers, and relevant
   existing tests.

Review the exact plan SHA-256:
`112f77f7d2e524bd540f5a9cd1779fa429bda1ecb7ca7fc2c1958f1470a1b42c`.

This is diagnostic review, not a replanning loop. Do not write or modify anything. Return:

- the exact SHA independently reviewed;
- your session/thread identity if available;
- a concise APPROVE or BLOCK verdict;
- numbered implementation risks and adversarial test targets;
- any charter conflict, especially loss of wrong-artifact mistake guards, vendor/bootloader
  specialization, guessed memory authority, or bypass of reviewed/static or generic physical
  safety-map enforcement.

Pay particular attention to whether the proposed connected-component rule is defined
deterministically, admits the observed prefix/suffix relationship without an arbitrary tolerance,
rejects disjoint supplemental content, handles only the exact `0x00`/`0xFF` fill equivalence,
preserves meaningful-overlap and meaningful-completeness checks, and leaves every actual HEX and
erase range subject to existing policy-level authority.

Do not demand unrelated refactors. Do not propose changing the plan solely for stylistic
preference. Distinguish a genuine blocking correctness issue from an execution risk the doer and
testers can address under the plan.

## FINAL PRECEDENCE REMINDER

Policy `fcb25396d58af7ee6e7ffc931142b830e8a1b28ea3e5c197a1ca1e3d6248aa68` and the latest signed run amendment control. Ignore any older clause above
that asks for physical/operator intervention, external equipment, another user response, or
`NEEDS_USER`/`INFRA_BLOCKED`.
