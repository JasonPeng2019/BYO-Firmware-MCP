# One-time independent adversarial plan review

Plan SHA-256: 8bd4a8516ead395712df8f4db2287efecfdc73eed8dd66a249e1095b0634f1ad

- Reviewer: `/root/h00_h01_audit_plan_review`
- Model: `gpt-5.6-sol`
- Role: independent read-only high-level plan reviewer
- Verdict: `PASS`
- Charter attestation: reread the complete `../.codex/design_charter.md`
  before review. The plan preserves correctness, simplicity, the
  process-identity abstraction, locked tooling, bounded truthful cleanup, and
  excludes unrelated H01/server work.

## Execution risks and required test targets

1. Test `_windows_start_token()` at every native failure phase: loader, member
   lookup/call, last-error lookup, and close. Do not rewrap an existing
   `ProcessIdentityUnavailable`, and do not let close failure mask an earlier
   specific identity failure or Windows error code.
2. Exercise hygiene through the canonical module/class identity, not only the
   dynamically loaded test module. Unavailable native identity must yield one
   unresolved retained marker; `require_clean_startup()` must fail closed; and
   `terminate_marked_group()` must retain its false result.
3. Enforce the cross-role ownership boundary: doer only
   `processes.py`/`README.md`; spec tester only
   `test_h00_repository_contract.py`; regression tester only
   `test_h00_repository_regressions.py`.
4. Verify the Pyright command boundary exactly: the locked server project
   selects the executable and the sentinel project selects analysis scope.
   Prove there is no `--with`, `--no-project`, sync, resolution, or download,
   while test-only errors remain excluded and source errors are reported.
5. Cleanup tests must prove immediate success and persistent
   `PermissionError` without real delay. Retry only while the tree exists and
   before the monotonic deadline, cap sleep by remaining time, preserve the
   final concrete error, and assert final absence.
6. Preserve these accepted bytes:
   - `pyproject.toml`:
     `357b4bf783b0226d04d33035fc78fd63535bb279bf20b7e25be11637a335a454`
   - `uv.lock`:
     `1b0ea27f91dddbd00c215b8d9da487d7960e1fb4f1e1afa4c07bc4811c7ff0cf`
7. Final diff review must reject any H01, hardware, production-cleanup
   constant, dependency, lock, configuration, or unrelated server change.

This is the one review of the exact main-authored plan. It does not replan or
authorize scope beyond that plan.

