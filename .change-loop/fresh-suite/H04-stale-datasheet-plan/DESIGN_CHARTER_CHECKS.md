# Design-charter checkpoints

This consolidated log was reconstructed by the main orchestrator after the neutral gate from the
role event stream in `controller.log`, the role last-message files, and the surviving regression
entries. The regression tester's first write replaced rather than appended the earlier checkpoint
log; this reconstruction restores the recorded checkpoints without changing production code,
tests, gate commands, or results.

## Main orchestrator

- 2026-07-25 — before defect classification and planning: reread
  `../.codex/design_charter.md`; classified stale selected datasheet bytes as a correctness failure
  for a compliant but fallible agent, not hostile-input hardening.
- 2026-07-25 — after deterministic code trace: reread the charter after tracing
  `PlanDefinition`, `PlanEngine` artifact binding, primary/paired guarded dispatch, and setup
  promotion. Chose the existing general digest mechanism as the simplest correct owner.
- 2026-07-25 — before finalizing the plan: reread the charter; excluded setup-specific state,
  board/vendor/OS branches, arbitrary limits, immutable-copy machinery, and unrelated edits.
- 2026-07-25 — after neutral gate and before independent oracle: reread the charter; confirmed the
  production diff remains the two declarative binding attributes and the guard permits unchanged
  PDFs while refusing only a verified stale-plan contradiction.

## Independent plan reviewer

- 2026-07-25 — before review: read the charter and confirmed stale verified assumptions are a
  correctness concern without hostile-input or paternalistic scope.
- 2026-07-25 — after code trace: reread the charter and confirmed the shared digest binding covers
  both `board_setup` and paired `board_fix_setup`.
- 2026-07-25 — before `PROCEED`: reread the charter and confirmed the plan preserves arbitrary
  board/vendor/OS behavior and adds no special case, copy subsystem, or cap.

## Persistent implementation doer

- 2026-07-25 — before analysis: read the charter and accepted the narrow declarative plan scope.
- 2026-07-25 — immediately before production edit: reread the charter and changed only the
  `board_setup` plan definition to bind `datasheet_path` with the `.pdf` contract suffix.
- 2026-07-25 — before verification: reread the charter; compilation and direct primary/paired
  definition assertions passed, while the selected environment's missing `pytest` was reported
  honestly rather than hidden.
- 2026-07-25 — before final verdict: reread the charter and confirmed no setup-specific mechanism,
  hardware action, arbitrary limit, or unrelated production edit was introduced.

## Persistent specification tester

- 2026-07-25 — before analysis: read the charter and chose in-process, host-only guardrail
  specifications with temporary arbitrary PDFs.
- 2026-07-25 — immediately before test editing: reread the charter and targeted exact binding,
  stale-primary ordering, stale-paired ordering, recovery, and unchanged-PDF controls.
- 2026-07-25 — between fixture/assertion corrections: reread the charter; corrected only invalid
  test plan IDs and then asserted the structured refusal code instead of assuming it appears in the
  human message.
- 2026-07-25 — before verification and final verdict: reread the charter; the four-test spec suite
  passed with no hardware action.
- 2026-07-25 — iteration 2 command repair: corrected the tester-owned neutral command to a
  Bash-safe repository-relative form; no production or test behavior changed.

## Persistent regression tester

- 2026-07-25 — before analysis: stale datasheet identity is a correctness guard; no hardware
  action or hostile-input limit is in scope.
- 2026-07-25 — before editing: tested the existing generic artifact lifecycle without introducing
  a board, vendor, OS, or path special case.
- 2026-07-25 — before verification and final verdict: retained the minimal correctness guard only;
  the focused suite passed without hardware access.
- 2026-07-25 — iteration 2 before state repair: used a shell-portable repository-relative test
  command; no production or hardware behavior was altered.
- 2026-07-25 — iteration 2 before final verdict: confirmed the recovery test command works under
  Bash and preserves the intended stale-artifact correctness boundary.
