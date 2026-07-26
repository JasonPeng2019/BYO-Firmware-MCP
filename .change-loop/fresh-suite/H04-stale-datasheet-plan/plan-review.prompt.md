Authorized local firmware-server validation. Scope is this local `BYO-Firmware-MCP` repository and
the host-only H04 repair plan. No remote or third-party target is in scope. Do not operate hardware.

Act as the one independent, read-only adversarial reviewer required by the change-loop skill.
Review exactly:

`.change-loop/fresh-suite/H04-stale-datasheet-plan/plan.md`

Its validated SHA-256 is:

`6c0d8e607e5fa1eaead62f35cbfc5054b911876279c9113a16083e91f907e4be`

Requirements:

1. Read `.codex/design_charter.md` from the parent `MCP-Trial-3` workspace before reviewing. Read it
   again after tracing the plan against code, and once more before finalizing.
2. Read the change request, main plan, and only the production/test files needed to check that the
   stated wiring, scope, preserved contracts, and automated targets are accurate.
3. Do not edit the plan, source, tests, configuration, Git state, or any existing file.
4. Do not author a replacement plan. This is a one-time risk/test-target review, not a replanning
   loop.
5. Write exactly one new file:
   `.change-loop/fresh-suite/H04-stale-datasheet-plan/plan-review.md`
6. That file must state the exact reviewed plan SHA, your model/session identity, three recorded
   design-charter checkpoints, a `PROCEED` or `BLOCK` verdict, and numbered execution risks/test
   targets. `BLOCK` is only for a genuine plan defect that makes safe implementation impossible;
   otherwise use `PROCEED` and give the risks to the doer/testers.
7. Check especially:
   - whether `artifact_binding_field="datasheet_path"` plus `(".pdf",)` really reuses the current
     generic mechanism for both primary and paired actions;
   - whether stale refusal happens before workflow/hardware action and without consuming execution;
   - whether the plan overstates what existing code guarantees;
   - unchanged-PDF success, wrong/correct applicability, plan replacement, and unrelated artifact
     regression targets;
   - charter compliance: simplicity, generality, correctness guard rather than hostile-input
     hardening, no arbitrary limit.

Do not commit, push, deploy, flash, erase, reset, or use hardware.
