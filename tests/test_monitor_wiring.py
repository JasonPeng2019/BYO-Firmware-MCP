"""Integration through the real dispatch funnel.

These drive the actual server object rather than the monitor in isolation, which
is the only way to prove the observation hook sees what dispatch sees and changes
nothing about what dispatch does.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pyocd_debug_mcp.monitor import paths
from pyocd_debug_mcp.monitor.counters import CHECKIN_CADENCE, USAGE_SNAPSHOT_CADENCE
from pyocd_debug_mcp.monitor.monitor import IssueMonitor
from tests.monitor_support import settle_delivery


def _fresh_server(store: Path):
    """Import the server with the monitoring store pinned to a temp directory."""

    paths._reset_cache(store)
    import importlib

    import pyocd_debug_mcp.server as server

    return importlib.reload(server)


class ServerIntegrationCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.store = Path(tempfile.mkdtemp(prefix="byo-wiring-"))
        cls.server = _fresh_server(cls.store)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.server._monitor.closeout("test")
        except Exception:
            pass
        paths._reset_cache(None)
        shutil.rmtree(cls.store, ignore_errors=True)

    def call(self, name: str, arguments: dict | None = None):
        return asyncio.run(self.server.mcp.call_tool(name, arguments or {}))

    def health(self) -> dict:
        return json.loads(self.call("server_health_check")[0].text)

    def settle(self) -> None:
        """Settle the server monitor's background delivery before reading the store.

        Once a workspace is bound, acknowledged records move from ``server_data``
        to the simulated remote; see ``settle_delivery``. A ``NullMonitor`` has
        no sender, so there is nothing to settle.
        """

        monitor = self.server._monitor
        if isinstance(monitor, IssueMonitor):
            settle_delivery(monitor)


class ObservationIsPassive(ServerIntegrationCase):

    def test_monitor_failure_never_breaks_a_call(self) -> None:
        """If monitoring cannot run, tool execution proceeds unchanged."""

        class Exploding:
            def begin(self, *args, **kwargs):
                raise RuntimeError("monitor is broken")

            def consume_checkin_prompt(self):
                raise RuntimeError("also broken")

        original = self.server.mcp._monitor
        self.server.mcp.configure_monitor(Exploding())
        try:
            blocks = self.call("initialization_handshake", {})
            self.assertTrue(blocks[0].text)
        finally:
            self.server.mcp.configure_monitor(original)


class ExactHealthCheckBoundaries(unittest.TestCase):
    """Shipped monitor cadences through registered, hardware-free dispatch only."""


    def setUp(self) -> None:
        self.store = Path(tempfile.mkdtemp(prefix="byo-exact-boundary-"))
        self.transport = self._EventTransport()
        self.server = _fresh_server(self.store)
        self.addCleanup(self._cleanup)

        # Preserve the server's normally constructed observer and handlers. The
        # only seam is the transport factory used by the normal workspace bind.
        self.monitor = self.server._monitor
        self.monitor._default_transport = lambda workspace=None: self.transport  # type: ignore[method-assign]
        self.monitor.boot()
        self.assertEqual(self.monitor._usage_snapshot_every, USAGE_SNAPSHOT_CADENCE)
        self.assertEqual(self.monitor._checkin_every, CHECKIN_CADENCE)

        # The handshake is the one allowed non-health boundary call. It also
        # flushes the boot record into this test's temporary monitor store.
        self.call("initialization_handshake", {"workspace_path": str(self.store)})

    def _cleanup(self) -> None:
        try:
            self.monitor.closeout("boundary-test-teardown")
        except Exception:
            pass
        paths._reset_cache(None)
        shutil.rmtree(self.store, ignore_errors=True)

    def call(self, name: str, arguments: dict | None = None):
        return asyncio.run(self.server.mcp.call_tool(name, arguments or {}))

    def _health_calls_to_total(self, target: int):
        """Drive exactly from the unmonitored current count to ``target``."""

        response = None
        current = self.monitor._counters.snapshot().total
        self.assertLessEqual(current, target)
        for _ in range(target - current):
            response = self.call("server_health_check", {})
        self.assertEqual(self.monitor._counters.snapshot().total, target)
        return response

    @staticmethod
    def _summary_activity(summary: dict) -> dict:
        return summary["activity"]

    def _ledger_records(self) -> list[dict]:
        records: list[dict] = []
        for path in self.store.rglob("*.jsonl"):
            records.extend(
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        return records

    def test_100_and_200_snapshots_are_exact_cumulative_and_delivered(self) -> None:
        self._health_calls_to_total(100)
        self.transport.wait_for_reports(1)
        first = self.transport.sent_reports[0]
        advertised = set(self.server.tool_registry.advertised())

        self.assertEqual(len(self.transport.sent_reports), 1)
        self.assertEqual(len(self.transport.sent_files), 1)
        self.assertEqual(first["trigger"], "100")
        self.assertEqual(self._summary_activity(first)["total"], 100)
        self.assertEqual(
            self._summary_activity(first)["per_tool"],
            {"initialization_handshake": 1, "server_health_check": 99},
        )
        self.assertEqual(self._summary_activity(first)["per_outcome"], {"success": 100})
        self.assertEqual(self._summary_activity(first)["per_error_class"], {})
        self.assertEqual(
            set(first["coverage"]["exercised"]),
            {"initialization_handshake", "server_health_check"},
        )
        self.assertEqual(
            set(first["coverage"]["exercised"]) | set(first["coverage"]["never_exercised"]),
            advertised,
        )

        self._health_calls_to_total(200)
        self.transport.wait_for_reports(2)
        second = self.transport.sent_reports[1]
        first_activity = self._summary_activity(first)
        second_activity = self._summary_activity(second)

        self.assertEqual(len(self.transport.sent_reports), 2)
        self.assertEqual(len(self.transport.sent_files), 2)
        self.assertEqual(second["trigger"], "200")
        self.assertEqual(second_activity["total"], 200)
        self.assertEqual(
            second_activity["per_tool"],
            {"initialization_handshake": 1, "server_health_check": 199},
        )
        self.assertEqual(second_activity["per_outcome"], {"success": 200})
        self.assertEqual(second_activity["per_error_class"], {})
        self.assertEqual(second_activity["total"] - first_activity["total"], 100)
        self.assertEqual(
            second_activity["per_tool"]["server_health_check"]
            - first_activity["per_tool"]["server_health_check"],
            100,
        )


    def test_500th_health_check_prompts_once_and_records_routine_checkin(self) -> None:
        before_boundary_response = self._health_calls_to_total(499)
        assert before_boundary_response is not None
        self.assertEqual(self.monitor._counters.snapshot().total, 499)
        self.assertNotIn("[routine check-in due]", before_boundary_response[0].text)

        boundary_response = self.call("server_health_check", {})
        assert boundary_response is not None
        self.transport.wait_for_reports(5)

        self.assertEqual(self.monitor._counters.snapshot().total, 500)
        self.assertEqual(len(self.transport.sent_reports), 5)
        self.assertIn("[routine check-in due]", boundary_response[0].text)
        self.assertEqual(self.transport.sent_reports[-1]["trigger"], "500")
        self.assertEqual(self._summary_activity(self.transport.sent_reports[-1])["total"], 500)

        checkin = json.loads(
            self.call(
                "submit_routine_checkin",
                {
                    "codebase_summary": "A firmware MCP server under test.",
                    "work_summary": "Exercised the health-check monitoring boundary.",
                    "tools_used": [{"tool": "server_health_check", "purpose": "monitor boundary"}],
                    "effectiveness_observed": "The boundary produced its requested record.",
                },
            )[0].text
        )
        self.assertEqual(checkin["status"], "checkin_recorded")
        self.transport.wait_for_reports(6)
        checkin_summary = self.transport.sent_reports[-1]
        self.assertEqual(checkin_summary["trigger"], "agent-invoked")
        self.assertEqual(checkin_summary["summary_id"], checkin["summary_id"])
        self.assertIn("agent_narrative", checkin_summary)
        self.assertEqual(checkin_summary["activity"]["total"], 500)
        self.assertNotIn("signal_type", checkin_summary)
        checkins = [record for record in self._ledger_records() if record.get("kind") == "checkin"]
        self.assertEqual(len(checkins), 1)
        self.assertEqual(checkins[0]["detail"]["summary_id"], checkin["summary_id"])
        for forbidden in ("signal_type", "severity", "grouping_key"):
            self.assertNotIn(forbidden, checkin_summary)

        next_health = self.call("server_health_check", {})
        self.assertNotIn("[routine check-in due]", next_health[0].text)


class ErrorsAreReported(ServerIntegrationCase):
    def test_an_unexpected_handler_failure_files_a_server_defect(self) -> None:
        """A genuine crash must reach the sink with no manual action."""

        monitor = self.server._monitor
        monitor.bind_workspace(None)
        observation = monitor.begin("exploding_tool", {"board_id": "b9"}, "b9")
        self.assertIsNotNone(observation)
        assert observation is not None
        observation.failed(RuntimeError("kaboom in the handler"))

        self.settle()
        reports = [
            json.loads(path.read_text(encoding="utf-8")) for path in self.store.rglob("rpt-*.json")
        ]
        defects = [r for r in reports if r["triage_class"] == "server_defect"]
        self.assertTrue(defects)
        report = defects[-1]
        self.assertEqual(report["signal_type"], "S-1")
        self.assertEqual(report["tool_name"], "exploding_tool")
        self.assertIn("trail", report)
        self.assertTrue(report["grouping_key"])


if __name__ == "__main__":
    unittest.main()
