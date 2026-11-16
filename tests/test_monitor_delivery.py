"""Delivery, the transport seam, and the self-draining spool.

The filler must never masquerade as a working transport: a stub that reports
success would leave months of sessions believed to be archived remotely while
nothing ever left the machine.
"""

from __future__ import annotations

import json
import time
import unittest

from pyocd_debug_mcp.monitor.delivery import DeliveryService
from pyocd_debug_mcp.monitor.ledger import SegmentLedger
from pyocd_debug_mcp.monitor.paths import resolve_store_root
from pyocd_debug_mcp.monitor.transport import DeliveryState, SimulatedRemoteTransport, TestTransport
from tests.monitor_support import MonitorTestCase
from tests.monitor_support import add_snapshot


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


class ReportDeleteOnACK(MonitorTestCase):
    """Report/summary files are deleted from disk once ACKed, mirroring segment behavior."""

    def setUp(self) -> None:
        super().setUp()
        self.store = resolve_store_root()

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


class ClosingOut(MonitorTestCase):
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

        The pass/fail gap is roughly budget vs. roughly 2x budget, while thread
        join/scheduling overhead is a roughly fixed amount on top of the budget
        (about 0.15s observed on a loaded macOS runner). A one-second budget
        keeps that fixed overhead small relative to the gap, so the test
        distinguishes one shared deadline from two stacked ones instead of
        measuring scheduler noise.

        That tolerance also means this real-thread test only catches the passed
        budget being spent twice. A step falling back to its own default (the
        default or a mixed composition) adds at most another 0.4s, inside the
        tolerance; the virtual-clock test below catches those deterministically.
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

        budget = 1.0
        started = time.monotonic()
        service.close_for_shutdown([sealed], budget=budget)
        elapsed = time.monotonic() - started

        # Comfortably above the ~budget plus fixed overhead this should take,
        # and comfortably below what two independent budgets stacked would
        # take (~2x).
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


if __name__ == "__main__":
    unittest.main()
