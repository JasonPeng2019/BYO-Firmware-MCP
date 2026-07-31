# Practical boundary-test evidence

The hardware-free practical boundary tests are preserved in
[`../tests/test_monitor_wiring.py`](../tests/test_monitor_wiring.py), in
`ExactHealthCheckBoundaries`:

- `test_100_and_200_snapshots_are_exact_cumulative_and_delivered` drives the
  registered `server_health_check` tool to the exact 100- and 200-call
  boundaries and verifies cumulative snapshot delivery.
- `test_delivered_snapshot_contains_required_complete_summary` drives the
  practical health-check path to 100 calls and verifies the delivered summary.
- `test_500th_health_check_prompts_once_and_records_routine_checkin` drives the
  same path to 499, 500, and the following call; it verifies the one-time
  500-call prompt, the resulting routine check-in, and its 500-call summary.

These tests use registered server dispatch only and do not touch physical
hardware. Their passing full-suite record is
[`final3-unittest-discovery.log`](final3-unittest-discovery.log): 477 tests
passed, with 3 skipped. The separate final E2E check-in record is
[`retest-e2e-checkin-iteration-5.log`](retest-e2e-checkin-iteration-5.log).
