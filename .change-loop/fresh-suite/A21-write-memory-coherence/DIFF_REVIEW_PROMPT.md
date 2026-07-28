Authorized local firmware-server validation. This review is limited to the named local
BYO-Firmware-MCP workspace and the A21 production repair. No remote or third-party target is in
scope.

You are the one-time independent adversarial reviewer of the implemented A21 `write_memory`
server repair. Use `gpt-5.6-terra` at medium reasoning on priority/Fast as configured by the
controller. This turn is strictly read-only: do not edit any file, run the server, operate
hardware, commit, push, deploy, replan, or launch another agent.

Before judging the diff:

1. Read the complete authoritative `../.codex/design_charter.md` and verify its SHA-256 is
   `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`.
2. Read:
   - `.change-loop/fresh-suite/A21-write-memory-coherence/changes.md`
   - `.change-loop/fresh-suite/A21-write-memory-coherence/plan.md`
   - `.change-loop/fresh-suite/A21-write-memory-coherence/plan-review.md`
   - `.change-loop/fresh-suite/A21-write-memory-coherence/state/test_report.md`
   - `.change-loop/fresh-suite/A21-write-memory-coherence/MAIN_VERIFICATION_FINDINGS.md`
   - `src/pyocd_debug_mcp/tools/memory.py`
   - the A21 help/wiring hunks in `src/pyocd_debug_mcp/server.py` and
     `src/pyocd_debug_mcp/guardrails/plan_defs.py`
   - `tests/test_a21_write_memory_coherence_spec.py`
   - `tests/test_regression_a21_write_memory_coherence.py`
   - the exact changed hunks in `tests/test_a20_sleeping_symbol_read_spec.py` and
     `tests/test_h00_repository_contract.py`.
3. Inspect Git status and the exact production/test diff. The accepted baseline is clean HEAD
   `db3fb8660c8186d351508050bf622a6aaf0b50fc`; do not attribute runtime evidence files to
   production behavior.
4. Treat these main-model verification results as evidence, but challenge their adequacy:
   - neutral gate: 8 spec tests + 12 subtests and 13 regression/adjacent tests + 6 subtests pass;
   - memory/blast-radius selection: 81 passed, 1 skipped, 45 subtests;
   - H00 clean-candidate transaction: 1 passed in 601.59 seconds;
   - the rest of the full suite: 356 passed, 4 skipped, 1 deselected, 186 subtests;
   - Ruff: pass; Pyright with the repository venv active: 0 errors; `git diff --check`: pass.

Adversarially trace every A21 execution path against plan SHA-256
`d2a1d29a7b8932133fa959bf35733906086cee3121240aa0594e15c0615fc626`.
In particular, check lifecycle ordering for already-HALTED and every non-HALTED state; state,
halt, write, verification-read, mismatch, cancellation-class, and resume failures; dual-failure
truthfulness and exception causality; success-event timing; all `MemoryToolServices` construction
sites; symbol and justified raw routes; pre-I/O refusals and containment; public parameters,
plan/budget/permission compatibility; and whether all published FastMCP/plan help meets the
charter. Challenge state normalization, provider return types, accidental resume, missing
readback, post-resume durability overclaim, test weakening, and the smallest plausible regression
surface. Reject board-, MCU-, firmware-, OS-, probe-, or fixture-specific behavior; retries or
resets; broad refactors; and new paternalistic or hostile-input guards.

Return a concise final report containing:

- verified charter and plan SHA-256 values;
- reviewer label `A21-write-memory-diff-reviewer-001`;
- `VERDICT: ACCEPT` only if there is no actionable correctness/scope/usability/test-integrity
  issue, otherwise `VERDICT: NEEDS_FIX`;
- numbered findings separating actionable defects from nonblocking residual risks;
- exact file/line or test evidence for every finding;
- an explicit statement that you made no edits and performed no hardware action.

Do not ask for another review. The main model will independently decide whether any finding
requires resuming the existing repair roles.
