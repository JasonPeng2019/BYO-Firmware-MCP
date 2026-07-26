# H01 null-string repair design-charter checkpoints

Governing file: `../.codex/design_charter.md` from the MCP-Trial-3 root.

## Current high-level main model

1. **After verified defect classification — PASS.** Reread the complete charter after preserving
   the A12 live evidence and reproducing the defect through the installed FastMCP `Tool.run()`
   boundary. Treating caller string `"null"` as actual NULL is an honesty/correctness defect. The
   repair must preserve exact caller meaning rather than weaken the H01 oracle.
2. **Between request and plan — PASS.** Reread the complete charter before authoring the plan.
   Selected the one existing owner, generated plan registration, rather than changing the plan
   engine, every handler, the SDK dependency, or any board/toolchain path. The plan is general over
   declarative text fields, keeps normal non-text compatibility, introduces no arbitrary limit or
   hostile-input defense, and excludes all accepted H00 work.
3. **Independent high-level plan review — PASS.** The `gpt-5.6-sol` reviewer
   `/root/h01_null_plan_review` reread the complete charter and passed exact plan SHA-256
   `21b059867ac578caf99acb7f4410e47494ecc8aeaa1317c16dfa8d6051801cc8`.
   Its eight execution risks/test targets are recorded in `plan-review.md` SHA-256
   `2ccdf602e7b5881ab68525022a82ba01679ad6abefbe8195642f52ee53c511a6`
   after the same reviewer added the controller-required standalone plan-hash line.
4. **Immediately before implementation deployment — PASS.** Reread the complete charter
   (SHA-256 `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`)
   after plan review. Confirmed the plan/review hashes, exact pre-implementation H00 file
   baselines, isolated runtime, sequential role order, host-only/no-board scope, no concurrent
   H01 change-loop process, no commit/deploy/flash authorization, and doer ownership limited to
   `src/pyocd_debug_mcp/tools/plans.py`.

## Required role checks

The doer, spec tester, and regression tester must each reread the complete charter:

- before first analysis;
- immediately before editing;
- between distinct production/test features;
- before verification; and
- before final verdict.

Each role must record those checkpoints in its final message. The main model will append the
independent plan-review, immediately-pre-implementation, between-feature, pre-verification,
post-risky-diff, and final-acceptance checks here while supervising the loop.
## 2026-07-24 17:57 PDT — pre-retry sandbox escalation

- Reread `.codex/design_charter.md` after the first controller stopped without any production or test edits.
- The doer and spec tester both proved that the Windows `workspace-write` Codex sandbox was enforced read-only: each attempted its role-owned write and recorded the rejection; the target production diff remained empty and no tester gate files were created.
- The change-loop skill explicitly prescribes `--sandbox danger-full-access --ignore-user-config` when the Windows workspace sandbox prevents required writes. The retry therefore removes only that broken sandbox layer while retaining the separate `-a on-request` wrapper and `--ignore-user-config`.
- This is an authorized local firmware-server validation run in the user-owned repository, with no remote target and no H01 hardware authorization. The combined `--dangerously-bypass-approvals-and-sandbox`/`--yolo` flags remain prohibited.
- Charter fit: this is the narrowest environment correction that permits the already-reviewed repair and tests to run; it introduces no product behavior, platform constant, hardware action, or new guard.

## 2026-07-24 18:23 PDT — main post-implementation and pre-verification checkpoint

- Reread the complete charter after the iteration-2 neutral gate passed both independently owned
  suites and before the main repository gate.
- Inspected the exact production diff and both H01 test files. The repair remains confined to the
  generated-plan registration owner, preserves caller-supplied strings by declared text type
  rather than by value-specific exceptions, delegates non-text compatibility to the pinned SDK,
  and introduces no hardware, board, OS, toolchain, dependency, limit, or hostile-input policy.
- Rechecked all six accepted H00 files byte-for-byte against their recorded SHA-256 values; every
  hash still matches. `git diff --check` is clean.
- Rebuilt the Windows locked environment only after the WSL neutral controller had exited, then
  reran the installed FastMCP `Tool.run()` reproducer. Literal `"null"` now reaches the plan
  engine and is rejected as `hypothesis must be concrete, not placeholder text`, while the
  response no longer misreports it as NULL.
- The separate high-level post-implementation reviewer is now assessing the risky diff. Main
  acceptance remains contingent on that verdict and the complete locked repository gate.

## 2026-07-24 18:32 PDT — post-risky-diff and final repair acceptance

- Reread the charter before accepting the repair. The independent high-level review returned
  `PASS` in `post-implementation-review.md` (SHA-256
  `277c87f3886027a25c8e19af15bedfe9538182943866aa8309c8b2573484de78`) after its own two
  complete charter reads.
- The main locked gate passed: lock check, sdist/wheel build, Ruff, Pyright, both H01 focused
  suites, collection of 215 tests, and full execution of 211 passed / 4 skipped / 110 subtests.
- Reaccepted only the narrow declarative text-boundary correction and its two independent test
  files. The private FastMCP metadata seam is localized, locked to MCP 1.28.1, and covered through
  the real registered invocation boundary; it is a documented upgrade-time maintenance risk, not
  a reason to broaden the repair.
- No H00 byte, dependency, plan/permission/gate behavior, hardware path, firmware artifact, board
  state, or unrelated production module changed. No commit, push, deploy, flash, erase, or hardware
  action was performed.
