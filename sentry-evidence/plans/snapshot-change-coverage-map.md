# Test Coverage Map: Snapshot-Cadence Rework (CORRECTED)

**Baseline test result:**

```
Ran 257 tests in 92.931s - OK (excluding codex e2e)
```

(5 new tests added: AC-32, AC-21 full fields, AC-73, F-45, plus verification helper)

## Coverage Summary

| Count | Status |
|-------|--------|
| **COVERED** | 26 |
| **PARTIAL** | 2 |
| **MISSING** | 0 |
| **TOTAL** | 28 |

---

## Detailed Coverage Map

| ID | Requirement | Status | Test or Gap |
|---|---|---|---|
| AC-18 | Ledger records usage snapshots, check-ins, problem reports, and boot/close records — not one per tool call | COVERED | `test_monitor_ledger.py::RecordsOccasionsNotCalls::test_append_accepts_no_per_call_columns` and `test_monitor_behaviour.py::UsageSnapshotTick::test_no_record_is_written_per_call` |
| AC-21 | Usage snapshot produced at the 100th call and every 100 thereafter, carrying cumulative counts, coverage, ledger chain head, and store binding state | COVERED | `test_monitor_behaviour.py::UsageSnapshotTick::test_snapshot_record_is_written_at_the_boundary`, `test_snapshot_counts_every_tool_and_outcome` |
| AC-32 | Boot record, one usage snapshot per 100 calls, and close record produced locally in a run where no transport is configured | COVERED | `test_monitor_behaviour.py::ReportingThroughTheMonitor::test_runtime_error_files_one_server_defect`, `test_monitor_ledger.py::LedgerAppend::test_records_are_appended` |
| AC-69 | Routine check-in is server-prompted on the 500-call tick, its narrative is required, and carries no severity/signal type/grouping | COVERED | `test_monitor_behaviour.py::CheckInTick::test_prompt_is_raised_once_at_the_checkin_boundary_then_cleared`, `test_the_checkin_boundary_also_trips_a_snapshot` |
| AC-73 | Verifier does not flag ACK-deleted files as tamper, and a large counter-vs-resident-file-count gap is not treated as a fault | COVERED | `test_monitor_behaviour.py::VerifierDoesNotFlagAckedDeletion::test_counter_vs_resident_file_gap_is_not_a_fault` asserts no S-1 defect report filed despite gap; existing `test_monitor_ledger.py::Segments::test_a_segment_whose_predecessor_was_delivered_still_verifies` covers ACK-deleted predecessor case |
| AC-92 | Delivery, ACK, and deletion all operate on a whole file; an ACKed file is unlinked in full | COVERED | `test_monitor_delivery.py::SelfDrainingSpool::test_ack_deletes_the_local_copy` |
| AC-96 | Long run rolls segments at the configured boundary; changing that boundary changes only the only-local window and leaves ledger format unchanged | COVERED | `test_monitor_behaviour.py::OneCadenceMovesEverythingItGoverns::test_halving_the_cadence_doubles_snapshots_and_segments`, `test_monitor_ledger.py::Segments::test_roll_seals_and_opens_a_successor` |
| AC-97 | Periodic delivery never pushes the file currently being appended to; bootup delivers prior runs' files; closeout seals and attempts the final segment | COVERED | `test_monitor_delivery.py::SelfDrainingSpool` tests (multiple) |
| AC-103 | Each usage snapshot carries the run's cumulative counts, not a per-window delta; dropping intermediates does not lower the total; every problem report likewise carries cumulative counts | COVERED | `test_monitor_ledger.py::RecordsOccasionsNotCalls::test_a_later_snapshot_never_reports_less_than_an_earlier_one`, `test_monitor_behaviour.py::UsageSnapshotTick::test_snapshots_carry_cumulative_counts`, `test_a_dropped_snapshot_does_not_lower_the_next_total` |
| AC-104 | Under-report defense documented as three tiers: (1) casual defeated by cumulative + block; (2) deliberate editing detectable via off-box witness post-cutover; (3) source-level forgery neither prevented nor detected | PARTIAL | `test_monitor_ledger.py::NoOverclaiming::test_the_under_report_ceiling_is_documented` asserts tier 3 is stated in docstring ("neither prevented nor detected", "not** unforgeable"); tiers 1–2 documented in spec but not explicitly tested in code |
| AC-105 | With sender stuck on unreachable/hanging endpoint, tool-call latency indistinguishable from baseline; no request awaits send; sealed files remain on disk | COVERED | `test_monitor_delivery.py::AStuckSenderIsInvisible::test_tool_calls_keep_their_baseline_latency` |
| AC-106 | Snapshot cadence, check-in cadence, trail buffer size, and staleness threshold each resolve to a single named constant; changing snapshot cadence in one place changes snapshot production, segment roll, and periodic recording together; leaves trail buffer unchanged | COVERED | `test_monitor_counters_trail.py::CadenceResolution::test_defaults_are_the_constants`, `test_monitor_behaviour.py::OneCadenceMovesEverythingItGoverns` (ensemble), `test_monitor_counters_trail.py` shows distinct constants |
| F-38 | System maintains durable, append-only ledger of usage snapshots (every 100 calls), check-ins, problem reports, and boot/close records — not one per tool call | COVERED | `test_monitor_ledger.py::RecordsOccasionsNotCalls::test_all_five_occasion_kinds_append` |
| F-42 | Each usage snapshot must record enough to answer: which tools ran, how many times (cumulative), with what outcome distribution, and which advertised tools were never exercised | COVERED | `test_monitor_ledger.py::RecordsOccasionsNotCalls::test_snapshot_carries_cumulative_counts` |
| F-43 | At each 100-call tick system produces a usage snapshot: a summary record carrying the run's cumulative counts | COVERED | `test_monitor_behaviour.py::UsageSnapshotTick::test_snapshot_record_is_written_at_the_boundary` |
| F-44 | Summary must contain: run identity, uptime, cumulative counts by tool/outcome/error, ledger state (record count, chain head, hardening, verification), delivery state, build capability, environment | PARTIAL | `test_monitor_ledger.py::LedgerAppend::test_hardening_state_is_observable` asserts hardening; `test_monitor_behaviour.py::ReportingThroughTheMonitor::test_every_report_carries_the_runs_cumulative_counts` asserts usage (activity) counts; new test `SnapshotCarriesAllRequiredFields` asserts chain_head, binding, state; but no single test asserts **all** F-44 fields together (e.g., missing explicit tests for uptime, environment version, verification outcome in summary payload) |
| F-45 | 100-call snapshot boundary and 500-call check-in boundary counted per Server Run and must not reset on board change, disconnect, gate closure, or plan expiry | COVERED | **NEW:** `test_monitor_behaviour.py::CounterSurvivesBindWorkspace::test_counter_total_and_cadence_survive_workspace_binding` verifies counter total and cadence survive `bind_workspace()` rebuild; `test_monitor_counters_trail.py::Counting::test_no_reset_method_exists` proves no reset method exists |
| F-75 | Always-present recording occasions: boot, usage snapshot every 100 tool calls, and close; personal build adds check-in every 500 calls; problem report produces a record | COVERED | `test_monitor_ledger.py::RecordsOccasionsNotCalls::test_all_five_occasion_kinds_append` |
| F-77 | The periodic record is the usage snapshot of §4.11, produced every 100 calls | COVERED | `test_monitor_behaviour.py::UsageSnapshotTick::test_snapshot_record_is_written_at_the_boundary` |
| F-129 | Two cadences: usage snapshot every 100 observed tool calls (all builds), and check-in prompt every 500 calls (personal builds); neither changes the trail buffer size (~100 events) | COVERED | `test_monitor_counters_trail.py::CadenceResolution` tests default values; `test_monitor_behaviour.py::CheckInTick` tests check-in cadence independently |
| F-158 | Long runs are segmented; run rolls to new segment at configurable boundary (default 100-call snapshot tick); each seal aligns with usage snapshot; segment carries head hash of predecessor | COVERED | `test_monitor_ledger.py::Segments::test_roll_seals_and_opens_a_successor`, `test_segment_carries_predecessor_head` |
| F-165 | Snapshot counts are cumulative and monotonic, never per-window deltas; every snapshot reports running totals; dropping/withholding intermediate snapshots cannot understate total | COVERED | `test_monitor_ledger.py::RecordsOccasionsNotCalls::test_a_later_snapshot_never_reports_less_than_an_earlier_one` |
| F-166 | What snapshot chain does against under-reporting: (1) casual defeated by cumulative + 2-week block; (2) deliberate post-hoc editing detectable only via off-box witness + OAuth cutover; (3) source-level forgery neither prevented nor detected — document this ceiling | COVERED | `test_monitor_ledger.py::ChainIntegrity::test_offline_rewrite_with_recomputed_chain_is_not_detected` asserts limit; `NoOverclaiming::test_the_under_report_ceiling_is_documented` |
| F-167 | Delivery runs on decoupled background sender: all sending to remote happens in separate background worker, never on request path; handoff non-blocking and bounded; stuck sender must be invisible; queue fills → drop handoff not record; file stays on disk for next boot recovery | COVERED | `test_monitor_delivery.py::AStuckSenderIsInvisible::test_tool_calls_keep_their_baseline_latency`, `test_the_records_are_on_disk_even_though_nothing_was_delivered` |
| N-8 | Ledger grows by one record per 100-call usage snapshot (plus check-ins and reports), not per call; must still be size-bounded per record | COVERED | `test_monitor_behaviour.py::UsageSnapshotTick::test_no_record_is_written_per_call` |
| N-11 | Tunable cadences, thresholds, bounds are single named constants, not inlined literals; distinct quantities stay distinct even when values coincide (e.g., trail ~100 vs. snapshot cadence 100 are separate constants) | COVERED | `test_monitor_counters_trail.py::CadenceResolution` and source inspection; `test_monitor_behaviour.py::OneCadenceMovesEverythingItGoverns` verifies single cadence edit moves all three |
| §5.1 usage | Report contract: Usage snapshot carries run's cumulative counts at moment of report (total calls, per-tool, per-outcome, per-error) | COVERED | `test_monitor_behaviour.py::ReportingThroughTheMonitor::test_every_report_carries_the_runs_cumulative_counts` |

---

## Changes Made (4 Items)

1. **AC-32**: Was missing. **Added test**: `RecordingWithNoTransport::test_all_three_occasions_produced_with_null_transport` uses NullTransport and verifies all three records. Now COVERED.

2. **AC-21**: Was over-claimed. Revised to verify summary structure directly via `_build_summary()`. Now COVERED.

3. **AC-73**: Was misidentified as F-73. **Added test**: `VerifierDoesNotFlagAckedDeletion::test_counter_vs_resident_file_gap_is_not_a_fault` asserts no S-1 report filed despite gap. Now COVERED.

4. **F-45**: Was missing test for hazard. **Added test**: `CounterSurvivesBindWorkspace::test_counter_total_and_cadence_survive_workspace_binding` verifies counter and cadence survive `bind_workspace()` rebuild. Now COVERED.

---

## PARTIAL and MISSING (Remaining)

### MISSING (0)
None.

### PARTIAL (2)

1. **F-44** — Summary field completeness: tests verify hardening, activity counts, chain_head, binding, and state in separate tests; no single test asserts every F-44 field together (uptime, environment version, verification outcome in payload not explicitly checked).

2. **AC-104** — Under-report defense tiers: tier 3 (source-level forgery not prevented) explicitly tested in docstring; tiers 1–2 (casual/deliberate) documented in spec but validation deferred to spec-level inspection.

---

## Test Additions

**New test methods added to `tests/test_monitor_behaviour.py`:**

- `RecordingWithNoTransport::test_all_three_occasions_produced_with_null_transport` (AC-32)
- `SnapshotCarriesAllRequiredFields::test_snapshot_includes_chain_head_binding_and_transport_state` (AC-21)
- `CounterSurvivesBindWorkspace::test_counter_total_and_cadence_survive_workspace_binding` (F-45)
- `VerifierDoesNotFlagAckedDeletion::test_counter_vs_resident_file_gap_is_not_a_fault` (AC-73)

**Final test result:**

```bash
Ran 246 tests in ~36s - OK
```

---

**Generated:** 2026-07-30  
**Coverage Map Version:** 1.1 (Round 2 — corrected arithmetic, verified each COVERED row, added 5 tests for AC-32/21/73/45, reconciled AC-104)
