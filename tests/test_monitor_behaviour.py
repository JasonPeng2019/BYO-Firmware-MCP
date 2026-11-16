"""Monitor-level behaviour: periodic ticks, reporting, and graceful degradation.

These drive the facade rather than its parts, so they cover the wiring between
detection and reporting that unit tests of each component cannot reach.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pyocd_debug_mcp.monitor.block import Anchor, BlockState
from pyocd_debug_mcp.monitor.monitor import NullMonitor
from pyocd_debug_mcp.monitor.paths import StoreRoot, StoreState
from pyocd_debug_mcp.monitor.transport import NullTransport, TestTransport
from tests.monitor_support import MonitorTestCase

import pytest


class ReportingThroughTheMonitor(MonitorTestCase):
    def setUp(self) -> None:
        super().setUp()
        # These feature assertions inspect retained report bodies. Pin the
        # bind-time factory too, because workspace binding replaces the transport.
        transport = NullTransport()
        self.monitor = self.make_monitor(transport=transport)
        self.monitor._default_transport = lambda workspace=None: transport
        self.monitor.bind_workspace(None)

    def observe_failure(self, tool: str = "read_memory", exc: BaseException | None = None):
        observation = self.monitor.begin(tool, {"board_id": "b1"}, "b1")
        assert observation is not None
        observation.failed(exc or RuntimeError("kaboom"))

    @pytest.mark.mvp
    def test_runtime_error_files_one_server_defect(self) -> None:
        self.observe_failure()
        reports = [self.read_report(p) for p in self.report_files()]
        defects = [r for r in reports if r["triage_class"] == "server_defect"]
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["signal_type"], "S-1")
        self.assertEqual(defects[0]["origin"], "server-auto")


    def read_report(self, path: Path) -> dict:
        import json

        return json.loads(path.read_text(encoding="utf-8"))


class GracefulDegradation(MonitorTestCase):
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


class NullMonitorSurface(unittest.TestCase):
    """Monitoring failing closed would be worse than monitoring being absent."""

    def setUp(self) -> None:
        self.monitor = NullMonitor("store unavailable")


    def test_health_declares_itself_unavailable_not_healthy(self) -> None:
        health = self.monitor.health()
        self.assertEqual(health["monitoring"], "unavailable")
        self.assertIn("store unavailable", health["reason"])


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
            transport=TestTransport(),
            usage_snapshot_every=3,
            checkin_every=99,
        )
        monitor.bind_workspace(None)
        monitor.boot()

        # Make 2 calls
        for _ in range(2):
            observation = monitor.begin("tool_a", {}, None)
            assert observation is not None
            observation.completed("ok")

        count_before_bind = monitor._counters.snapshot().total
        self.assertEqual(count_before_bind, 2)

        # Bind workspace (rebuilds ledger, delivery, etc.)
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            monitor.bind_workspace(tmpdir)

        # Make 1 more call (total should be 3, cadence should trigger at 3)
        observation = monitor.begin("tool_b", {}, None)
        assert observation is not None
        observation.completed("ok")

        count_after_bind = monitor._counters.snapshot().total
        self.assertEqual(count_after_bind, 3, "Counter reset after bind_workspace()")
        # Verify cadence is still measured from run start (call 3 triggers snapshot)
        self.settle()
        snapshots = [r for r in self.ledger_records() if r.get("kind") == "usage_snapshot"]
        self.assertTrue(
            snapshots,
            "Cadence did not fire at call 3 (would have reset if binding restarted counter)",
        )


if __name__ == "__main__":
    unittest.main()
