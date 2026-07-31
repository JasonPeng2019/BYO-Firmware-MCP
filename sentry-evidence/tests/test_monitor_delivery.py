"""Delivery, the transport seam, and the self-draining spool.

The filler must never masquerade as a working transport: a stub that reports
success would leave months of sessions believed to be archived remotely while
nothing ever left the machine.
"""

from __future__ import annotations

import json
import threading
import time
import unittest

from pyocd_debug_mcp.monitor.delivery import DeliveryService
from pyocd_debug_mcp.monitor.ledger import SegmentLedger, file_identity
from pyocd_debug_mcp.monitor.paths import resolve_store_root
from pyocd_debug_mcp.monitor.transport import (
    DeliveryState,
    NullTransport,
    SimulatedRemoteTransport,
    TestTransport,
    _as_sentry_event,
)
from tests.monitor_support import MonitorTestCase
from tests.test_monitor_ledger import add_snapshot


class TransportStates(MonitorTestCase):
    def test_null_transport_is_not_configured(self) -> None:
        result = NullTransport().send_files([])
        self.assertIs(result.state, DeliveryState.NOT_CONFIGURED)
        self.assertFalse(result.is_durable_off_box)

    def test_filler_reports_simulated_never_sent(self) -> None:
        store = resolve_store_root()
        ledger = SegmentLedger(store, "ws", "run-1")
        add_snapshot(ledger)
        sealed = ledger.seal()
        assert sealed is not None
        transport = SimulatedRemoteTransport(store.simulated_remote, "ws")
        result = transport.send_files([(sealed.identity, sealed.path)])
        self.assertIs(result.state, DeliveryState.FILLER_SIMULATED)
        self.assertIsNot(result.state, DeliveryState.SENT)

    def test_filler_is_never_a_durable_off_box_copy(self) -> None:
        store = resolve_store_root()
        transport = SimulatedRemoteTransport(store.simulated_remote, "ws")
        result = transport.send_report({"report_id": "rpt-1", "title": "x"})
        self.assertFalse(result.is_durable_off_box)

    def test_test_transport_can_fail_always(self) -> None:
        transport = TestTransport(fail_always=True)
        self.assertIs(transport.send_files([]).state, DeliveryState.FAILED)


class SentryEnvelopeMapping(unittest.TestCase):
    """Reports are built as genuine Sentry events so cutover is configuration."""

    def test_grouping_key_becomes_the_fingerprint(self) -> None:
        event = _as_sentry_event({"grouping_key": "abc123", "title": "t"})
        self.assertEqual(event["fingerprint"], ["abc123"])

    def test_classification_becomes_tags(self) -> None:
        event = _as_sentry_event(
            {
                "signal_type": "S-1",
                "triage_class": "server_defect",
                "origin": "server-auto",
                "tool_name": "flash",
                "environment": {"narrative_logging": "enabled"},
            }
        )
        self.assertEqual(event["tags"]["signal_type"], "S-1")
        self.assertEqual(event["tags"]["triage_class"], "server_defect")
        self.assertEqual(event["tags"]["narrative_logging"], "enabled")

    def test_trail_becomes_breadcrumbs(self) -> None:
        event = _as_sentry_event(
            {"trail": [{"tool": "get_state", "outcome": "success", "board": "b"}]}
        )
        self.assertEqual(len(event["breadcrumbs"]["values"]), 1)
        self.assertEqual(event["breadcrumbs"]["values"][0]["message"], "get_state")

    def test_severity_becomes_level(self) -> None:
        self.assertEqual(_as_sentry_event({"severity": "error"})["level"], "error")


class SelfDrainingSpool(MonitorTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = resolve_store_root()
        self.ledger = SegmentLedger(self.store, "ws", "run-1")

    def _sealed(self):
        add_snapshot(self.ledger)
        sealed = self.ledger.seal()
        assert sealed is not None
        return sealed

    def test_ack_deletes_the_local_copy(self) -> None:
        sealed = self._sealed()
        service = DeliveryService(
            self.store, "ws", "run-1", TestTransport(), None
        )
        service.enqueue_segments([sealed])
        service.start()
        self.addCleanup(service.stop)
        import time

        for _ in range(200):
            if not sealed.path.exists():
                break
            time.sleep(0.02)
        self.assertFalse(sealed.path.exists())

    def test_unacked_file_is_never_deleted(self) -> None:
        sealed = self._sealed()
        service = DeliveryService(
            self.store, "ws", "run-1", TestTransport(fail_always=True), None
        )
        service.drain_for_closeout([sealed], budget=1.0)
        self.assertTrue(sealed.path.exists())

    def test_simulated_remote_retains_the_delivered_copy(self) -> None:
        sealed = self._sealed()
        transport = SimulatedRemoteTransport(self.store.simulated_remote, "ws")
        service = DeliveryService(self.store, "ws", "run-1", transport, None)
        service.drain_for_closeout([sealed], budget=5.0)
        self.assertFalse(sealed.path.exists())
        copies = list((self.store.simulated_remote / "ws" / "ledger").glob("*.jsonl"))
        self.assertEqual(len(copies), 1)

    def test_delivery_progress_is_durable(self) -> None:
        sealed = self._sealed()
        service = DeliveryService(self.store, "ws", "run-1", TestTransport(), None)
        service.drain_for_closeout([sealed], budget=5.0)
        state = json.loads((self.server_data / "delivery_state.json").read_text())
        self.assertIn(sealed.identity, state["acked"])

    def test_recovery_does_not_resend_acked_content(self) -> None:
        import time

        acked = self._sealed()
        first = DeliveryService(self.store, "ws", "run-1", TestTransport(), None)
        first.drain_for_closeout([acked], budget=5.0)

        # A second, still-undelivered file proves the queue really drained, so
        # the absence of the acked one is a decision rather than an idle service.
        second_ledger = SegmentLedger(self.store, "ws", "run-2")
        add_snapshot(second_ledger, total=200)
        pending = second_ledger.seal()
        assert pending is not None

        transport = TestTransport()
        service = DeliveryService(self.store, "ws", "run-2", transport, None)
        service.start()
        self.addCleanup(service.stop)
        service.enqueue_segments([acked, pending])
        for _ in range(200):
            if transport.sent_files:
                break
            time.sleep(0.02)
        self.assertEqual(transport.sent_files, [pending.identity])
        self.assertNotIn(acked.identity, transport.sent_files)

    def test_resend_is_tolerated_and_identity_is_stable(self) -> None:
        """Crash timing can always produce a resend, so identity must be stable."""

        sealed = self._sealed()
        transport = TestTransport()
        first = DeliveryService(self.store, "ws", "run-1", transport, None)
        first.drain_for_closeout([sealed], budget=5.0)
        sent_once = list(transport.sent_files)

        # Replay the same identity as a crash-timing duplicate would.
        result = transport.send_files([(sealed.identity, sealed.path)])
        self.assertIn(sealed.identity, result.acked)
        self.assertEqual(sent_once, [sealed.identity])
        self.assertEqual(transport.sent_files.count(sealed.identity), 2)

    def test_identity_is_stable_for_receiver_dedup(self) -> None:
        self.assertEqual(
            file_identity("ws", "run-1", 1), file_identity("ws", "run-1", 1)
        )


class ReportDeleteOnACK(MonitorTestCase):
    """Report/summary files are deleted from disk once ACKed, mirroring segment behavior."""

    def setUp(self) -> None:
        super().setUp()
        self.store = resolve_store_root()

    def test_report_file_is_deleted_from_disk_once_acked_falsified(self) -> None:
        """Falsification: patch out the delete call and confirm the test fails.

        This test demonstrates that test_report_file_is_deleted_from_disk_once_acked
        is evidence by showing it would fail if the delete behavior were removed.
        """

        import unittest.mock as mock

        # Patch out the unlink call that deletes the report file
        with mock.patch("pathlib.Path.unlink", side_effect=lambda **kwargs: None):
            report = {"report_id": "rpt-falsify-delete-test", "title": "test"}

            # Write the file to disk
            report_dir = self.server_data / "ws" / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            report_file = report_dir / "rpt-falsify-delete-test.json"
            report_file.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )

            service = DeliveryService(self.store, "ws", "run-1", TestTransport(), None)
            service.start()
            self.addCleanup(service.stop)
            service.enqueue_report(report)

            # Wait briefly
            for _ in range(200):
                if not report_file.exists():
                    break
                time.sleep(0.02)

            # With the delete patched out, the file should still exist
            # (this is what would happen if the implementation didn't delete)
            self.assertTrue(report_file.exists(),
                           "Falsification: file should NOT be deleted when unlink is patched"
                           " (proving real test catches deletion)")

    def test_report_file_is_deleted_from_disk_once_acked(self) -> None:
        """Report file is deleted from disk once ACKed by the transport."""

        # Create and enqueue a report through the delivery service
        service = DeliveryService(self.store, "ws", "run-1", TestTransport(), None)
        report = {"report_id": "rpt-ack-delete-test", "title": "test"}

        # Write the file to disk (as monitor.py does)
        report_dir = self.server_data / "ws" / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_file = report_dir / "rpt-ack-delete-test.json"
        report_file.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self.assertTrue(report_file.exists())

        # Enqueue and wait for delivery
        service.start()
        self.addCleanup(service.stop)
        service.enqueue_report(report)

        # Wait for the file to be deleted (poll with timeout)
        for _ in range(200):
            if not report_file.exists():
                break
            time.sleep(0.02)

        self.assertFalse(report_file.exists(), "Report file should be deleted after ACK")

    def test_report_file_is_not_deleted_before_ack(self) -> None:
        """Report file is not deleted if send fails or is not yet acknowledged."""

        transport = TestTransport(fail_always=True)
        service = DeliveryService(self.store, "ws", "run-1", transport, None)
        report = {"report_id": "rpt-no-delete-on-fail", "title": "test"}

        # Write the file to disk
        report_dir = self.server_data / "ws" / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_file = report_dir / "rpt-no-delete-on-fail.json"
        report_file.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        # Start the daemon thread so reports are actually processed
        service.start()
        self.addCleanup(service.stop)

        # Enqueue the report for delivery with a failing transport
        service.enqueue_report(report)

        # Poll for _send_report to have run (it will fail, but should run).
        # _last_state starts as NOT_CONFIGURED and changes on any send attempt.
        for _ in range(200):
            if service._last_state != DeliveryState.NOT_CONFIGURED:
                break
            time.sleep(0.02)

        # Since the send failed (fail_always=True), the file should still exist
        self.assertTrue(report_file.exists(), "Report file should survive a failed send")

    def test_report_file_is_not_deleted_before_ack_falsified(self) -> None:
        """Falsification: patch _send_report to delete unconditionally.

        This demonstrates that test_report_file_is_not_deleted_before_ack
        would fail if _send_report deleted the file regardless of ACK outcome.
        """

        import unittest.mock as mock

        transport = TestTransport(fail_always=True)
        service = DeliveryService(self.store, "ws", "run-1", transport, None)
        report = {"report_id": "rpt-falsify-fail-delete", "title": "test"}

        # Write the file to disk
        report_dir = self.server_data / "ws" / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_file = report_dir / "rpt-falsify-fail-delete.json"
        report_file.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        # Start daemon thread
        service.start()
        self.addCleanup(service.stop)

        # Patch _send_report to unconditionally delete the file after sending
        # (simulating the bug: deletion regardless of ACK outcome)
        original_send_report = service._send_report

        def buggy_send_report(report_dict: dict[str, any]) -> None:  # type: ignore[type-arg]
            """Buggy version that deletes regardless of ACK."""
            original_send_report(report_dict)
            # Bug: delete the file unconditionally (regardless of ACK)
            identity = str(
                report_dict.get("report_id") or report_dict.get("summary_id") or ""
            )
            if identity:
                server_data = service._store.server_data
                if server_data is not None:
                    path = server_data / service._workspace / "reports" / f"{identity}.json"
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass

        with mock.patch.object(service, "_send_report", side_effect=buggy_send_report):
            service.enqueue_report(report)

            # Poll for the send to complete using _last_state change
            for _ in range(200):
                if service._last_state != DeliveryState.NOT_CONFIGURED:
                    break
                time.sleep(0.02)

        # With the buggy version, file would be deleted even though send failed
        self.assertFalse(report_file.exists(),
                        "Falsification: file was deleted unconditionally by buggy _send_report"
                        " (proving real test catches the bug)")


class DeliveryStatePruning(MonitorTestCase):
    """Prune _acked entries when their backing files are confirmed absent."""

    def setUp(self) -> None:
        super().setUp()
        self.store = resolve_store_root()

    def test_segment_acked_entry_is_pruned_when_file_is_gone_falsified(self) -> None:
        """Falsification: disable pruning and confirm test would fail.

        This test demonstrates that test_segment_acked_entry_is_pruned_when_file_is_gone
        is evidence by showing it would fail if pruning were disabled.
        """

        import unittest.mock as mock

        # Create delivery service
        service = DeliveryService(self.store, "ws", "run-test", TestTransport(), None)

        # Pre-populate _acked with a fake segment that doesn't exist
        fake_identity = "ws:run-old-falsify:0001"
        service._acked.add(fake_identity)
        service._save_state()

        # Verify it's in the state file
        state_file = self.server_data / "delivery_state.json"
        state = json.loads(state_file.read_text())
        self.assertIn(fake_identity, state["acked"])

        # Patch _prune_acked to do nothing (disable pruning)
        with mock.patch.object(service, '_prune_acked', return_value=None):
            # Add entries to trigger the pruning boundary
            for i in range(210):
                ledger = SegmentLedger(self.store, "ws", f"run-falsify-{i:03d}")
                add_snapshot(ledger)
                sealed = ledger.seal()
                if sealed is None:
                    continue
                service._acked.add(sealed.identity)
                service._save_state()

        # With pruning disabled, the fake entry should still be there
        state = json.loads(state_file.read_text())
        self.assertIn(
            fake_identity, state["acked"],
            "Falsification: entry should NOT be pruned when _prune_acked is patched"
            " (proving real test catches pruning)"
        )

    def test_segment_acked_entry_is_pruned_when_file_is_gone(self) -> None:
        """A segment _acked entry is pruned once its file is confirmed absent.

        This test works by:
        1. Pre-populating _acked with a fake entry for a non-existent segment
        2. Creating many real segments that get ACKed, triggering pruning at boundaries
        3. Verifying the fake entry was pruned (since its file doesn't exist)
        """

        # Create delivery service
        service = DeliveryService(self.store, "ws", "run-test", TestTransport(), None)

        # Pre-populate _acked with a fake segment that doesn't exist on disk
        # Segments use identity format: workspace:run_id:segment_number
        fake_identity = "ws:run-old:0001"
        service._acked.add(fake_identity)
        service._save_state()

        # Verify it's in the state file
        state_file = self.server_data / "delivery_state.json"
        state = json.loads(state_file.read_text())
        self.assertIn(fake_identity, state["acked"])

        # Now create and deliver real segments to trigger pruning via _save_state.
        # We need to cross the DELIVERY_STATE_PRUNE_INTERVAL boundary (200 entries).
        # Add entries until we hit and pass the 200 boundary to trigger pruning.
        for i in range(210):
            # Create a ledger for each segment
            ledger = SegmentLedger(self.store, "ws", f"run-{i:03d}")
            add_snapshot(ledger)
            sealed = ledger.seal()
            if sealed is None:
                continue
            # ACK it directly to add to _acked and trigger pruning in _save_state
            service._acked.add(sealed.identity)
            service._save_state()

        # After hitting the prune boundary, the fake entry should be removed
        state = json.loads(state_file.read_text())
        self.assertNotIn(
            fake_identity, state["acked"],
            "Segment entry should be pruned when file is confirmed absent"
        )

    def test_acked_entry_is_never_pruned_if_file_still_exists_falsified(self) -> None:
        """Falsification: make _identity_file_exists lie and confirm test fails.

        This regression-test falsification demonstrates that without the
        existence check, entries whose files still exist would be pruned,
        losing the crash-window protection.
        """

        import unittest.mock as mock

        # Create delivery service
        service = DeliveryService(self.store, "ws", "run-test", TestTransport(), None)

        # Create a real segment file that we'll keep
        ledger = SegmentLedger(self.store, "ws", "run-keep")
        add_snapshot(ledger)
        sealed = ledger.seal()
        assert sealed is not None
        self.assertTrue(sealed.path.exists())

        # Add it to _acked
        service._acked.add(sealed.identity)
        service._save_state()

        # Patch _identity_file_exists to lie and say the file doesn't exist
        with mock.patch.object(
            service, '_identity_file_exists',
            return_value=False  # Lie: pretend file is absent
        ):
            # Add entries to cross the pruning boundary
            for i in range(210):
                ledger = SegmentLedger(self.store, "ws", f"run-falsify-{i:03d}")
                add_snapshot(ledger)
                sealed_tmp = ledger.seal()
                if sealed_tmp is None:
                    continue
                service._acked.add(sealed_tmp.identity)
                service._save_state()

        # With existence check bypassed, the entry WOULD be incorrectly pruned
        # (This proves the real test catches the crash-window regression)
        state_file = self.server_data / "delivery_state.json"
        state = json.loads(state_file.read_text())
        self.assertNotIn(
            sealed.identity, state["acked"],
            "Falsification: entry was pruned even though file still exists"
            " when existence check was bypassed (proving real test catches regression)"
        )

    def test_acked_entry_is_never_pruned_if_file_still_exists(self) -> None:
        """An _acked entry is never pruned while its backing file exists (crash-window protection).

        This is the regression test: pruning must never remove an entry whose file
        still exists, even at pruning boundaries. This protects against the crash
        window between "ACK recorded + saved" and "file actually deleted".
        """

        # Create delivery service
        service = DeliveryService(self.store, "ws", "run-test", TestTransport(), None)

        # Create a real segment file that we'll keep around
        ledger = SegmentLedger(self.store, "ws", "run-keep")
        add_snapshot(ledger)
        sealed = ledger.seal()
        assert sealed is not None

        # Verify file exists
        self.assertTrue(sealed.path.exists(), "Setup: segment file should exist")

        # Add this identity to _acked and save
        service._acked.add(sealed.identity)
        service._save_state()

        # Now add many entries to trigger pruning (happens automatically in _save_state)
        for i in range(210):
            ledger = SegmentLedger(self.store, "ws", f"run-filler-{i:03d}")
            add_snapshot(ledger)
            sealed_tmp = ledger.seal()
            if sealed_tmp is None:
                continue
            service._acked.add(sealed_tmp.identity)
            service._save_state()

        # File should still exist (we never deleted it)
        self.assertTrue(sealed.path.exists(), "Setup: file should still be there")

        # Verify the entry was NOT pruned despite pruning boundaries being crossed
        state_file = self.server_data / "delivery_state.json"
        state = json.loads(state_file.read_text())
        self.assertIn(
            sealed.identity, state["acked"],
            "Entry should never be pruned while file exists (crash-window protection)"
        )

    def test_report_identity_is_checked_across_all_workspace_directories_falsified(self) -> None:
        """Falsification: scope report check to one workspace and confirm test fails.

        This test demonstrates that without checking all workspaces, reports
        in one workspace could be pruned even though copies exist elsewhere.
        """

        import unittest.mock as mock

        service = DeliveryService(self.store, "ws-1", "run-test", TestTransport(), None)

        # Create report files in multiple workspaces
        report_id = "rpt-falsify-multi-ws"
        for ws in ["ws-1", "ws-2", "ws-3"]:
            report_dir = self.server_data / ws / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            report_file = report_dir / f"{report_id}.json"
            report_file.write_text(
                json.dumps({"report_id": report_id}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        service._acked.add(report_id)
        service._save_state()

        # Delete from ws-1 only
        (self.server_data / "ws-1" / "reports" / f"{report_id}.json").unlink()

        # Create a scoped version of _identity_file_exists that only checks self._workspace
        original_exists = service._identity_file_exists

        def scoped_check_only(identity: str) -> bool:
            """Buggy version that only checks the current workspace."""
            if ":" in identity:
                return original_exists(identity)  # segments still work
            # For reports, only check service._workspace (BUGGY!)
            server_data = service._store.server_data
            if server_data is None:
                return False
            return (server_data / service._workspace / "reports" / f"{identity}.json").exists()

        with mock.patch.object(service, '_identity_file_exists', side_effect=scoped_check_only):
            # Add entries to trigger pruning
            for i in range(210):
                ledger = SegmentLedger(self.store, "ws-1", f"run-falsify-{i:03d}")
                add_snapshot(ledger)
                sealed = ledger.seal()
                if sealed is None:
                    continue
                service._acked.add(sealed.identity)
                service._save_state()

        # With scoped check, the entry would be pruned (WRONG!)
        state_file = self.server_data / "delivery_state.json"
        state = json.loads(state_file.read_text())
        self.assertNotIn(
            report_id, state["acked"],
            "Falsification: report was pruned when checking only self._workspace"
            " (proving real test catches multi-workspace check)"
        )

    def test_report_identity_is_checked_across_all_workspace_directories(self) -> None:
        """A report identity is only pruned if absent from ALL workspace directories.

        Since report identities (rpt-*/sum-*) don't contain workspace info,
        pruning must check every workspace directory before pruning.
        """

        service = DeliveryService(self.store, "ws-1", "run-test", TestTransport(), None)

        # Create report files in multiple workspaces
        report_id = "rpt-multi-workspace"
        for ws in ["ws-1", "ws-2", "ws-3"]:
            report_dir = self.server_data / ws / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            report_file = report_dir / f"{report_id}.json"
            report_file.write_text(
                json.dumps({"report_id": report_id}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        # Add the report to _acked
        service._acked.add(report_id)
        service._save_state()

        state_file = self.server_data / "delivery_state.json"
        state = json.loads(state_file.read_text())
        self.assertIn(report_id, state["acked"], "Setup: report should be in _acked")

        # Delete the file from only ws-1
        (self.server_data / "ws-1" / "reports" / f"{report_id}.json").unlink()

        # Add entries to trigger pruning boundary (happens automatically in _save_state)
        for i in range(210):
            ledger = SegmentLedger(self.store, "ws-1", f"run-trigger-{i:03d}")
            add_snapshot(ledger)
            sealed = ledger.seal()
            if sealed is None:
                continue
            service._acked.add(sealed.identity)
            service._save_state()

        # Entry should NOT be pruned because it still exists in ws-2 and ws-3
        state = json.loads(state_file.read_text())
        self.assertIn(
            report_id, state["acked"],
            "Report entry should not be pruned while file exists in any workspace"
        )

        # Now delete from all workspaces
        for ws in ["ws-2", "ws-3"]:
            (self.server_data / ws / "reports" / f"{report_id}.json").unlink()

        # Add more entries to trigger another pruning boundary
        for i in range(210):
            ledger = SegmentLedger(self.store, "ws-1", f"run-final-{i:03d}")
            add_snapshot(ledger)
            sealed = ledger.seal()
            if sealed is None:
                continue
            service._acked.add(sealed.identity)
            service._save_state()

        # Now it should be pruned
        state = json.loads(state_file.read_text())
        self.assertNotIn(
            report_id, state["acked"],
            "Report entry should be pruned once absent from all workspaces"
        )


class AStuckSenderIsInvisible(MonitorTestCase):
    """A wedged sender must not back-pressure, stall, or crash tool handling.

    The failure this guards against is the one that looks like a server bug: an
    unreachable endpoint making every tool call wait on a socket. The server's
    obligation ends at the local append, so a hung send may cost delivery
    latency and nothing else.
    """

    def setUp(self) -> None:
        super().setUp()
        self.released = threading.Event()

        class WedgedTransport(TestTransport):
            name = "wedged"

            def send_files(inner, items):  # type: ignore[no-untyped-def]
                # Blocks until the test lets go, imitating a hung socket.
                self.released.wait(timeout=30)
                return super().send_files(items)

            def send_report(inner, report):  # type: ignore[no-untyped-def]
                self.released.wait(timeout=30)
                return super().send_report(report)

        self.transport = WedgedTransport()
        self.addCleanup(self.released.set)

    def test_tool_calls_keep_their_baseline_latency(self) -> None:
        monitor = self.make_monitor(
            transport=self.transport, usage_snapshot_every=2, checkin_every=99
        )
        monitor.bind_workspace(None)
        monitor.boot()  # queues a bootup drain that will wedge immediately

        started = time.monotonic()
        for index in range(12):  # crosses six snapshot boundaries
            observation = monitor.begin(f"tool{index}", {}, None)
            assert observation is not None
            observation.completed("ok")
        elapsed = time.monotonic() - started

        # Twelve calls against a sender stuck on a 30s wait. Anything near the
        # wait means a request waited on the send.
        self.assertLess(elapsed, 5.0, f"a wedged sender delayed tool calls: {elapsed:.2f}s")
        # The counter kept counting regardless of delivery health.
        self.assertEqual(monitor.health()["counters"]["total"], 12)

    def test_the_records_are_on_disk_even_though_nothing_was_delivered(self) -> None:
        """The file stays put for the next boot's recovery to ship."""

        monitor = self.make_monitor(
            transport=self.transport, usage_snapshot_every=2, checkin_every=99
        )
        monitor.bind_workspace(None)
        for index in range(4):
            observation = monitor.begin(f"tool{index}", {}, None)
            assert observation is not None
            observation.completed("ok")

        snapshots = [r for r in self.ledger_records() if r.get("kind") == "usage_snapshot"]
        self.assertEqual(len(snapshots), 2)
        # Nothing was ACKed, so nothing was deleted: un-ACKed files are the only
        # ones ever resident, and dropping a handoff never drops a record.
        self.assertTrue(self.ledger_files())
        health = monitor.health()
        self.assertNotEqual(health["delivery"]["state"], "sent")
        self.assertFalse(health["delivery"]["durable_off_box"])

    def test_health_and_shutdown_still_work(self) -> None:
        """Read-only introspection and exit must not wait on the sender either."""

        monitor = self.make_monitor(
            transport=self.transport, usage_snapshot_every=2, checkin_every=99
        )
        monitor.bind_workspace(None)
        observation = monitor.begin("get_state", {}, None)
        assert observation is not None
        observation.completed("ok")

        started = time.monotonic()
        self.assertIn("counters", monitor.health())
        monitor.closeout("wedged-sender-test")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0, f"shutdown waited on the sender: {elapsed:.2f}s")


class ClosingOut(MonitorTestCase):
    def test_closeout_is_bounded_even_if_the_transport_hangs(self) -> None:
        """Past the client's kill grace the process is killed regardless.

        Anything unfinished is carried by bootup recovery, which is why
        abandoning a slow send is safe.
        """

        import time

        store = resolve_store_root()
        ledger = SegmentLedger(store, "ws", "run-1")
        add_snapshot(ledger)
        sealed = ledger.seal()
        assert sealed is not None

        class HangingTransport(TestTransport):
            def send_files(self, items):  # type: ignore[no-untyped-def]
                time.sleep(30)
                return super().send_files(items)

        service = DeliveryService(store, "ws", "run-1", HangingTransport(), None)
        started = time.monotonic()
        service.drain_for_closeout([sealed], budget=0.3)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.0)

    def test_close_for_shutdown_spends_one_shared_deadline_not_two(self) -> None:
        """Regression test for C-3: drain-then-stop must share one deadline.

        ``close_for_shutdown`` exists because calling ``drain_for_closeout()``
        and ``stop()`` back to back, each defaulting to its own full
        ``CLOSEOUT_BUDGET_SECONDS``, let the two timeouts add -- roughly
        doubling the intended closeout budget and blowing past
        ``CLIENT_KILL_GRACE_SECONDS``. A wedged transport blocks both the
        drain worker and the daemon thread's in-flight send, so if the shared
        deadline ever regressed back to two independent waits, this would
        take about twice as long as the budget passed in.

        The budget used here is deliberately much smaller than the real
        ``CLOSEOUT_BUDGET_SECONDS``/``CLIENT_KILL_GRACE_SECONDS`` constants, so
        the pass/fail gap (roughly budget vs. roughly 2x budget) is wide
        relative to scheduler noise on a loaded machine, while still proving
        the property those constants depend on.
        """

        store = resolve_store_root()
        ledger = SegmentLedger(store, "ws", "run-1")
        add_snapshot(ledger)
        sealed = ledger.seal()
        assert sealed is not None

        class HangingTransport(TestTransport):
            def send_files(self, items):  # type: ignore[no-untyped-def]
                time.sleep(30)
                return super().send_files(items)

            def send_report(self, report):  # type: ignore[no-untyped-def]
                time.sleep(30)
                return super().send_report(report)

        service = DeliveryService(store, "ws", "run-1", HangingTransport(), None)
        service.start()
        # Occupy the daemon thread in a hanging send before measuring, so
        # stop()'s join has something real to wait on -- an idle thread would
        # exit almost immediately regardless of the timeout given, which would
        # not distinguish a shared deadline from two stacked ones.
        service.enqueue_report({"report_id": "rpt-keep-thread-busy"})
        for _ in range(100):
            if service._queue.empty():
                break
            time.sleep(0.01)

        budget = 0.2
        started = time.monotonic()
        service.close_for_shutdown([sealed], budget=budget)
        elapsed = time.monotonic() - started

        # Comfortably above the ~budget this should actually take, comfortably
        # below what two independent budgets stacked would take (~2x), and far
        # below CLIENT_KILL_GRACE_SECONDS regardless of the budget chosen here.
        self.assertLess(
            elapsed,
            budget * 1.75,
            f"close_for_shutdown took {elapsed:.2f}s against a {budget}s shared "
            "budget -- looks like drain and stop are spending independent "
            "budgets again",
        )

    def test_report_produces_a_real_sentry_envelope(self) -> None:
        """A dead SDK path would otherwise hide behind the JSON copy."""

        store = resolve_store_root()
        transport = SimulatedRemoteTransport(store.simulated_remote, "ws")
        transport.send_report(
            {
                "report_id": "rpt-envelope",
                "title": "boom",
                "severity": "error",
                "grouping_key": "abc",
                "signal_type": "S-1",
                "triage_class": "server_defect",
                "trail": [],
                "environment": {"narrative_logging": "enabled"},
            }
        )
        transport.close()
        reports = store.simulated_remote / "ws" / "reports"
        envelopes = list(reports.glob("envelope-*.txt"))
        self.assertTrue(envelopes, "the Sentry SDK produced no envelope")
        body = envelopes[0].read_text(encoding="utf-8", errors="replace")
        self.assertIn("abc", body)

    def test_local_json_copy_survives_an_sdk_failure(self) -> None:
        """The report must not depend on the SDK being healthy."""

        store = resolve_store_root()
        transport = SimulatedRemoteTransport(store.simulated_remote, "ws")
        transport._client = False  # simulate an unusable SDK
        result = transport.send_report({"report_id": "rpt-nosdk", "title": "x"})
        self.assertIs(result.state, DeliveryState.FILLER_SIMULATED)
        self.assertTrue(
            (store.simulated_remote / "ws" / "reports" / "rpt-nosdk.json").exists()
        )

    def test_no_transport_configured_still_reports_state(self) -> None:
        service = DeliveryService(resolve_store_root(), "ws", "run-1", NullTransport(), None)
        described = service.describe()
        self.assertEqual(described["state"], DeliveryState.NOT_CONFIGURED.value)
        self.assertFalse(described["durable_off_box"])
        self.assertIn("no off-box copy", described["off_box_note"])


if __name__ == "__main__":
    unittest.main()
