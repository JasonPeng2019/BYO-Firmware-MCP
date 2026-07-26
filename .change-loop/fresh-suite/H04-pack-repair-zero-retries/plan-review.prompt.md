Authorized local firmware-server validation. The target is limited to this
local BYO-Firmware-MCP repository. This is a host-only, read-only adversarial
plan review; no physical board, remote target, firmware operation, deployment,
commit, push, or flash is in scope.

Act as the one independent adversarial reviewer required before executing the
main-model-authored server repair plan.

Workspace:
`C:\Users\Jason\Documents\Jason\FirmCLI_Tester\Firmware-Test-Manual\MCP-Trial-3\BYO-Firmware-MCP`

Required inputs:

- `../.codex/design_charter.md`
- `.change-loop/fresh-suite/H04-pack-repair-zero-retries/changes.md`
- `.change-loop/fresh-suite/H04-pack-repair-zero-retries/plan.md`
- relevant existing production source needed to verify the plan's claims

Rules:

1. Read `../.codex/design_charter.md` before reviewing, again after tracing the
   relevant source, and again before finalizing. Record all three checkpoints.
2. Verify the exact plan SHA-256 is
   `8bb7371fd01f2bc751a78a18fffd390f638ec434b9dbd99656ab448df4504da1`.
3. Review only; do not edit the plan, request, source, tests, configuration, Git
   state, or any other file.
4. Do not regenerate or replace the plan. Identify numbered execution risks,
   missing test targets, compatibility hazards, or charter conflicts that the
   implementation doer and testers must account for.
5. The plan is allowed to proceed unless a concrete contradiction makes it
   unimplementable. This is one review, not a review/replanning loop.
6. Write exactly one file:
   `.change-loop/fresh-suite/H04-pack-repair-zero-retries/plan-review.md`.
   It must contain the exact plain line:
   `Plan SHA-256: 8bb7371fd01f2bc751a78a18fffd390f638ec434b9dbd99656ab448df4504da1`
   plus your reviewer session identity/model, verdict, three charter
   checkpoints, and numbered risks/test targets.
7. Do not add adversarial-input hardening, arbitrary limits, vendor/board/OS
   special cases, or hardware operations.

Finish after writing and rereading that one review file.
