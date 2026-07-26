# H04 plan review

Plan SHA-256: 6c0d8e607e5fa1eaead62f35cbfc5054b911876279c9113a16083e91f907e4be

Reviewer identity: persistent external Codex session
`019f9be8-3975-76a3-b4bf-59293d5ae9ca`, model `gpt-5.6-terra`, reasoning `medium`;
local host-only review completed on 2026-07-25 America/Los_Angeles.

## Design-charter checkpoints

1. **Before review:** Read `../.codex/design_charter.md`; confirmed that stale verified assumptions are correctness failures and that the remedy must not add hostile-input hardening, paternalism, or arbitrary limits.
2. **After code trace:** Read the charter again after tracing `PlanDefinition`, `PlanEngine.enforce`, guarded dispatch, and the paired setup wiring; confirmed that declaratively reusing the generic digest binding is the simplest general correctness guard.
3. **Before finalizing:** Read the charter a third time; confirmed the review's test targets preserve arbitrary board/vendor/OS behavior and do not call for a special case, copy subsystem, or new cap.

## Verdict

**PROCEED**

The stated one-definition change is sufficient. `definition_for_action("board_fix_setup")` returns the `board_setup` definition, so setting `artifact_binding_field="datasheet_path"` and `artifact_binding_suffixes=(".pdf",)` uses the existing binding for both the primary and paired calls. On execution, the engine verifies that binding before scope/precondition work and before decrementing allowance or consuming permission; an artifact refusal invalidates the shared plan and relocks its primary and paired actions. The guarded dispatcher runs this enforcement before either setup handler enters its workflow.

## Execution risks and test targets

1. **Declarative wiring:** Assert the `board_setup` definition exposes exactly `artifact_binding_field="datasheet_path"` and `artifact_binding_suffixes=(".pdf",)`, and that the accepted public payload contains the existing byte-binding/replacement-plan reminder. Do not add a setup-specific binding implementation.
2. **Primary stale refusal ordering:** Accept a plan for a valid local PDF, alter one byte, then invoke `board_setup`. Assert `plan/artifact-changed`, inactive/relocked primary and paired actions, unchanged call/permission allowance, and no calls into `workflow.begin_plan`, `workflow.board_setup`, or profile/safety persistence.
3. **Paired stale refusal ordering:** Start an unchanged primary call that legitimately leaves the paired repair allowance open, alter the PDF, then invoke `board_fix_setup`. Assert the same artifact-change refusal occurs before `workflow.board_fix_setup`, completion/ready authority is absent, and the replacement plan can be accepted for the new stable bytes.
4. **Unchanged-PDF control:** Exercise successful primary and paired setup with byte-identical PDF contents. Preserve exact action parameters, `max_calls=1`, `max_calls_buffer=0`, one-time/full-session permission behavior, the one paired allowance, and normal research/continuation routing.
5. **Applicability controls:** Keep the existing wrong-family refusal and correct-family acceptance green; byte binding is an identity/freshness guard, not a substitute for the established applicability proof.
6. **Shared-mechanism regression:** Run the generic artifact-bound plan coverage (including changed, unreadable, and resolved-path replacement behavior where already covered) plus adjacent strict MCP/setup-plan regressions, so the definition-only change neither changes unrelated bindings nor overstates engine guarantees.
7. **Replacement semantics:** Verify a stale invalidation does not permanently block progress: a newly submitted plan binds the current unchanged PDF bytes, unlocks both actions, and is distinct from the invalidated plan. This is the required recovery path, not an additional restriction.

## Mandatory role charter checkpoints

The doer, spec tester, and regression tester must each read `../.codex/design_charter.md` before
acting, reread it after tracing or implementing its distinct milestone, and reread it before its
final report. Each role must append a dated entry naming those stages and its charter decisions to
`.change-loop/fresh-suite/H04-stale-datasheet-plan/DESIGN_CHARTER_CHECKS.md`. This log is a repair
artifact, not a tester-owned test or gate-control file.
