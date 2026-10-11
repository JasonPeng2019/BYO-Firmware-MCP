# Sentry evidence

This folder is the preserved verification record for the Sentry issue-monitor
and logger work. It is evidence, not an active test directory for the packaged
server.

## Contents

- `tests/` — the complete source test-suite snapshot exercised by the final
  green run.
- `specifications/` — the feature and verification specifications.
- `plans/` — implementation, coverage, test, and HIL plans.
- `results/` — final test output, HIL records, and generated evidence used by
  the passing HIL run.

## Recorded passing checks

- `results/final3-unittest-discovery.log`: 477 tests passed, 3 skipped.
- `results/final3-ruff.log`: Ruff reported all checks passed.
- `results/final3-pyright.log`: Pyright reported zero errors, warnings, and
  informational messages.
- `results/retest-e2e-checkin-iteration-5.log`: final E2E check-in test passed.
- `results/practical-boundary-test-summary.md`: exact 100- and 500-call
  practical boundary tests and their full-suite/E2E evidence.
- `results/HIL_RESULTS.md`: the final setup-relock HIL regression passed with
  no destructive board operation.

The command outcomes are preserved in `results/`. Before source publication,
operator home-directory paths and physical probe identifiers were replaced by
explicit redaction markers. These files are privacy-redacted derivatives, not
byte-for-byte copies of the private originals; the substitutions do not change
the recorded pass/fail outcomes. `SANITIZATION_MANIFEST.json` records the
replacement classes and exact retained derivative hashes. Verify both the
manifest and generic privacy scan with
`python scripts/verify_evidence_privacy.py`; use its explicit
`--write-manifest` mode only after an intentional evidence update.
