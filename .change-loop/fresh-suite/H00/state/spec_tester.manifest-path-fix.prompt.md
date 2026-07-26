# Root review finding — candidate manifest control-path defect

Remain in the BYO-Firmware-MCP repository root. Edit only your owned `tests/test_h00_repository_contract.py`; do not edit production or any other test.

Before editing, read the complete `../.codex/design_charter.md`, then reread `.change-loop/fresh-suite/H00/plan.md` CL-005 and the one-time plan-review risk 4. After editing and before verdict, reread the complete charter again and attest both checks.

Verified defect in the current spec test:
- Lines 35-37 point `FINAL_CANDIDATE_MANIFEST*` at `.change-loop/fresh-suite/H00/...`.
- CL-005 instead requires the root-owned materializer to copy the two controls into each clean host candidate at candidate-root names `H00_FINAL_CANDIDATE_MANIFEST.json` and `H00_FINAL_CANDIDATE_MANIFEST.sha256`.
- The current optional check therefore silently skips those controls in a clean candidate and violates plan-review risk 4 (“no change-loop runtime dependency”).

Make the smallest correction so:
1. the live pre-materialization neutral gate still works with root controls absent;
2. when root controls are present at the candidate root, the copied test verifies both the manifest's exact six hashes and detached SHA-256;
3. one present/one missing control is rejected rather than silently skipped;
4. no `.change-loop` runtime path is required inside the host candidate.

Run your exact isolated spec command and leave `spec_test_cmd`/manifest honest. Final response must cite the changed lines, test result, and both complete-charter rereads.