# Main-model verification

Date: 2026-07-28

## Boundary and charter

- Re-read `.codex/design_charter.md` before this verification boundary.
- Reviewed the complete A21 production diff and both new focused test files.
- The change is provider-neutral and lifecycle-service based; it adds no board,
  operating-system, or provider special case.
- The success contract is truthful: a success event is emitted only after an exact
  same-address/same-width readback and, when the tool inserted a halt, successful
  restoration.
- Failure paths preserve the primary mutation/readback failure and report a
  simultaneous restoration failure without fabricating success.
- Published tool and plan guidance explains the lifecycle behavior, the limited
  meaning of immediate success, and operator recovery.

## Neutral change-loop gate

The completed iteration-2 neutral gate is GREEN:

- Spec suite: 8 passed, 12 subtests.
- Regression suite: 13 passed, 6 subtests.
- The persistent regression tester removed one obsolete A20 assumption that raw
  writes bypass lifecycle behavior while retaining A20's raw-read contract proof.

## Main verification commands

- `.venv\Scripts\ruff.exe check src tests`
  - PASS: all checks passed.
- `uv run --locked pyright`
  - PASS: 0 errors, 0 warnings, 0 informations.
- `.venv\Scripts\python.exe -m pytest -q`
  - PASS: 357 passed, 4 skipped, 186 subtests passed in 1019.82 seconds.
  - This includes the H00 clean-candidate-clone contract test.
- `git diff --check`
  - PASS. Git emitted only the repository's line-ending conversion warnings.

An initial direct `.venv\Scripts\pyright.exe` invocation reported unresolved
third-party imports because it did not use the repository's locked uv environment.
That was an invocation/configuration error, not a source failure. The documented
locked-environment command above passed cleanly.

## Diff review verdict

ACCEPTED for targeted hardware retest. No unresolved production-code criticism or
test-gate failure remains. Hardware acceptance still requires the exact A21 live
reproducer against a freshly started MCP process on this repaired working tree.
