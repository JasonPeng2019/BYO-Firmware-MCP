Authorized local firmware validation. Scope is the local `BYO-Firmware-MCP` repository and a
host-only artifact-manifest line-ending repair. No board or hardware operation is in scope.

Act as the one independent, read-only adversarial reviewer of the main-authored H03 server-repair
plan. Work only in this repository. Do not edit production source, tests, the plan, or any file
except `.change-loop/fresh-suite/H03/plan-review.md`. Do not operate hardware and do not commit.

At the start, read `.change-loop/fresh-suite/H03/plan.md`,
`.change-loop/fresh-suite/H03/changes.md`, and `.change-loop/design_charter.md`. Inspect the
relevant production source and existing tests/diff only as needed to challenge plan correctness,
scope, preservation constraints, and objective testability. Before writing the verdict, reread
`.change-loop/design_charter.md` and explicitly confirm that second check in the review.

Review exactly plan SHA-256
`e1e8ecdc0dd5e59c5afaeaf84f681e1f8df014b59d8b044bab02a5f5c7621e10`.
Write `.change-loop/fresh-suite/H03/plan-review.md` containing:

- the reviewed SHA-256;
- your Codex thread/session ID if available (otherwise `pending-controller-extraction`);
- numbered execution risks and tester targets;
- any BLOCK condition only if implementation cannot safely begin as written;
- a final `PROCEED` or `BLOCK` verdict.

This is one review, not a replanning loop. Do not rewrite or regenerate the plan.
