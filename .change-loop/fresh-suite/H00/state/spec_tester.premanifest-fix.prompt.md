# Native Windows gate finding — remaining runtime-manifest dependency

Remain in the BYO-Firmware-MCP repository root. Edit only your owned `tests/test_h00_repository_contract.py`; do not edit production or any other test.

Before editing, read the complete `../.codex/design_charter.md`, CL-005, and plan-review risk 4. After editing and before verdict, reread the complete charter and attest both checks.

Verified failure evidence:
- Root materialized a clean Windows candidate at baseline `6f3da0a...`, overlaid the exact six files from `FINAL_CANDIDATE_MANIFEST.json`, and copied the two root controls.
- The exact seven-command native gate reached full pytest, then failed because the copied test tried to open `.change-loop/fresh-suite/H00/PRE_POSIX_REPAIR_MANIFEST.json` inside the clean candidate.
- Log: `.change-loop/fresh-suite/H00/native-gates/windows-ordinary.log`.
- This is the same prohibited clean-candidate runtime dependency called out by CL-005 and plan-review risk 4.

Make the smallest self-contained correction:
1. remove the clean candidate's dependency on `PRE_POSIX_REPAIR_MANIFEST.json`;
2. retain an objective assertion that the cloned candidate is based on exact commit `6f3da0a9a0bb97fb535c8c0ba11a4d2b31f5e876` before removing nested `.git`;
3. retain the fixed six-path/hash overlay and root manifest-control verification;
4. do not weaken or skip any gate.

Run your exact isolated spec command and keep its command/manifest honest. Final response must cite changed lines, the test result, and both complete-charter rereads.