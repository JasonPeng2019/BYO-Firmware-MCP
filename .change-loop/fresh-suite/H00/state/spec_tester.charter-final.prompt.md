# Required final design-charter checkpoint — spec tester

Remain in the BYO-Firmware-MCP repository root. This is a read-only checkpoint; do not edit production or tests in this turn.

1. Read the complete sibling charter at `../.codex/design_charter.md` now (not merely the plan's quotation or a heading).
2. Reinspect your current `tests/test_h00_repository_contract.py` diff against the complete charter, `.change-loop/fresh-suite/H00/plan.md`, and the one-time plan review.
3. Check that the test remains an honest, minimal falsification surface; does not fabricate platform evidence, weaken failures, add arbitrary guards, or overfit one host; and preserves its ownership boundary.
4. Report any concrete violation with exact file/line evidence. If none, explicitly attest that you read the complete charter after your edits and before this final verdict. Do not edit in this checkpoint.