# Final main-model validation

- Main-model design-charter SHA-256:
  `03347b0f7ce185922e07e373784a39d49f3a497c3ee8c35035bb3bfab5411bdb`
- Neutral report: `state/test_report.md`
  - spec: 19 passed, 7 subtests passed
  - regression: 1 passed
- Standard Windows root verification:
  `root-verification-windows-standard.log`
  - lock/sync/build/unrelated import/Ruff/Pyright: pass
  - collection: 209 tests
  - focused: 57 passed, 3 skipped, 1 deselected, 7 subtests passed
  - full: 205 passed, 4 skipped, 74 subtests passed
- The earlier `root-verification-windows.log` failure was caused by the
  orchestrator's nonstandard absolute `UV_PROJECT_ENVIRONMENT` override being
  inherited by the clean-candidate transaction. The standard documented
  environment was rebuilt and passed; this was not classified as a server
  defect.
- Preserved accepted hashes:
  - `pyproject.toml`:
    `357b4bf783b0226d04d33035fc78fd63535bb279bf20b7e25be11637a335a454`
  - `uv.lock`:
    `1b0ea27f91dddbd00c215b8d9da487d7960e1fb4f1e1afa4c07bc4811c7ff0cf`
- `git diff --check`: pass
- Main final-diff review: pass after the primary-error, absent-tree, callback
  typing, and redundant-flag follow-ups.
- No commit, push, deploy, flash, erase, or hardware action.
