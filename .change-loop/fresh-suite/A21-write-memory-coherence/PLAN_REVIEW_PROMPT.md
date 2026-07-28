Authorized local firmware-server validation. The production target is limited to this local
`BYO-Firmware-MCP` repository. Hardware is not used in this review. No remote or third-party target
is in scope.

You are the one-time independent adversarial reviewer for a verified production-server repair
plan. Work only in this BYO-Firmware-MCP repository. This turn is source-read-only except for the
two explicitly permitted runtime review files below. Do not run the server, operate hardware,
change production source, change tests, edit or replace the plan/request, commit, push, deploy, or
replan.

Before reviewing, read the complete authoritative `../.codex/design_charter.md` and verify its
SHA-256 is `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`.
Then read:

- `.change-loop/fresh-suite/A21-write-memory-coherence/changes.md`
- `.change-loop/fresh-suite/A21-write-memory-coherence/plan.md`
- the relevant implementation, production wiring, public guidance, and existing tests
- current Git status and diff, so the runtime files and pre-existing committed repairs are not
  mistaken for implementation work

Review exactly plan SHA-256
`d2a1d29a7b8932133fa959bf35733906086cee3121240aa0594e15c0615fc626`.
Do not edit it. Challenge it against the request and charter: correctness/no-fabrication,
execution-state preservation, primary-plus-restoration error semantics, cancellation-class
failures, exact readback, compatibility, simplicity, generality, scope containment, usability,
testability, and preservation of pre-I/O refusals.

You may write only:

1. `.change-loop/fresh-suite/A21-write-memory-coherence/plan-review.md`
2. `.change-loop/fresh-suite/A21-write-memory-coherence/DESIGN_CHARTER_CHECKS.md`

Write `plan-review.md` with:

- the exact reviewed plan SHA-256;
- reviewer label `A21-write-memory-plan-reviewer-001`;
- model `gpt-5.6-terra`, reasoning `medium`, service tier `priority/Fast`;
- a concise overall assessment;
- numbered execution risks paired with objective test targets;
- any nonblocking clarification implementation roles must observe; and
- explicit confirmation that you did not edit the plan, production source, or tests.

This is one diagnostic review, not a verdict loop. Do not demand a second review or generate a
replacement plan. The main-authored plan remains the execution contract unless later execution
proves a genuine plan mistake.

Reread the complete charter immediately before writing. Append a short
`Plan adversarial review — A21-write-memory-plan-reviewer-001` section to
`DESIGN_CHARTER_CHECKS.md`, recording the verified charter hash and how the risks preserve
correctness, simplicity, generalizability, neatness/usability, dynamism, and the trust boundary.
