# MCP Issue Monitor — Remaining Work

Status reviewed 2026-10-10 against the current specification, implementation,
repository-owned client artifacts, and focused test suite. The local monitor,
source/raw-sdist client-workspace attachment, and schema/coverage verification
are complete within the preview's explicit source-handoff contract; the
installed server wheel does not deliver client skills. This file records the one
remaining remote-delivery item without treating the current filler as off-box
evidence.

## Remaining work

- [ ] **OAuth/OpenID remote delivery:** replace the current local
  `simulated_remote/` filler with the real authenticated pipeline. The present Sentry SDK
  integration uses `dsn=None` and a local envelope writer, so
  `FILLER_SIMULATED` acknowledgements are not off-box delivery.

## Completed and evidenced

- [x] Real snapshot trigger and delivery tested at the 100- and 200-call
  boundaries in `tests/test_monitor_wiring.py`.
- [x] Delivered summary fields, including uptime, environment, and verification
  state, are checked by
  `ExactHealthCheckBoundaries.test_delivered_snapshot_contains_required_complete_summary`.
- [x] Adversarial gaps around ACK deletion, counter transitions, and closeout
  timing have focused regression tests.
- [x] Repository-owned client artifacts ship in `client-workspace/`; AT09 no
  longer depends on an unversioned parent-directory workspace.
- [x] `skills/byo-issue-report/` contains the canonical tool-agnostic S-4…S-14
  criteria and exact issue/check-in templates; a byte-identical copy under
  `client-workspace/skills-src/firmware/byo-issue-report/` is selected by the
  attached firmware mode, and tests prevent the two copies from drifting.
- [x] AC-104 tier 1 now has an explicit behavioral test; the existing tier-2
  recomputed-chain test and tier-3 ceiling test remain cited in the reconciled
  coverage map.
- [x] The coverage map is reconciled and the current spec-to-implementation
  review is recorded in
  `sentry-evidence/plans/current-spec-implementation-review.md`.
- [x] Historical baseline verification evidence is preserved: 477 tests passed,
  3 skipped; Ruff and Pyright passed; the final E2E check-in test passed. See
  `sentry-evidence/results/`.
- [x] The baseline monitor source, tests, and evidence were committed before
  this review. The current `0.1.8rc1` release-remediation slice is implemented
  and locally verified in the working tree but remains intentionally
  uncommitted until the maintainer requests a commit.
