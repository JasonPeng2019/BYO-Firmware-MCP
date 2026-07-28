AUTHORIZED LOCAL FIRMWARE VALIDATION: This is a read-only review of a local production-server
repair plan. Do not operate hardware, access external systems, edit files, commit, push, deploy,
flash, or broaden scope.

You are the one independent adversarial plan reviewer. Work only inside the supplied
BYO-Firmware-MCP repository.

Before reviewing:

1. Read `../.codex/design_charter.md` in full.
2. Read `.change-loop/fresh-suite/S11-datasheet-lazy/changes.md`.
3. Read `.change-loop/fresh-suite/S11-datasheet-lazy/plan.md`.
4. Inspect the narrow named production function, its callers, and relevant existing tests.

Review the exact plan SHA-256:
`5450988a2e1e521278ed0bfa130ed15d1620db517c4f56f07fe7837ccbfb1fbf`.

This is diagnostic review, not a replanning loop. Do not write or modify anything. Return:

- the exact SHA reviewed;
- your session/thread identity if available;
- a concise verdict;
- numbered implementation risks and adversarial test targets;
- any charter conflict, especially fabrication, weakened authority, environment specialization,
  arbitrary limits, or edits beyond the verified eager-extraction defect.

Do not demand unrelated refactors. Do not propose changing the plan solely for stylistic
preference. Distinguish a genuine blocking correctness issue from an execution risk the doer and
testers can address under the plan.
