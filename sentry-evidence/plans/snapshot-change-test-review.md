# Adversarial review of the monitor TEST SUITE (snapshot-cadence rework)

Scope: `tests/test_monitor_behaviour.py`, `test_monitor_ledger.py`,
`test_monitor_counters_trail.py`, `test_monitor_delivery.py`, `tests/monitor_support.py`,
`tests/codex_harness.py`, `tests/test_monitor_codex_e2e.py`, `tests/test_monitor_stdio_lifecycle.py`,
cross-checked against `docs/Live_Edit_Docs/snapshot-change-coverage-map.md`.

Note on method: this file is being edited concurrently by another agent, so some
tests read here may already differ by the time this lands. Every finding below cites
the exact content read at review time; two are demonstrated with a throwaway
`unittest.mock.patch` script rather than just asserted. Scripts are in the session
scratchpad (`prove_t1.py`, `prove_t2.py`, `prove_t3.py`) and are not part of the repo.

---

### T-1. The AC-21 snapshot test cannot fail — it never runs the code path it claims to verify — HIGH
**Where:** `tests/test_monitor_behaviour.py::SnapshotCarriesAllRequiredFields::test_snapshot_includes_chain_head_binding_and_transport_state`

**Requirement:** AC-21 / F-43 / F-44 ("usage snapshot produced at the 100th call... carrying cumulative counts, coverage, ledger chain head, and store binding state")

**Why it cannot fail:** The test does not tick the monitor to a real snapshot boundary and read what got delivered. It calls two private methods directly and asserts on their own return value:
```python
snapshot = monitor._counters.snapshot()
summary = monitor._build_summary(trigger=str(snapshot.total), snapshot=snapshot)
self.assertIn("total", summary.get("activity", {}), ...)
...
```
This is pattern #2/#3 at once: it constructs the object under test and asserts on what it just built, and it calls a private method to stand in for the real trigger (`IssueMonitor._usage_snapshot_tick`, which is what actually fires at the cadence boundary and hands the summary to delivery). `build_summary()` unconditionally populates these keys — there is no code path through `_build_summary` that could produce a dict missing `"chain_head"` or `"workspace_bound"`, so the assertions are really checking that `reports.build_summary`'s literal dict-construction still contains its own hardcoded keys.

**Proof:** Patched `IssueMonitor._usage_snapshot_tick` to raise `AssertionError` unconditionally, then ran this test in isolation. It passed (`OK`, 1/1) — meaning the real per-100-call occasion that AC-21 is about was never invoked.

**Fix in test or product?** Test. The docstring even says so: *"the actual delivery through the background thread is integration-tested elsewhere"* — it isn't, elsewhere; see T-5. The fix is to tick the monitor through the real cadence with a real `TestTransport`, poll `transport.sent_reports` (as `test_monitor_delivery.py::SelfDrainingSpool` correctly does for files), and assert on the delivered dict, not a duplicate built by hand.

---

### T-2. Coverage map cites two tests, for two different requirements, that do not exist in the repo — HIGH
**Where:** `docs/Live_Edit_Docs/snapshot-change-coverage-map.md` rows for AC-73 and F-45, vs. `tests/test_monitor_behaviour.py`

**Requirement:** AC-73, F-45, and the map's own "Changes Made" section, which claims both were added this round.

**Why it cannot fail:** it doesn't exist to run. `grep -r "def test_acked_deleted_file_produces_no_tamper_finding" tests/` and `grep -r "def test_counter_survives_disconnect_mid_run" tests/` both return zero matches anywhere in the test tree. The map's AC-73 row cites `VerifierDoesNotFlagAckedDeletion::test_acked_deleted_file_produces_no_tamper_finding` as one of two tests proving the row COVERED; only `test_counter_vs_resident_file_gap_is_not_a_fault` exists in that class (and see T-3 for what it actually proves). The map's F-45 row cites `CounterSurvivesStateTransitions::test_counter_survives_disconnect_mid_run` as the primary evidence; that class does not exist anywhere in `tests/`. Tellingly, `test_monitor_behaviour.py` itself contains an honest inline comment at the point where that class would be (lines ~582-590) stating F-45 is *"COVERED BY DESIGN... not by a behavioural test"* — the source disagrees with the map that describes it.

**Fix in test or product?** Neither — this is the map. F-45's actual coverage (`RunCounters` has no `reset()`/`clear()`, asserted by `test_monitor_counters_trail.py::Counting::test_no_reset_method_exists`) is a legitimate architectural-invariant proof and can stay PARTIAL-but-honest; AC-73 needs the real test T-3 describes. The map needs its citations corrected to what actually exists, not what a changelog entry claims was added.

---

### T-3. The AC-73 "gap is not a fault" test only rules out one specific signal_type value, not "a fault" — HIGH
**Where:** `tests/test_monitor_behaviour.py::VerifierDoesNotFlagAckedDeletion::test_counter_vs_resident_file_gap_is_not_a_fault`

**Requirement:** AC-73 ("a large counter-vs-resident-file-count gap is not treated as a fault")

**Why it cannot fail (for the failure mode it names):**
```python
for report in reports:
    self.assertNotEqual(report.get("signal_type"), "S-1", "...")
```
This only excludes `signal_type == "S-1"`. A regression that added a gap-detector using *any other* signal type — S-2 (thrash), S-8 (coverage gap), S-13 (frustration), or a new one — would sail through this check untouched, and the report would still be "a fault" in every sense the requirement cares about.

**Proof:** Ran the exact scenario the test builds (4 calls, `usage_snapshot_every=2`, `TestTransport`), then injected one synthetic report via `monitor._file_report(signal=Signal.FRUSTRATION, ...)` standing in for a hypothetical bad gap-detector. Replayed the test's exact assertion logic against the resulting report set: it reports **PASS**, with a report present that a real bad detector could plausibly have produced. (Incidentally, the scenario as written already produces one *unrelated* report — a real S-2 thrash finding from calling the same tool 4 times with identical args/outcome/no board — which the loop also lets through, since it isn't S-1 either; the test's guard comment doesn't account for this at all.)

**Fix in test or product?** Test. Either assert `self.assertEqual(self.report_files(), [])` outright (the spec's actual claim is stronger than "not S-1" — no fault report of any kind for this gap), or explicitly enumerate every `Signal` value and assert none of them appear.

---

### T-4. `test_segment_rolls_at_the_boundary` cannot detect that rolling is completely broken — MEDIUM
**Where:** `tests/test_monitor_behaviour.py::UsageSnapshotTick::test_segment_rolls_at_the_boundary`

**Requirement:** F-158 (segment roll at the cadence boundary) — not separately cited by the coverage map (which correctly cites `OneCadenceMovesEverythingItGoverns` and `test_monitor_ledger.py::Segments::test_roll_seals_and_opens_a_successor` instead), but this test exists in the suite making the same claim and is worth flagging on its own.

**Why it cannot fail:**
```python
def test_segment_rolls_at_the_boundary(self) -> None:
    self.tick(self.SNAPSHOT_EVERY)
    files = self.ledger_files()
    self.assertGreaterEqual(len(files), 1)
    self.tick(self.SNAPSHOT_EVERY)
    self.assertGreaterEqual(len(self.ledger_files()), 1)
```
Pattern #1 exactly: `bind_workspace()` alone guarantees at least one `.jsonl` file exists (the boot record's segment). `len(files) >= 1` is true before any tick ever runs, let alone a roll.

**Proof:** Patched `SegmentLedger.roll` to a no-op (`return None`, never seals or opens a new segment) and ran this test in isolation. It passed.

**Fix in test or product?** Test — assert the segment count actually changed (distinct `(run_id, segment)` pairs, or `current_segment` incrementing), the way `OneCadenceMovesEverythingItGoverns` already does correctly elsewhere in the same file.

---

### T-5. Diagnosis of the flaky AC-21 test: the race was real and test-side, and the "fix" removed the coverage instead of the race — MEDIUM
**Where:** `tests/test_monitor_behaviour.py::SnapshotCarriesAllRequiredFields`

I could not reproduce the specific failure the manager reported (`AssertionError: [] is not true : Snapshot summary was not sent to transport`) against the current file — that assertion is no longer present; the test was rewritten (see T-1) while this review was in progress. Reconstructing from the trace and this codebase's delivery model:

**The original race:** F-167 makes delivery deliberately asynchronous — a snapshot's summary is hand ed to a background queue (`DeliveryService.enqueue_report`) and a separate daemon thread drains it. A test that ticks the monitor and *immediately* inspects `transport.sent_reports` (or similar) without polling races that thread and will intermittently observe `[]`. This is exactly finding-class #5: reading state the background delivery thread mutates. Several tests in `test_monitor_delivery.py` (`test_ack_deletes_the_local_copy`, `test_recovery_does_not_resend_acked_content`) get this right with an explicit bounded poll loop; whatever version of this test previously asserted on `transport.sent_reports` apparently did not.

**What actually landed:** rather than adding a poll (the fix every other delivery-racing test in the suite already uses), the replacement bypasses the real trigger and delivery path entirely and asserts on a hand-built duplicate (T-1). The flake is gone because the code that could race is no longer exercised — not because the race was fixed.

**Fix in test or product?** Test, and specifically: add the same poll-loop pattern used elsewhere in the suite, then assert on what `transport.sent_reports[-1]` actually contains. The product's asynchrony here is required by spec (F-167), not a defect.

---

### T-6. A residual, narrower version of the same background-thread race can undercount segments in `OneCadenceMovesEverythingItGoverns` — LOW
**Where:** `tests/test_monitor_behaviour.py::OneCadenceMovesEverythingItGoverns::test_halving_the_cadence_doubles_snapshots_and_segments`, via `tests/monitor_support.py::MonitorTestCase.ledger_records`

**Requirement:** AC-96 / N-11 (cadence change moves the roll together with snapshot production)

**Why it's a residual risk, not proven non-falsifiable:** `ledger_records()` now defensively skips a file that vanishes between `glob()` and `read_text()` (a `try/except FileNotFoundError`, already present — likely the other agent's fix for the race flagged in the round-1 code review). That stops the crash, but a segment ACKed+deleted by the background delivery thread before the test reads it is now *silently absent* from the count entirely (`TestTransport` retains no copy anywhere else, unlike `SimulatedRemoteTransport`), so `segments = len({(run_id, segment) ...})` can under-count. Both the "coarse" and "fine" runs race the same way, and "fine" always creates twice as many segments, so in practice `assertGreater(fine["segments"], coarse["segments"])` should hold almost always — this is a narrow flake, not a guaranteed pass, so I did not attempt to force a failure.

**Fix in test or product?** Test. Either use a transport that never ACKs (`TestTransport(fail_always=True)`) for this specific count comparison so nothing gets deleted mid-test, or poll to quiescence before counting.

---

### T-7 / T-8. Two loose `assertGreaterEqual` checks where the value is deterministically exact — LOW
**Where:**
- `tests/test_monitor_behaviour.py::GracefulDegradation::test_monitor_degrades_when_the_store_is_unavailable` — `self.assertGreaterEqual(health["counters"]["total"], 2)` after exactly two single-threaded calls.
- `tests/test_monitor_stdio_lifecycle.py::CloseoutOrdering::test_final_counters_reach_the_close_record` — `self.assertGreaterEqual(detail.get("total_calls", 0), 2)` after exactly two tool calls over a real stdio session.

**Requirement:** general counting correctness (F-45/N-3 territory), not a specific numbered clause.

**Why it's weak, not vacuous:** both would still fail on 0 or 1 (a real regression), so this is pattern #1 in its milder form — loose rather than non-falsifiable. Worth tightening since the actual call count in both scenarios is fully deterministic (no concurrency), so `assertEqual` costs nothing and catches a narrower class of miscounting bug (e.g., double-counting).

**Fix in test or product?** Test, low priority — swap `assertGreaterEqual(..., 2)` for `assertEqual(..., 2)` in both.

---

### T-9. No test exercises the shared-deadline property of `DeliveryService.close_for_shutdown` added for C-3 — LOW/MEDIUM, coverage gap
**Where:** `tests/test_monitor_delivery.py::ClosingOut::test_closeout_is_bounded_even_if_the_transport_hangs` (calls `drain_for_closeout` directly, not `close_for_shutdown`); `tests/test_monitor_delivery.py::AStuckSenderIsInvisible::test_health_and_shutdown_still_work` (calls `monitor.closeout(...)`, which does reach `close_for_shutdown`, but with a 5-second assertion margin).

**Requirement:** N-11 / F-113 — the closeout budget must fit inside `CLIENT_KILL_GRACE_SECONDS` (~0.5s); this is the property the C-3 fix in this same review cycle introduced (`close_for_shutdown` splitting one shared deadline across `drain_for_closeout` + `stop` instead of stacking two independent `CLOSEOUT_BUDGET_SECONDS` timeouts).

**Why it's a gap, not proven non-falsifiable:** `test_health_and_shutdown_still_work`'s `self.assertLess(elapsed, 5.0, ...)` is so much larger than the ~0.4-0.8s range in question that it would pass identically whether or not the double-budget bug C-3 fixed were still present — it was passing before that fix too. No test in the suite asserts a bound tight enough to distinguish "closeout takes ~0.4s" from "closeout takes ~0.8-0.9s," which is exactly the regression this round's C-3 fix targeted.

**Fix in test or product?** Test — add a case that calls `DeliveryService.close_for_shutdown` directly with a wedged transport and a known `budget`, and asserts total elapsed time stays within roughly that budget (a margin like 2x `budget`, not 12x), so a regression back to summed independent timeouts would be caught.

---

## Severity summary

- **HIGH:** 3 (T-1 AC-21 test never runs the real tick, proven; T-2 coverage map cites two tests that don't exist; T-3 AC-73 test only excludes one signal_type, proven vacuous against a plausible bad detector)
- **MEDIUM:** 2 (T-4 segment-roll test proven non-falsifiable; T-5 flaky-test diagnosis — real test-side race, "fixed" by deleting the coverage rather than adding the poll every other delivery test in the suite already uses)
- **LOW:** 4 (T-6 residual background-thread race, narrow; T-7/T-8 loose `assertGreaterEqual` on deterministic values; T-9 no test tight enough to catch a regression of this round's own C-3 fix)

No findings in `tests/codex_harness.py` or `tests/test_monitor_codex_e2e.py` — that suite already guards against vacuous passes explicitly in several places (e.g. `test_a_session_of_correct_refusals_files_no_server_defect` asserts a refusal was actually recorded before checking no defect followed) and was not run live (no logged-in `codex` CLI in this environment) so its assertions were reviewed by inspection only, not executed.
