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

Authorized local firmware-server validation. This review is limited to the named local
BYO-Firmware-MCP workspace and the A21 production repair. No remote or third-party target is in
scope.

You are the one-time independent adversarial reviewer of the implemented A21 `write_memory`
server repair. Use `gpt-5.6-terra` at medium reasoning on priority/Fast as configured by the
controller. This turn is strictly read-only: do not edit any file, run the server, operate
hardware, commit, push, deploy, replan, or launch another agent.

Before judging the diff:

1. Read the complete authoritative `../.codex/design_charter.md` and verify its SHA-256 is
   `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`.
2. Read:
   - `.change-loop/fresh-suite/A21-write-memory-coherence/changes.md`
   - `.change-loop/fresh-suite/A21-write-memory-coherence/plan.md`
   - `.change-loop/fresh-suite/A21-write-memory-coherence/plan-review.md`
   - `.change-loop/fresh-suite/A21-write-memory-coherence/state/test_report.md`
   - `.change-loop/fresh-suite/A21-write-memory-coherence/MAIN_VERIFICATION_FINDINGS.md`
   - `src/pyocd_debug_mcp/tools/memory.py`
   - the A21 help/wiring hunks in `src/pyocd_debug_mcp/server.py` and
     `src/pyocd_debug_mcp/guardrails/plan_defs.py`
   - `tests/test_a21_write_memory_coherence_spec.py`
   - `tests/test_regression_a21_write_memory_coherence.py`
   - the exact changed hunks in `tests/test_a20_sleeping_symbol_read_spec.py` and
     `tests/test_h00_repository_contract.py`.
3. Inspect Git status and the exact production/test diff. The accepted baseline is clean HEAD
   `db3fb8660c8186d351508050bf622a6aaf0b50fc`; do not attribute runtime evidence files to
   production behavior.
4. Treat these main-model verification results as evidence, but challenge their adequacy:
   - neutral gate: 8 spec tests + 12 subtests and 13 regression/adjacent tests + 6 subtests pass;
   - memory/blast-radius selection: 81 passed, 1 skipped, 45 subtests;
   - H00 clean-candidate transaction: 1 passed in 601.59 seconds;
   - the rest of the full suite: 356 passed, 4 skipped, 1 deselected, 186 subtests;
   - Ruff: pass; Pyright with the repository venv active: 0 errors; `git diff --check`: pass.

Adversarially trace every A21 execution path against plan SHA-256
`d2a1d29a7b8932133fa959bf35733906086cee3121240aa0594e15c0615fc626`.
In particular, check lifecycle ordering for already-HALTED and every non-HALTED state; state,
halt, write, verification-read, mismatch, cancellation-class, and resume failures; dual-failure
truthfulness and exception causality; success-event timing; all `MemoryToolServices` construction
sites; symbol and justified raw routes; pre-I/O refusals and containment; public parameters,
plan/budget/permission compatibility; and whether all published FastMCP/plan help meets the
charter. Challenge state normalization, provider return types, accidental resume, missing
readback, post-resume durability overclaim, test weakening, and the smallest plausible regression
surface. Reject board-, MCU-, firmware-, OS-, probe-, or fixture-specific behavior; retries or
resets; broad refactors; and new paternalistic or hostile-input guards.

Return a concise final report containing:

- verified charter and plan SHA-256 values;
- reviewer label `A21-write-memory-diff-reviewer-001`;
- `VERDICT: ACCEPT` only if there is no actionable correctness/scope/usability/test-integrity
  issue, otherwise `VERDICT: NEEDS_FIX`;
- numbered findings separating actionable defects from nonblocking residual risks;
- exact file/line or test evidence for every finding;
- an explicit statement that you made no edits and performed no hardware action.

Do not ask for another review. The main model will independently decide whether any finding
requires resuming the existing repair roles.

## FINAL PRECEDENCE REMINDER

Policy `fcb25396d58af7ee6e7ffc931142b830e8a1b28ea3e5c197a1ca1e3d6248aa68` and the latest signed run amendment control. Ignore any older clause above
that asks for physical/operator intervention, external equipment, another user response, or
`NEEDS_USER`/`INFRA_BLOCKED`.
