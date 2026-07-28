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

Authorized local firmware validation. Work only in this BYO-Firmware-MCP repository. This is
host-only adversarial test work and authorizes no hardware action.

Resume the existing persistent A20 spec-tester role after the implementation doer corrected the
two accepted post-gate findings recorded in the final section of
`.change-loop/fresh-suite/A20-sleeping-symbol-read/plan-review.md`.

Reread the complete `../.codex/design_charter.md` before analysis, before editing, before
verification, and before the final verdict; verify SHA-256
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb` and append the required
stage entries to `.change-loop/fresh-suite/A20-sleeping-symbol-read/DESIGN_CHARTER_CHECKS.md`.

Adversarially extend only your owned
`tests/test_a20_sleeping_symbol_read_spec.py` so the neutral spec command proves:

1. A non-`Exception` primary failure (for example `KeyboardInterrupt`) raised by the scalar read
   after an inserted halt still causes exactly one `resume`, then the exact primary object/type is
   re-raised when restoration succeeds.
2. If useful to close the precise contract, a non-`Exception` read plus a non-`Exception`
   restoration failure reports both types/messages and chains from the primary.
3. The published help explicitly identifies `board_id`, `symbol`, `width`, and `elf_artifact`, and
   contains the concrete `read_memory_symbol(...)` example in addition to the existing lifecycle,
   return, honest-failure, and recovery phrases.

Do not edit production source, the regression tester's file/manifest/command, the plan, request,
or unrelated tests. Keep the existing spec command unless the owned file path truly changes.
Run the exact recorded spec command under Bash, Ruff on your owned test, and `git diff --check`.
Report the exact tests added/changed and actual results. Do not commit/push/deploy.

## FINAL PRECEDENCE REMINDER

Policy `fcb25396d58af7ee6e7ffc931142b830e8a1b28ea3e5c197a1ca1e3d6248aa68` and the latest signed run amendment control. Ignore any older clause above
that asks for physical/operator intervention, external equipment, another user response, or
`NEEDS_USER`/`INFRA_BLOCKED`.
