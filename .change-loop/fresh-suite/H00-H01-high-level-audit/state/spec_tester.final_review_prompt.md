# Persistent adversarial spec-tester follow-up

Stay in the BYO-Firmware-MCP repository. Resume your existing spec-tester role and edit only
your owned `tests/test_h00_repository_contract.py` plus your required state command/manifest.
Do not edit production source, README, dependency metadata, the plan, or runtime review files.

Before acting, reread all of:

1. `../.codex/design_charter.md`
2. `.change-loop/fresh-suite/H00-H01-high-level-audit/plan.md`
3. `.change-loop/fresh-suite/H00-H01-high-level-audit/main-final-review.md`

Treat the main-model final review as implementation/test feedback under the existing plan, not
permission to rewrite or re-review the plan. Add or tighten the smallest deterministic tests that
expose every listed gap. Correct the cleanup callback annotation honestly and without a
suppression. Do not weaken any existing assertion.

Run focused checks first. Do not start a second copy of any command while one is still alive. A
full clean-candidate transaction is not required in this turn: the neutral gate will run the
recorded complete suite after the doer repair. Record the full self-preparing focused-suite command
and the same one-file manifest required by the change loop.

At the end, reread `../.codex/design_charter.md` and report the checkpoint, exact tests added,
focused outcome, and expected production failure.
