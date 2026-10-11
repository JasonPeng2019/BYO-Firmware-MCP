"""Monitor-level behaviour: periodic ticks, reporting, and graceful degradation.

These drive the facade rather than its parts, so they cover the wiring between
detection and reporting that unit tests of each component cannot reach.
"""

from __future__ import annotations

import unittest
import json
import shutil
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from pyocd_debug_mcp.monitor.block import Anchor, BlockState
from pyocd_debug_mcp.monitor.monitor import (
    CHECKIN_PROMPT,
    ArtifactPersistence,
    IssueMonitor,
    NullMonitor,
)
from pyocd_debug_mcp.monitor.paths import StoreRoot, StoreState
from pyocd_debug_mcp.monitor.trail import TRAIL_MAX_EVENTS
from pyocd_debug_mcp.monitor.transport import TestTransport
from tests.monitor_support import MonitorTestCase, make_context


class ReportingThroughTheMonitor(MonitorTestCase):
    def setUp(self) -> None:
        super().setUp()
        # These tests inspect durable report bodies. TestTransport acknowledges
        # reports without retaining a disk copy, so a delivery-thread race can
        # otherwise remove the only readable file before the assertion.
        self.monitor = self.make_monitor()
        self.monitor.bind_workspace(None)

    def observe_failure(self, tool: str = "read_memory", exc: BaseException | None = None):
        observation = self.monitor.begin(tool, {"board_id": "b1"}, "b1")
        assert observation is not None
        observation.failed(exc or RuntimeError("kaboom"))

    def test_runtime_error_files_one_server_defect(self) -> None:
        self.observe_failure()
        reports = [self.read_report(p) for p in self.report_files()]
        defects = [r for r in reports if r["triage_class"] == "server_defect"]
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["signal_type"], "S-1")
        self.assertEqual(defects[0]["origin"], "server-auto")

    def test_report_body_replacement_is_atomic_and_cleans_failed_temporary(self) -> None:
        import os

        from unittest.mock import patch

        from pyocd_debug_mcp.monitor.monitor import _fsync_directory

        identity = "rpt-atomic-regression"
        creation_syncs = []

        def record_creation_sync(directory) -> None:  # type: ignore[no-untyped-def]
            creation_syncs.append(directory)
            _fsync_directory(directory)

        with patch(
            "pyocd_debug_mcp.monitor.monitor._fsync_directory",
            side_effect=record_creation_sync,
        ):
            self.assertIs(
                self.monitor._write_artifact_file({"report_id": identity, "generation": 1}),
                ArtifactPersistence.PERSISTED,
            )
        path = self.server_data / self.monitor._workspace / "reports" / f"{identity}.json"
        self.assertIn(path.parent.parent, creation_syncs)
        self.assertIn(path.parent, creation_syncs)
        original = path.read_bytes()

        with patch(
            "pyocd_debug_mcp.monitor.monitor.os.replace",
            side_effect=OSError("simulated interruption before atomic replacement"),
        ):
            self.assertIs(
                self.monitor._write_artifact_file({"report_id": identity, "generation": 2}),
                ArtifactPersistence.FAILED,
            )

        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(f".{identity}.*.tmp")), [])

        calls: list[str] = []
        original_replace = os.replace

        def replace_then_record(source: str, destination: str) -> None:
            if destination == path:
                calls.append("replace")
            original_replace(source, destination)

        def sync_then_record(directory) -> None:
            if directory == path.parent:
                calls.append("directory-fsync")
            _fsync_directory(directory)

        with (
            patch(
                "pyocd_debug_mcp.monitor.monitor.os.replace",
                side_effect=replace_then_record,
            ),
            patch(
                "pyocd_debug_mcp.monitor.monitor._fsync_directory",
                side_effect=sync_then_record,
            ),
        ):
            self.assertIs(
                self.monitor._write_artifact_file({"report_id": identity, "generation": 3}),
                ArtifactPersistence.PERSISTED,
            )
        self.assertEqual(calls, ["replace", "directory-fsync"])

        with patch(
            "pyocd_debug_mcp.monitor.monitor._fsync_directory",
            side_effect=OSError("simulated post-replace directory sync failure"),
        ):
            self.assertIs(
                self.monitor._write_artifact_file({"report_id": identity, "generation": 4}),
                ArtifactPersistence.UNCERTAIN,
            )
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["generation"], 4)

    def test_report_helper_waits_for_each_local_report_to_be_remote(self) -> None:
        old_remote = self.simulated_remote / "workspace" / "reports" / "rpt-old.json"
        new_local = self.server_data / "workspace" / "reports" / "rpt-new.json"
        old_remote.parent.mkdir(parents=True, exist_ok=True)
        new_local.parent.mkdir(parents=True, exist_ok=True)
        old_remote.write_text(json.dumps({"report_id": "rpt-old"}), encoding="utf-8")
        new_local.write_text(json.dumps({"report_id": "rpt-new"}), encoding="utf-8")

        def deliver_new() -> None:
            time.sleep(0.03)
            destination = old_remote.with_name(new_local.name)
            shutil.copyfile(new_local, destination)
            new_local.unlink()

        worker = threading.Thread(target=deliver_new)
        worker.start()
        try:
            reports = {path.name: path for path in self.report_files()}
        finally:
            worker.join()
        self.assertEqual(reports["rpt-new.json"].parent, old_remote.parent)
        self.assertEqual(self.read_report(reports["rpt-new.json"])["report_id"], "rpt-new")

    def test_runtime_error_report_omits_exception_secrets_payloads_and_paths(self) -> None:
        secret = "SECRET_TOKEN_ABC"
        windows_path = r"C:\Users\alice\private\fw.elf"
        payload = "CAFE" * 40
        self.observe_failure(exc=RuntimeError(f"{secret} {windows_path} {payload}"))

        reports = [self.read_report(path) for path in self.report_files()]
        self.assertEqual(len(reports), 1)
        persisted = str(reports[0])
        self.assertNotIn(secret, persisted)
        self.assertNotIn("alice", persisted)
        self.assertNotIn("fw.elf", persisted)
        self.assertNotIn(payload, persisted)
        self.assertIn("exception text was omitted", reports[0]["description"])

    def test_same_arguments_cannot_be_correlated_across_persisted_reports(self) -> None:
        self.observe_failure(exc=RuntimeError("first failure"))
        self.observe_failure(exc=ValueError("second failure"))

        reports = [self.read_report(path) for path in self.report_files()]
        self.assertEqual(len(reports), 2)
        self.assertEqual(len({report["args_fingerprint"] for report in reports}), 2)
        self.assertNotEqual(reports[0]["trail"][-1]["args_fp"], reports[1]["trail"][-1]["args_fp"])

    def test_raw_connection_identity_never_enters_trail_or_report(self) -> None:
        raw_connection = "probeid:stlink:SYNTHETIC-CONNECTION-SECRET-1234"
        context = replace(make_context(), connection_id=lambda board: raw_connection)
        monitor = self.make_monitor(context=context)
        monitor.bind_workspace(None)
        observation = monitor.begin("read_memory", {"board_id": "b1"}, "b1")
        assert observation is not None
        observation.failed(RuntimeError("failure"))

        reports = self.report_files()
        self.assertEqual(len(reports), 1)
        report = self.read_report(reports[0])
        persisted = str(report)
        self.assertNotIn(raw_connection, persisted)
        self.assertNotIn("SYNTHETIC-CONNECTION-SECRET-1234", persisted)
        self.assertRegex(str(report["trail"][-1]["connection"]), r"^[0-9a-f]{16}$")

    def test_environment_fault_is_tagged_not_a_code_defect(self) -> None:
        from pyocd_debug_mcp.target_errors import ProbeNotFoundError

        self.observe_failure(exc=ProbeNotFoundError("probe vanished"))
        reports = [self.read_report(p) for p in self.report_files()]
        self.assertTrue(reports)
        self.assertEqual(reports[-1]["signal_type"], "S-3")
        self.assertEqual(reports[-1]["triage_class"], "environment_fault")

    def test_report_storm_collapses_to_one_file(self) -> None:
        """One recurring exception must not generate a storm."""

        for _ in range(25):
            self.observe_failure()
        self.assertEqual(len(self.report_files()), 1)

    def test_suppressed_count_is_carried(self) -> None:
        for _ in range(5):
            self.observe_failure()
        report = self.read_report(self.report_files()[0])
        self.assertIn("suppressed_since_last", report)

    def test_every_report_carries_the_runs_cumulative_counts(self) -> None:
        """A report is self-describing about the activity surrounding the failure.

        The companion to the periodic snapshot: a problem report filed instead of
        a routine tick still carries the true running total, so usage cannot be
        understated by never reaching a snapshot boundary.
        """

        for index in range(3):
            observation = self.monitor.begin(f"probe{index}", {}, None)
            assert observation is not None
            observation.completed("ok")
        self.observe_failure()
        report = self.read_report(self.report_files()[0])
        usage = report["usage"]
        self.assertTrue(usage["cumulative"])
        # Three successes plus the failing call.
        self.assertEqual(usage["total_calls"], 4)
        self.assertEqual(usage["per_outcome"]["success"], 3)
        self.assertEqual(usage["per_outcome"]["unexpected_error"], 1)
        self.assertIn("read_memory", usage["per_tool"])

    def test_a_filed_report_is_a_ledger_occasion(self) -> None:
        """Reports are one of the five record kinds, alongside the snapshots."""

        self.observe_failure()
        records = [r for r in self.ledger_records() if r.get("kind") == "report"]
        self.assertEqual(len(records), 1)
        detail = records[0]["detail"]
        self.assertEqual(detail["signal_type"], "S-1")
        self.assertEqual(detail["triage_class"], "server_defect")
        self.assertTrue(detail["report_id"])
        self.assertTrue(detail["grouping_key"])
        # It carries the counts too, for the same reason the report itself does.
        self.assertTrue(detail["cumulative"])

    def test_refusals_never_file_a_server_defect(self) -> None:
        """A refusal that names a remedy is not an issue.

        Note that repeating the *same* refusal many times is legitimately
        thrashing, so this asserts the absence of server-defect reports rather
        than the absence of all reports.
        """

        from pyocd_debug_mcp.services.session_runtime import PolicyRefusal

        for _ in range(10):
            self.observe_failure(exc=PolicyRefusal("plan/gate-closed", "Validate first."))
        reports = [self.read_report(p) for p in self.report_files()]
        self.assertEqual([r for r in reports if r["triage_class"] == "server_defect"], [])

    def test_a_few_refusals_file_nothing_at_all(self) -> None:
        from pyocd_debug_mcp.services.session_runtime import PolicyRefusal

        for _ in range(2):
            self.observe_failure(exc=PolicyRefusal("plan/gate-closed", "Validate first."))
        self.assertEqual(self.report_files(), [])

    def test_trail_in_a_report_is_bounded(self) -> None:
        for index in range(TRAIL_MAX_EVENTS + 40):
            observation = self.monitor.begin(f"t{index}", {"board_id": "b1"}, "b1")
            assert observation is not None
            observation.completed("ok")
        self.observe_failure()
        report = self.read_report(self.report_files()[0])
        self.assertLessEqual(len(report["trail"]), TRAIL_MAX_EVENTS)

    def test_thrash_is_detected_through_the_monitor(self) -> None:
        for _ in range(8):
            observation = self.monitor.begin("read_memory", {"board_id": "b1"}, "b1")
            assert observation is not None
            observation.completed("Refused [plan/gate-closed]: Validate first.")
        reports = [self.read_report(path) for path in self.report_files()]
        thrash = [report for report in reports if report["signal_type"] == "S-2"]
        self.assertEqual(len(thrash), 1)
        self.assertEqual(thrash[0]["origin"], "server-thrash-detector")


class _TickingMonitor(MonitorTestCase):
    """Two cadences set far apart, so a test can tell which one fired.

    Scaled down from the real 100/500 but keeping the same relationship: the
    check-in boundary is a multiple of the snapshot boundary, so the call that
    trips a check-in trips a snapshot too.
    """

    SNAPSHOT_EVERY = 3
    CHECKIN_EVERY = 9

    def setUp(self) -> None:
        super().setUp()
        self.monitor = self.make_monitor(
            # These tests inspect local segment history; a successful async ACK
            # may legitimately delete older segments before the assertion.
            transport=TestTransport(fail_always=True),
            usage_snapshot_every=self.SNAPSHOT_EVERY,
            checkin_every=self.CHECKIN_EVERY,
        )
        self.monitor.bind_workspace(None)

    def tick(self, count: int) -> None:
        for index in range(count):
            observation = self.monitor.begin(f"tool{index}", {}, None)
            assert observation is not None
            observation.completed("ok")

    def kinds(self) -> list[str]:
        return [record["kind"] for record in self.ledger_records()]

    def snapshots(self) -> list[dict]:
        return [r for r in self.ledger_records() if r["kind"] == "usage_snapshot"]


class UsageSnapshotTick(_TickingMonitor):
    """The usage-snapshot occasion: a cumulative record every N calls."""

    def test_snapshot_record_is_written_at_the_boundary(self) -> None:
        self.tick(self.SNAPSHOT_EVERY)
        self.assertIn("usage_snapshot", self.kinds())

    def test_no_record_is_written_per_call(self) -> None:
        """The ledger records occasions, never one entry per tool call.

        Eight calls past two snapshot boundaries must produce two snapshot
        records, not eight call records.
        """

        self.tick(8)
        kinds = self.kinds()
        self.assertNotIn("call", kinds)
        self.assertEqual(kinds.count("usage_snapshot"), 2)
        # Boot plus two snapshots. Nothing scales with the call count.
        self.assertLessEqual(len(kinds), 4)

    def test_snapshots_carry_cumulative_counts(self) -> None:
        """Running totals, not per-window deltas.

        This is the anti-under-report property: because each snapshot carries the
        true running total, dropping an intermediate one cannot understate usage.
        """

        self.tick(6)
        snapshots = self.snapshots()
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(all(s["detail"]["cumulative"] for s in snapshots))
        self.assertEqual(snapshots[0]["detail"]["total_calls"], 3)
        self.assertEqual(snapshots[1]["detail"]["total_calls"], 6)

    def test_a_dropped_snapshot_does_not_lower_the_next_total(self) -> None:
        """Discarding the first snapshot leaves the true total in the second."""

        self.tick(6)
        snapshots = self.snapshots()
        surviving = snapshots[-1]["detail"]
        self.assertEqual(surviving["total_calls"], 6)
        self.assertEqual(sum(surviving["per_tool"].values()), 6)

    def test_snapshot_counts_every_tool_and_outcome(self) -> None:
        self.tick(3)
        detail = self.snapshots()[0]["detail"]
        self.assertEqual(detail["per_outcome"]["success"], 3)
        self.assertEqual(len(detail["per_tool"]), 3)

    def test_coverage_is_computed_against_the_live_advertised_set(self) -> None:
        """Tool visibility changes as plans are accepted, so a set captured at
        boot goes stale and under-reports what was never exercised.

        The snapshot must re-read the advertised set before computing coverage,
        not reuse whatever was current when the run started.
        """

        advertised = {"names": ("tool0", "tool1", "tool2")}
        monitor = self.make_monitor(
            context=make_context(advertised_tools=lambda: advertised["names"]),
            transport=TestTransport(),
            usage_snapshot_every=3,
            checkin_every=99,
        )
        monitor.bind_workspace(None)
        # A tool becomes visible only after the run has already begun.
        advertised["names"] = ("tool0", "tool1", "tool2", "late_arrival")
        for index in range(3):
            observation = monitor.begin(f"tool{index}", {}, None)
            assert observation is not None
            observation.completed("ok")
        detail = self.snapshots()[0]["detail"]
        self.assertIn(
            "late_arrival",
            detail["never_exercised"],
            "coverage was computed against a stale advertised set",
        )

    def test_segment_rolls_at_the_boundary(self) -> None:
        """The roll actually advances the segment and seals a file.

        ``len(files) >= 1`` is true from the boot record alone, before any
        roll ever runs -- it cannot tell a working roll from a disabled one
        (a no-op ``roll()`` still leaves >=1 file on disk). This asserts the
        segment number actually advanced, a sealed segment exists after each
        tick, and records landed in more than one distinct segment file.
        """

        self.tick(self.SNAPSHOT_EVERY)
        self.assertEqual(
            self.monitor._ledger.current_segment,
            2,
            "the first snapshot tick did not roll to a new segment",
        )
        self.assertTrue(
            self.monitor._ledger.sealed_segments(),
            "the first snapshot tick did not seal a segment",
        )
        segments_after_first = {r.get("segment") for r in self.ledger_records()}

        self.tick(self.SNAPSHOT_EVERY)
        self.assertEqual(
            self.monitor._ledger.current_segment,
            3,
            "the second snapshot tick did not roll again",
        )
        segments_after_second = {r.get("segment") for r in self.ledger_records()}
        self.assertGreater(
            len(segments_after_second),
            len(segments_after_first),
            "records after the second roll did not land in a new segment file",
        )

    def test_snapshot_is_not_an_issue(self) -> None:
        self.tick(self.SNAPSHOT_EVERY)
        self.assertEqual(self.report_files(), [])


class OneCadenceMovesEverythingItGoverns(MonitorTestCase):
    """Changing the snapshot cadence moves all three things it drives.

    Snapshot production, the segment roll, and the delivery handoff must ride the
    same number. If any of them kept its own copy, raising the cadence would move
    some and not others and the only-local window would silently drift away from
    the documented value.
    """

    def _run_at_cadence(self, cadence: int, calls: int) -> dict:
        # fail_always=True: nothing is ever ACKed, so the background delivery
        # thread never unlinks a segment's file out from under this count.
        # Rolling happens synchronously and locally regardless of delivery
        # outcome (it is not gated on a successful send), so this changes
        # nothing about what is being measured -- it only removes a race
        # against that thread. With a real-acking transport, a segment could
        # be delivered and deleted between the tick loop finishing and
        # ledger_records() reading it, silently undercounting "segments" and
        # making this comparison flaky rather than deterministic.
        monitor = self.make_monitor(
            transport=TestTransport(fail_always=True),
            usage_snapshot_every=cadence,
            checkin_every=cadence * 5,
        )
        monitor.bind_workspace(None)
        for index in range(calls):
            observation = monitor.begin(f"tool{index}", {}, None)
            assert observation is not None
            observation.completed("ok")
        records = self.ledger_records()
        snapshots = [r for r in records if r.get("kind") == "usage_snapshot"]
        return {
            "snapshots": len(snapshots),
            # Segments *created*, not segments resident: delete-on-ACK removes the
            # delivered ones, so counting files on disk would measure delivery
            # health rather than the roll cadence.
            "segments": len({(r.get("run_id"), r.get("segment")) for r in records}),
        }

    def test_halving_the_cadence_doubles_snapshots_and_segments(self) -> None:
        coarse = self._run_at_cadence(4, 8)
        self.assertEqual(coarse["snapshots"], 2)
        coarse_segments = coarse["segments"]

        self.tearDown()
        self.doCleanups()
        self.setUp()

        fine = self._run_at_cadence(2, 8)
        self.assertEqual(fine["snapshots"], 4)
        # The roll rides the same number, so more snapshots means more segments.
        self.assertGreater(fine["segments"], coarse_segments)


class CheckInTick(_TickingMonitor):
    """The check-in prompt runs on its own, longer cadence."""

    def test_no_prompt_at_a_snapshot_boundary(self) -> None:
        """A snapshot is not a check-in: the two cadences are independent."""

        self.tick(self.SNAPSHOT_EVERY)
        self.assertIn("usage_snapshot", self.kinds())
        self.assertIsNone(self.monitor.consume_checkin_prompt())

    def test_prompt_is_raised_once_at_the_checkin_boundary_then_cleared(self) -> None:
        self.tick(self.CHECKIN_EVERY)
        self.assertEqual(self.monitor.consume_checkin_prompt(), CHECKIN_PROMPT)
        # Cleared on emission, not on receipt: compliance is behavioural.
        self.assertIsNone(self.monitor.consume_checkin_prompt())

    def test_no_prompt_before_the_boundary(self) -> None:
        self.tick(self.CHECKIN_EVERY - 1)
        self.assertIsNone(self.monitor.consume_checkin_prompt())

    def test_the_checkin_boundary_also_trips_a_snapshot(self) -> None:
        """At call 9 both fire, exactly as call 500 does in the real cadences."""

        self.tick(self.CHECKIN_EVERY)
        self.assertEqual(self.snapshots()[-1]["detail"]["total_calls"], self.CHECKIN_EVERY)
        self.assertEqual(self.monitor.consume_checkin_prompt(), CHECKIN_PROMPT)


class ProfessionalBuildSuppressesTheCheckInPrompt(MonitorTestCase):
    def test_no_prompt_when_narrative_is_not_built(self) -> None:
        import pyocd_debug_mcp.monitor.monitor as monitor_module

        original = monitor_module.NARRATIVE_LOGGING
        monitor_module.NARRATIVE_LOGGING = False
        self.addCleanup(setattr, monitor_module, "NARRATIVE_LOGGING", original)
        monitor = self.make_monitor(
            transport=TestTransport(), usage_snapshot_every=2, checkin_every=2
        )
        monitor.bind_workspace(None)
        for index in range(4):
            observation = monitor.begin(f"t{index}", {}, None)
            assert observation is not None
            observation.completed("ok")
        self.assertIsNone(monitor.consume_checkin_prompt())


class GracefulDegradation(MonitorTestCase):
    def test_uncertain_automatic_summary_is_recovered_only_after_restart(self) -> None:
        from unittest.mock import patch

        monitor = self.make_monitor(
            context=make_context(run_id="run-auto-uncertain"),
            usage_snapshot_every=1,
        )
        monitor.bind_workspace(None)
        report_directory = self.server_data / monitor._workspace / "reports"
        report_directory.mkdir(parents=True, exist_ok=True)
        with (
            patch(
                "pyocd_debug_mcp.monitor.monitor._fsync_directory",
                side_effect=OSError("simulated post-publish sync failure"),
            ),
            patch.object(monitor._delivery, "enqueue_report") as enqueue,
        ):
            observation = monitor.begin("get_state", {"board_id": "b"}, "b")
            assert observation is not None
            observation.completed("ok")
        enqueue.assert_not_called()
        snapshots = [
            record
            for record in self.ledger_records()
            if record["kind"] == "usage_snapshot" and record["run_id"] == "run-auto-uncertain"
        ]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["detail"]["body_persistence"], "uncertain")
        self.assertFalse(snapshots[0]["detail"]["delivery_enqueued"])
        summary_id = snapshots[0]["detail"]["summary_id"]
        self.assertTrue((report_directory / f"{summary_id}.json").is_file())

        monitor.closeout("simulated-restart")
        restarted = self.make_monitor(context=make_context(run_id="run-auto-restart"))
        restarted.bind_workspace(None)
        recovered = self.simulated_remote / restarted._workspace / "reports" / f"{summary_id}.json"
        deadline = time.monotonic() + 2.0
        while not recovered.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(recovered.is_file())

    def test_unpersisted_automatic_bodies_are_disclaimed_and_not_enqueued(self) -> None:
        from unittest.mock import patch

        scenarios = (("success", 1, "usage_snapshot"), ("failure", 0, "report"))
        for persistence in (ArtifactPersistence.FAILED, ArtifactPersistence.UNCERTAIN):
            for outcome, cadence, record_kind in scenarios:
                with self.subTest(outcome=outcome, persistence=persistence.value):
                    run_id = f"run-{outcome}-{persistence.value}"
                    monitor = self.make_monitor(
                        context=make_context(run_id=run_id),
                        transport=TestTransport(),
                        usage_snapshot_every=cadence,
                    )
                    monitor.bind_workspace(None)
                    with (
                        patch.object(
                            monitor,
                            "_write_artifact_file",
                            return_value=persistence,
                        ),
                        patch.object(monitor._delivery, "enqueue_report") as enqueue,
                    ):
                        observation = monitor.begin("get_state", {"board_id": "b"}, "b")
                        assert observation is not None
                        if outcome == "success":
                            observation.completed("ok")
                        else:
                            observation.failed(RuntimeError("boom"))
                    enqueue.assert_not_called()
                    records = [
                        record
                        for record in self.ledger_records()
                        if record["kind"] == record_kind and record["run_id"] == run_id
                    ]
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0]["detail"]["body_persistence"], persistence.value)
                    self.assertFalse(records[0]["detail"]["body_persisted"])
                    self.assertFalse(records[0]["detail"]["delivery_enqueued"])
                    monitor.closeout("test")

    def test_monitor_degrades_when_the_store_is_unavailable(self) -> None:
        """If monitoring cannot write, tool execution proceeds unchanged."""

        monitor = self.make_monitor(transport=TestTransport())
        monitor._store = StoreRoot(StoreState.BUFFERING, None)
        monitor.bind_workspace(None)
        observation = monitor.begin("get_state", {"board_id": "b"}, "b")
        assert observation is not None
        observation.completed("ok")
        observation2 = monitor.begin("get_state", {"board_id": "b"}, "b")
        assert observation2 is not None
        observation2.failed(RuntimeError("boom"))
        health = monitor.health()
        # Exactly two calls were made, single-threaded, with no concurrency:
        # the count is deterministic, not merely a lower bound.
        self.assertEqual(health["counters"]["total"], 2)

    def test_counter_leads_the_ledger_without_an_anomaly(self) -> None:
        monitor = self.make_monitor(transport=TestTransport())
        monitor._store = StoreRoot(StoreState.BUFFERING, None)
        monitor.bind_workspace(None)
        for _ in range(3):
            observation = monitor.begin("t", {}, None)
            assert observation is not None
            observation.completed("ok")
        health = monitor.health()
        self.assertGreater(health["recording"]["counter_minus_appended"], 0)

    def test_internal_failures_never_produce_a_report(self) -> None:
        """A failure inside the reporting path must not report on itself."""

        monitor = self.make_monitor(transport=TestTransport())
        monitor.bind_workspace(None)
        broken = make_context()
        object.__setattr__(
            broken,
            "connection_id",
            lambda board: 1 / 0,  # type: ignore[misc]
        )
        monitor._ctx = broken
        observation = monitor.begin("t", {"board_id": "b"}, "b")
        assert observation is not None
        observation.completed("ok")
        self.assertEqual(self.report_files(), [])


class NullMonitorSurface(unittest.TestCase):
    """Monitoring failing closed would be worse than monitoring being absent."""

    def setUp(self) -> None:
        self.monitor = NullMonitor("store unavailable")

    def test_satisfies_the_whole_surface(self) -> None:
        self.assertIsNone(self.monitor.begin("t", {}, None))
        self.assertIsNone(self.monitor.consume_checkin_prompt())
        self.monitor.bind_workspace(None)
        self.monitor.boot()
        self.monitor.closeout("test")
        self.monitor.check_block()

    def test_health_declares_itself_unavailable_not_healthy(self) -> None:
        health = self.monitor.health()
        self.assertEqual(health["monitoring"], "unavailable")
        self.assertIn("store unavailable", health["reason"])

    def test_submissions_are_refused_honestly(self) -> None:
        self.assertEqual(self.monitor.submit_report({})["status"], "monitor_unavailable")
        self.assertEqual(self.monitor.submit_checkin({})["status"], "monitor_unavailable")

    def test_the_surface_matches_the_real_monitor(self) -> None:
        for name in (
            "begin",
            "bind_workspace",
            "boot",
            "closeout",
            "check_block",
            "consume_checkin_prompt",
            "health",
            "submit_report",
            "submit_checkin",
        ):
            with self.subTest(method=name):
                self.assertTrue(hasattr(IssueMonitor, name))
                self.assertTrue(hasattr(NullMonitor, name))


class BlockRefusesThroughTheMonitor(MonitorTestCase):
    def test_tripped_block_refuses_with_a_named_remedy(self) -> None:
        from pyocd_debug_mcp.services.session_runtime import PolicyRefusal

        monitor = self.make_monitor(transport=TestTransport())
        monitor._block.inject(
            Anchor(datetime.now(timezone.utc) - timedelta(days=30), "filler", "filler")
        )
        self.assertIs(monitor._block.state(), BlockState.TRIPPED)
        with self.assertRaises(PolicyRefusal) as caught:
            monitor.check_block()
        message = str(caught.exception)
        self.assertIn("deliver", message.lower())
        self.assertEqual(caught.exception.code, "monitor/logging-stale")

    def test_the_block_refusal_classifies_as_a_refusal_not_a_defect(self) -> None:
        """It names its remedy, so it is correct behaviour rather than a defect."""

        from pyocd_debug_mcp.monitor.classify import (
            Outcome,
            TriageClass,
            classify_exception,
        )
        from pyocd_debug_mcp.services.session_runtime import PolicyRefusal

        outcome, triage, _ = classify_exception(
            PolicyRefusal("monitor/logging-stale", "Restore network and deliver.")
        )
        self.assertIs(outcome, Outcome.POLICY_REFUSAL)
        self.assertIs(triage, TriageClass.NONE)

    def test_dormant_and_armed_blocks_do_not_refuse(self) -> None:
        monitor = self.make_monitor(transport=TestTransport())
        monitor._block.inject(None)
        monitor.check_block()
        monitor._block.inject(
            Anchor(datetime.now(timezone.utc) - timedelta(days=1), "filler", "filler")
        )
        monitor.check_block()

    def test_check_block_does_no_file_io(self) -> None:
        """It runs inside a held board lock on every guarded call."""

        monitor = self.make_monitor(transport=TestTransport())
        monitor._block.inject(
            Anchor(datetime.now(timezone.utc) - timedelta(days=1), "filler", "filler")
        )
        import builtins

        opened: list[str] = []
        real_open = builtins.open

        def tracking_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        builtins.open = tracking_open
        try:
            monitor.check_block()
        finally:
            builtins.open = real_open
        self.assertEqual(opened, [])


class RecordingWithNoTransport(MonitorTestCase):
    """AC-32: Boot + snapshot + close records produced with no transport configured."""

    def test_all_three_occasions_produced_with_null_transport(self) -> None:
        """Boot, usage snapshot, and close records appear even without transport."""
        from pyocd_debug_mcp.monitor.transport import NullTransport

        monitor = self.make_monitor(
            transport=NullTransport(),
            usage_snapshot_every=2,
            checkin_every=99,
        )
        monitor.bind_workspace(None)
        monitor.boot()

        # Trigger a snapshot
        for _ in range(2):
            observation = monitor.begin("test_tool", {}, None)
            assert observation is not None
            observation.completed("ok")

        monitor.closeout("test")

        kinds = [record["kind"] for record in self.ledger_records()]
        self.assertIn("boot", kinds)
        self.assertIn("usage_snapshot", kinds)
        self.assertIn("close", kinds)


class SnapshotCarriesAllRequiredFields(MonitorTestCase):
    """AC-21: Snapshot carries cumulative counts, coverage, chain head, binding state, transport state."""

    def test_snapshot_includes_chain_head_binding_and_transport_state(self) -> None:
        """AC-21: Snapshot summary carries all required fields (structure verified, delivery tested elsewhere)."""
        from pyocd_debug_mcp.monitor.transport import TestTransport

        monitor = self.make_monitor(
            transport=TestTransport(),
            usage_snapshot_every=2,
            checkin_every=99,
        )
        monitor.bind_workspace(None)
        monitor.boot()

        for _ in range(2):
            observation = monitor.begin("test_tool", {}, None)
            assert observation is not None
            observation.completed("ok")

        # AC-21: Verify the summary structure that _usage_snapshot_tick builds
        # (the actual delivery through the background thread is integration-tested elsewhere
        # in the codex e2e tests and in AStuckSenderIsInvisible)
        snapshot = monitor._counters.snapshot()
        summary = monitor._build_summary(trigger=str(snapshot.total), snapshot=snapshot)

        # Verify all AC-21 required fields are present in built summary structure
        self.assertIn("total", summary.get("activity", {}), "Missing total in activity")
        self.assertIn("per_tool", summary.get("activity", {}), "Missing per_tool in activity")
        self.assertIn("per_outcome", summary.get("activity", {}), "Missing per_outcome in activity")
        self.assertIn("exercised", summary.get("coverage", {}), "Missing exercised in coverage")
        self.assertIn(
            "never_exercised", summary.get("coverage", {}), "Missing never_exercised in coverage"
        )
        self.assertIn("chain_head", summary.get("ledger", {}), "Missing chain_head in ledger")
        self.assertIn(
            "workspace_bound", summary.get("delivery", {}), "Missing workspace_bound in delivery"
        )
        self.assertIn("state", summary.get("delivery", {}), "Missing state in delivery")


class CounterSurvivesBindWorkspace(MonitorTestCase):
    """F-45: Counter does not reset when bind_workspace() rebuilds monitor internals."""

    def test_counter_total_and_cadence_survive_workspace_binding(self) -> None:
        """F-45: Counter total and cadence boundaries are unaffected by bind_workspace().

        The hazard: bind_workspace() rebuilds self._ledger and self._delivery.
        Future changes could also rebuild self._counters, silently resetting counts.
        This test verifies that cannot happen silently — counter total and cadence
        boundaries survive the binding event.
        """
        from pyocd_debug_mcp.monitor.transport import TestTransport

        monitor = self.make_monitor(
            transport=TestTransport(fail_always=True),
            usage_snapshot_every=3,
            checkin_every=99,
        )
        monitor.boot()
        prebind_body = {
            "report_id": "rpt-prebind-migration",
            "kind": "test",
            "session_id": monitor._ctx.run_id,
        }
        self.assertIs(monitor._write_artifact_file(prebind_body), ArtifactPersistence.PERSISTED)
        unbound_body = self.server_data / "unbound" / "reports" / "rpt-prebind-migration.json"
        self.assertTrue(unbound_body.is_file())
        foreign_body = {
            "report_id": "rpt-other-prebind-run",
            "kind": "test",
            "session_id": "another-live-run",
        }
        self.assertIs(monitor._write_artifact_file(foreign_body), ArtifactPersistence.PERSISTED)
        foreign_unbound = self.server_data / "unbound" / "reports" / "rpt-other-prebind-run.json"
        conflicting_body = {
            "report_id": "rpt-prebind-conflict",
            "kind": "source",
            "session_id": monitor._ctx.run_id,
        }
        self.assertIs(monitor._write_artifact_file(conflicting_body), ArtifactPersistence.PERSISTED)
        conflicting_unbound = self.server_data / "unbound" / "reports" / "rpt-prebind-conflict.json"

        # Make 2 calls
        for _ in range(2):
            observation = monitor.begin("tool_a", {}, None)
            assert observation is not None
            observation.completed("ok")

        count_before_bind = monitor._counters.snapshot().total
        self.assertEqual(count_before_bind, 2)

        # Bind workspace (rebuilds ledger, delivery, etc.)
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            from pyocd_debug_mcp.monitor import paths

            target_workspace = paths.workspace_id(project)
            prior_directory = self.server_data / target_workspace
            prior_directory.mkdir(parents=True, exist_ok=True)
            (prior_directory / "corrupt-prior.0001.jsonl").write_text(
                '{"hash":"bad","prev":0}\n', encoding="utf-8"
            )
            conflicting_bound = prior_directory / "reports" / "rpt-prebind-conflict.json"
            conflicting_bound.parent.mkdir(parents=True, exist_ok=True)
            conflicting_bound.write_text('{"kind":"unrelated"}\n', encoding="utf-8")
            monitor.bind_workspace(project)
            self.assertTrue(monitor._bound)
            self.assertEqual(
                monitor._verification["corrupt-prior.0001.jsonl"],
                "chain_invalid",
            )
            bound_body = (
                self.server_data / monitor._workspace / "reports" / "rpt-prebind-migration.json"
            )
            self.assertTrue(bound_body.is_file())
            self.assertFalse(unbound_body.exists())
            self.assertTrue(foreign_unbound.is_file())
            self.assertTrue(conflicting_unbound.is_file())
            self.assertEqual(
                json.loads(conflicting_bound.read_text(encoding="utf-8"))["kind"],
                "unrelated",
            )

        # Make 1 more call (total should be 3, cadence should trigger at 3)
        observation = monitor.begin("tool_b", {}, None)
        assert observation is not None
        observation.completed("ok")

        count_after_bind = monitor._counters.snapshot().total
        self.assertEqual(count_after_bind, 3, "Counter reset after bind_workspace()")
        # Verify cadence is still measured from run start (call 3 triggers snapshot)
        snapshots = [r for r in self.ledger_records() if r.get("kind") == "usage_snapshot"]
        self.assertTrue(
            snapshots,
            "Cadence did not fire at call 3 (would have reset if binding restarted counter)",
        )


class VerifierDoesNotFlagAckedDeletion(MonitorTestCase):
    """AC-73: Verifier does not flag ACK-deleted files as tamper, counter-vs-file gap not a fault."""

    def test_counter_vs_resident_file_gap_is_not_a_fault(self) -> None:
        """AC-73: a counter-vs-resident-file gap must not be treated as a fault
        at all -- not filed as a server defect, and not filed as anything else.

        Excluding only ``signal_type == "S-1"`` does not prove that: a bad
        detector could file the same complaint under any other signal (S-2
        thrash, S-8 coverage gap, a new one entirely) and slip straight past a
        check that only rules out one value. The requirement is "not treated
        as a fault", so what is asserted is that the gap produces no report at
        all.
        """
        from pyocd_debug_mcp.monitor.transport import TestTransport

        monitor = self.make_monitor(
            transport=TestTransport(),
            usage_snapshot_every=2,
            checkin_every=99,
        )
        monitor.bind_workspace(None)

        # Distinct tool names: calling the same tool repeatedly with identical
        # args and outcome is legitimately thrashing (S-2) and would confound
        # this scenario with an unrelated, correct report -- exactly the kind
        # of noise a signal_type-specific check would silently let through.
        for index in range(4):
            observation = monitor.begin(f"tool{index}", {}, None)
            assert observation is not None
            observation.completed("ok")

        # Settle delivery so the gap this test is about has actually happened,
        # rather than asserting against a race with the background sender.
        monitor._delivery.stop(timeout=1.0)
        health = monitor.health()
        self.assertLess(
            health["ledger"]["resident_files"],
            health["counters"]["total"],
            "no gap was created; delivery may not have run, so this proves nothing",
        )

        # AC-73: the gap produces no report of any kind, not merely no S-1.
        self.assertEqual(
            self.report_files(),
            [],
            "the counter-vs-resident-file gap produced a report",
        )


if __name__ == "__main__":
    unittest.main()
