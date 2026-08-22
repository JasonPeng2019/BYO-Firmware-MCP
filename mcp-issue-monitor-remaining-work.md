# MCP Issue Monitor — Remaining Work

Status reviewed 2026-08-21 against `sentry-evidence/` and the current test
suite. The server-side monitor and its main verification work are complete;
this file records only the remaining release-readiness work.

## Remaining work

- [ ] Add and ship the required client-side, tool-agnostic workspace skill.
- [ ] Include the skill's enumerated signal criteria and exact issue/check-in
  templates.
- [ ] Complete explicit automated coverage for AC-104 under-reporting tiers 1
  and 2. Tier 3's limit is tested and documented, but the coverage map still
  classifies AC-104 as partial.
- [ ] Reconcile the coverage map and any stale implementation/handoff documents
  with the tests that now exist, particularly the real boundary/delivery,
  ACK-deletion, counter-transition, and closeout-deadline tests.
- [ ] Perform a final current spec-to-implementation review before declaring
  the feature complete.

## Completed and evidenced

- [x] Real snapshot trigger and delivery tested at the 100- and 200-call
  boundaries in `tests/test_monitor_wiring.py`.
- [x] Delivered summary fields, including uptime, environment, and verification
  state, are checked by
  `ExactHealthCheckBoundaries.test_delivered_snapshot_contains_required_complete_summary`.
- [x] Adversarial gaps around ACK deletion, counter transitions, and closeout
  timing have focused regression tests.
- [x] Clean verification evidence is preserved: 477 tests passed, 3 skipped;
  Ruff and Pyright passed; the final E2E check-in test passed. See
  `sentry-evidence/results/`.
- [x] Monitor source, tests, and evidence are committed.
