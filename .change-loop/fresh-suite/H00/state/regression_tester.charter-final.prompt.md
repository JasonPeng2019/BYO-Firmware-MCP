# Required final design-charter checkpoint — regression tester

Remain in the BYO-Firmware-MCP repository root. This is a read-only checkpoint; do not edit production or tests in this turn.

1. Read the complete sibling charter at `../.codex/design_charter.md` now (not merely the plan's quotation or a heading).
2. Reinspect your current `tests/test_h00_repository_regressions.py` diff against the complete charter, `.change-loop/fresh-suite/H00/plan.md`, and the one-time plan review.
3. Check that the test independently protects the adjacent Pyright scope without weakening diagnostics, relying on the broken repo venv, fabricating platform proof, or crossing ownership boundaries.
4. Report any concrete violation with exact file/line evidence. If none, explicitly attest that you read the complete charter after your edits and before this final verdict. Do not edit in this checkpoint.