Changed `tests/test_h00_repository_contract.py:285-291`.

What changed:
- Removed the clean-candidate read of `.change-loop/fresh-suite/H00/PRE_POSIX_REPAIR_MANIFEST.json`.
- Added an objective baseline check before deleting nested `.git`: `git rev-parse HEAD` must equal `6f3da0a9a0bb97fb535c8c0ba11a4d2b31f5e876`.
- Kept the fixed six-path overlay and the candidate-root manifest-control verification intact at the existing `H00_FINAL_CANDIDATE_MANIFEST.json` / `.sha256` check.

Recorded isolated spec command:
- `uv run --no-project --with pytest --with psutil pytest tests/test_h00_repository_contract.py`

Result:
- `10 passed, 1 skipped`

Charter rereads:
- I read the complete `../.codex/design_charter.md` before editing.
- I reread the complete `../.codex/design_charter.md` again after editing and before this verdict.

Manifest honesty:
- `.change-loop/fresh-suite/H00/state/spec_test_cmd` contains the exact command above.
- `.change-loop/fresh-suite/H00/state/spec_tester.manifest` lists only `tests/test_h00_repository_contract.py`.
