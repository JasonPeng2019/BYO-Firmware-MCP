Authorized local firmware validation. This review is limited to the named local BYO-Firmware-MCP
workspace and the A20 production repair. No remote or third-party target is in scope.

You are the one-time independent adversarial reviewer of the implemented A20 server repair. Use
`gpt-5.6-terra` at medium reasoning on priority/Fast as configured by the controller. This turn is
strictly read-only: do not edit any file, run the server, operate hardware, commit, push, deploy,
replan, or launch another agent.

Before judging the diff:

1. Read the complete authoritative `../.codex/design_charter.md` and verify its SHA-256 is
   `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`.
2. Read:
   - `.change-loop/fresh-suite/A20-sleeping-symbol-read/changes.md`
   - `.change-loop/fresh-suite/A20-sleeping-symbol-read/plan.md`
   - `.change-loop/fresh-suite/A20-sleeping-symbol-read/plan-review.md`
   - `.change-loop/fresh-suite/A20-sleeping-symbol-read/state/test_report.md`
   - `src/pyocd_debug_mcp/tools/memory.py`
   - the exact A20 wiring hunk in `src/pyocd_debug_mcp/server.py`
   - `tests/test_a20_sleeping_symbol_read_spec.py`
   - `tests/test_regression_a20_sleeping_symbol_read.py`
3. Inspect Git status and the exact diff for only
   `src/pyocd_debug_mcp/tools/memory.py` and `src/pyocd_debug_mcp/server.py`. The worktree contains
   older accepted dirty repairs; do not attribute unrelated hunks/files to A20.

Adversarially trace every A20 execution path against plan SHA-256
`a974e693dd8b16d03d993b27b0c16f1113891482de58e67160608b2cc6da0a07`.
In particular, check lifecycle ordering for HALTED and every non-HALTED state, successful and
failed state/halt/read/resume operations, dual-failure truthfulness and exception causality,
success-event timing, compatibility of all `MemoryToolServices` construction sites, raw read/write
isolation, production wiring, and whether the published FastMCP help satisfies the charter.
Challenge target-state normalization, provider return types, accidental target resume, and the
smallest plausible regression surface. Reject board-, OS-, probe-, or fixture-specific behavior,
retries, value heuristics, broad refactors, and new paternalistic or hostile-input guards.

Return a concise final report containing:

- verified charter and plan SHA-256 values;
- reviewer label `A20-diff-adversarial-reviewer-001`;
- `VERDICT: ACCEPT` only if there is no actionable correctness/scope/usability issue, otherwise
  `VERDICT: NEEDS_FIX`;
- numbered findings, separating actionable defects from nonblocking residual risks;
- exact file/line or test evidence for every finding;
- an explicit statement that you made no edits and performed no hardware action.

Do not ask for another review. The main model will independently decide whether any finding
requires resuming the existing repair roles.
