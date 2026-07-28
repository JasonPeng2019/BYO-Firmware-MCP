Authorized local firmware validation. Targets are limited to the named local workspace and the
user-owned development boards explicitly assigned by the test. Follow every declared hardware
plan and permission gate. No remote or third-party target is in scope.

You are the one-time independent adversarial reviewer for a verified production-server repair
plan. Work only in this BYO-Firmware-MCP repository. This turn is source-read-only: do not run the
server, operate hardware, change production source, change tests, modify the plan, amend the
request, commit, push, deploy, or replan. You may write only these runtime evidence files:

1. `.change-loop/fresh-suite/A20-sleeping-symbol-read/plan-review.md`
2. `.change-loop/fresh-suite/A20-sleeping-symbol-read/DESIGN_CHARTER_CHECKS.md`

Before reviewing, read the authoritative `../.codex/design_charter.md` and verify its SHA-256 is
`03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`.
Then read:

- `.change-loop/fresh-suite/A20-sleeping-symbol-read/changes.md`
- `.change-loop/fresh-suite/A20-sleeping-symbol-read/plan.md`
- the relevant implementation, wiring, and existing tests
- the current Git diff/status, so pre-existing repairs are not mistaken for this plan's work

Review exactly plan SHA-256
`a974e693dd8b16d03d993b27b0c16f1113891482de58e67160608b2cc6da0a07`.
Do not edit that plan even if you find a weakness. Challenge it against the request and charter:
correctness, failure cleanup, observable contracts, simplicity, generality, scope containment,
usability, compatibility, testability, and preservation of both primary and restoration failures.

Write `plan-review.md` with:

- the exact reviewed plan SHA-256;
- reviewer identity label `A20-plan-adversarial-reviewer-001`;
- model `gpt-5.6-terra`, reasoning `medium`, service tier `priority/Fast`;
- a concise overall assessment;
- numbered execution risks and corresponding objective test targets;
- any nonblocking clarification the implementation roles must observe; and
- explicit confirmation that you did not edit the plan, production source, or tests.

This is a single diagnostic review, not a verdict loop. Do not demand another plan review or
generate a replacement plan. The main-authored plan remains the execution contract unless later
execution proves a genuine plan mistake.

Reread `../.codex/design_charter.md` immediately before writing the report. Append a short
`Plan adversarial review — A20-plan-adversarial-reviewer-001` section to
`DESIGN_CHARTER_CHECKS.md` recording the verified charter hash and how your risks preserve
correctness, simplicity, generalizability, neatness/usability, dynamism, and the stated trust
boundary.
