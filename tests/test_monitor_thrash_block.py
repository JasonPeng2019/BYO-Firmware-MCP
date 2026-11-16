"""Repetition detection and the remote-logging staleness backstop.

The detector's risk is the mirror of its purpose: this server has many correct
repetition patterns, so a naive detector becomes a false-positive engine.
Repetition alone is not thrashing -- repetition with an identical outcome and no
state transition is.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from pyocd_debug_mcp.monitor.block import Anchor
from pyocd_debug_mcp.monitor.thrash import ThrashDetector
from pyocd_debug_mcp.monitor.transport import TestTransport
from pyocd_debug_mcp.services.session_runtime import PolicyRefusal
from tests.monitor_support import MonitorTestCase


class Thrashing(unittest.TestCase):
    def setUp(self) -> None:
        self.detector = ThrashDetector()

    def observe(self, **kwargs):
        base = dict(
            board="b1",
            tool="read_memory",
            args_fp="fp-1",
            outcome="policy_refusal",
            error_class="plan/gate-closed",
            guard_fp="guard-1",
        )
        base.update(kwargs)
        return self.detector.observe(**base)

    def test_a_genuine_loop_fires_once(self) -> None:
        fired = [self.observe() for _ in range(8)]
        self.assertEqual(fired.count(True), 1)

    def test_changing_outcome_is_progress_not_a_loop(self) -> None:
        self.observe()
        self.observe()
        self.observe()
        self.assertFalse(self.observe(outcome="success", error_class=None))

    def test_polling_loops_never_fire(self) -> None:
        for tool in ("get_state", "read_execution_state", "get_setup_status", "wait"):
            with self.subTest(tool=tool):
                detector = ThrashDetector()
                results = [
                    detector.observe(
                        board="b",
                        tool=tool,
                        args_fp="fp",
                        outcome="success",
                        error_class=None,
                        guard_fp="g",
                    )
                    for _ in range(12)
                ]
                self.assertFalse(any(results))


class BlockDiagnostics(MonitorTestCase):
    """The age policy is unchanged, even when no delivery was attempted."""

    def setUp(self) -> None:
        super().setUp()
        self.now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.clock = self.enterContext(
            patch("pyocd_debug_mcp.monitor.block.datetime", wraps=datetime)
        )
        self.clock.now.return_value = self.now
        transport_clock = self.enterContext(
            patch("pyocd_debug_mcp.monitor.transport.datetime", wraps=datetime)
        )
        transport_clock.now.return_value = self.now

    def _deliver_sealed_segment(self, transport):
        monitor = self.make_monitor(transport=transport)
        anchor = Anchor(self.now - timedelta(days=30), "simulated_remote", "filler")
        monitor._block.inject(anchor)
        from tests.monitor_support import add_snapshot

        add_snapshot(monitor._ledger)
        sealed = monitor._ledger.seal()
        assert sealed is not None
        monitor._delivery.drain_for_closeout([sealed], budget=2.0)
        return monitor, anchor, sealed

    def test_actual_failed_delivery_leaves_the_old_anchor_and_gate(self) -> None:
        monitor, anchor, sealed = self._deliver_sealed_segment(TestTransport(fail_always=True))
        self.assertTrue(sealed.path.exists())
        self.assertEqual(monitor._delivery.describe()["state"], "failed")
        self.assertIs(monitor._block.anchor, anchor)
        with self.assertRaises(PolicyRefusal) as caught:
            monitor.check_block()
        self.assertIn("transport: simulated_remote", str(caught.exception))
        self.assertIn("server_health_check", str(caught.exception))
        self.assertNotIn("Restore network", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
