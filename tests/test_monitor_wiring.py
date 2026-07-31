"""Integration through the real dispatch funnel.

These drive the actual server object rather than the monitor in isolation, which
is the only way to prove the observation hook sees what dispatch sees and changes
nothing about what dispatch does.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import inspect
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from pyocd_debug_mcp.monitor import paths
from pyocd_debug_mcp.monitor.counters import CHECKIN_CADENCE, USAGE_SNAPSHOT_CADENCE
from pyocd_debug_mcp.monitor.transport import TestTransport


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


class CorrectRefusalsProduceNoDefects(ServerIntegrationCase):
    """The primary gate: nothing else matters if this fails.

    A full session of correct guarded behaviour -- locked handlers, an all-NULL
    plan guide, a closed gate, a missing board -- must produce zero server-defect
    reports. This server refuses by design, so a classifier that reads refusals
    as defects would flood the sink on the first real session.
    """

    def test_guarded_session_files_no_server_defect(self) -> None:
        # A locked hardware action, called directly.
        with self.assertRaises(Exception):
            self.call("flash_application", {"board_id": "b1", "artifact_path": "x"})
        # An all-NULL plan guide: the documented first step, not an error.
        self.call("flash_application-plan", {"board_id": None})
        # A tool needing a board that was never connected.
        with self.assertRaises(Exception):
            self.call("get_state", {"board_id": "ghost"})
        # An unlisted tool name.
        with self.assertRaises(Exception):
            self.call("not_a_real_tool", {})
        # A guarded tool with no board id at all.
        with self.assertRaises(Exception):
            self.call("flash_application", {})

        reports = list(self.store.rglob("rpt-*.json"))
        defects = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in reports
            if json.loads(path.read_text(encoding="utf-8"))["triage_class"]
            == "server_defect"
        ]
        self.assertEqual(defects, [], f"unexpected server-defect reports: {defects}")

    def test_refusals_are_still_counted_and_recorded(self) -> None:
        before = self.health()["counters"]["total"]
        with self.assertRaises(Exception):
            self.call("get_state", {"board_id": "ghost-2"})
        after = self.health()
        self.assertGreater(after["counters"]["total"], before)
        self.assertIn("policy_refusal", after["counters"]["per_outcome"])


class ObservationIsPassive(ServerIntegrationCase):
    def test_refusal_text_is_unchanged(self) -> None:
        """Monitoring must never soften or alter what dispatch propagates."""

        with self.assertRaises(Exception) as caught:
            self.call("get_state", {"board_id": "ghost-3"})
        message = str(caught.exception)
        self.assertIn("not connected", message)
        self.assertIn("Safe exit:", message)

    def test_successful_result_is_unchanged(self) -> None:
        blocks = self.call("initialization_handshake", {})
        self.assertIn("Guarded Hardware Server operating guidance", blocks[0].text)

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


class DispatchFunnelCoverage(ServerIntegrationCase):
    def test_every_call_is_observed(self) -> None:
        before = self.health()["counters"]["total"]
        self.call("initialization_handshake", {})
        self.call("initialization_handshake", {})
        after = self.health()["counters"]["total"]
        # Two handshakes plus the health check that read `before`.
        self.assertGreaterEqual(after - before, 3)

    def test_per_tool_counts_name_real_tools(self) -> None:
        self.call("initialization_handshake", {})
        counts = self.health()["counters"]["per_tool"]
        self.assertIn("initialization_handshake", counts)

    def test_coverage_lists_unexercised_tools(self) -> None:
        health = self.health()
        self.assertIn("never_exercised", health["coverage"])
        self.assertGreater(len(health["coverage"]["never_exercised"]), 0)


class HealthCheckAsTestOracle(ServerIntegrationCase):
    """Assertions about server activity must be expressible through the protocol."""

    def test_oracle_reports_the_expected_shape(self) -> None:
        health = self.health()
        for key in (
            "run_id",
            "uptime_seconds",
            "narrative_logging",
            "counters",
            "coverage",
            "ledger",
            "recording",
            "delivery",
            "block",
        ):
            self.assertIn(key, health)

    def test_transport_state_never_claims_a_real_send(self) -> None:
        delivery = self.health()["delivery"]
        self.assertNotEqual(delivery["state"], "sent")
        self.assertFalse(delivery["durable_off_box"])

    def test_recording_delta_is_visible(self) -> None:
        recording = self.health()["recording"]
        self.assertIn("counter_minus_appended", recording)
        self.assertIn("last_write_error", recording)


class MonitorToolsAreOutsideTheSafetySurface(ServerIntegrationCase):
    def test_all_three_are_advertised(self) -> None:
        advertised = set(self.server.tool_registry.advertised())
        self.assertIn("report_agent_issue", advertised)
        self.assertIn("server_health_check", advertised)
        self.assertIn("submit_routine_checkin", advertised)

    def test_none_are_guarded_or_layer_two(self) -> None:
        for name in ("report_agent_issue", "server_health_check", "submit_routine_checkin"):
            with self.subTest(tool=name):
                definition = self.server.tool_registry.definition(name)
                self.assertFalse(definition.hidden_by_default)
                self.assertFalse(definition.locked_by_default)
                self.assertNotIn(name, self.server.mcp._layer2_tools)
                self.assertNotIn(name, self.server.mcp._guarded_dispatch)

    def test_they_are_refused_as_batch_children(self) -> None:
        from pyocd_debug_mcp.tools.batch import BatchChild, BatchValidationError, _validate_children

        with self.assertRaises(BatchValidationError) as caught:
            _validate_children(
                "b1",
                [
                    BatchChild(
                        tool_name="server_health_check",
                        arguments={"board_id": "b1"},
                    )
                ],
                tool_exists=lambda name: True,
            )
        self.assertIn("not batchable", str(caught.exception))

    def test_registering_them_did_not_churn_discovery(self) -> None:
        """Their visibility must not cause spurious list-changed notifications."""

        revision = self.server.tool_registry.list_revision
        self.call("server_health_check")
        self.assertEqual(self.server.tool_registry.list_revision, revision)


class ExactHealthCheckBoundaries(unittest.TestCase):
    """Shipped monitor cadences through registered, hardware-free dispatch only."""

    class _EventTransport(TestTransport):
        """A deterministic delivery witness; it never talks to a real transport."""

        def __init__(self) -> None:
            super().__init__()
            self._reports_ready = threading.Condition()

        def send_report(self, report):  # type: ignore[no-untyped-def]
            result = super().send_report(report)
            with self._reports_ready:
                self._reports_ready.notify_all()
            return result

        def wait_for_reports(self, count: int) -> None:
            with self._reports_ready:
                delivered = self._reports_ready.wait_for(
                    lambda: len(self.sent_reports) >= count, timeout=5
                )
            if not delivered:
                raise AssertionError(
                    f"timed out waiting for {count} delivered summaries; "
                    f"got {len(self.sent_reports)}"
                )

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

    def test_delivered_snapshot_contains_required_complete_summary(self) -> None:
        self._health_calls_to_total(100)
        self.transport.wait_for_reports(1)
        summary = self.transport.sent_reports[0]
        activity = summary["activity"]
        coverage = summary["coverage"]
        ledger = summary["ledger"]
        delivery = summary["delivery"]

        self.assertEqual(summary["record_type"], "summary")
        self.assertTrue(summary["summary_id"])
        self.assertEqual(summary["session_id"], self.server._monitor_context.run_id)
        self.assertEqual(
            summary["run_started_at"],
            self.server._monitor_context.run_started_at.isoformat().replace("+00:00", "Z"),
        )
        datetime.fromisoformat(summary["run_started_at"].replace("Z", "+00:00"))
        self.assertGreaterEqual(summary["uptime_seconds"], 0)
        self.assertEqual(activity["total"], 100)
        self.assertEqual(activity["per_tool"], {"initialization_handshake": 1, "server_health_check": 99})
        self.assertEqual(activity["per_outcome"], {"success": 100})
        self.assertEqual(activity["per_error_class"], {})
        self.assertEqual(sum(activity["per_tool"].values()), activity["total"])
        self.assertEqual(sum(activity["per_outcome"].values()), activity["total"])
        self.assertTrue(activity["first_at"])
        self.assertTrue(activity["last_at"])
        exercised = set(coverage["exercised"])
        never_exercised = set(coverage["never_exercised"])
        self.assertFalse(exercised & never_exercised)
        self.assertEqual(exercised | never_exercised, set(self.server.tool_registry.advertised()))
        self.assertGreaterEqual(ledger["total_appended"], 0)
        self.assertGreaterEqual(ledger["resident_files"], 0)
        self.assertTrue(ledger["chain_head"])
        self.assertIn(
            ledger["hardening"], {"applied", "unsupported", "failed", "not_attempted"}
        )
        self.assertIsInstance(ledger["verification"], dict)
        self.assertIsNone(ledger["last_write_error"])
        self.assertEqual(delivery["transport"], "test")
        self.assertEqual(delivery["origin"], "test")
        self.assertIn(
            delivery["state"], {"sent", "failed", "not_configured", "filler_simulated"}
        )
        self.assertIsInstance(delivery["acked_files"], int)
        self.assertGreaterEqual(delivery["undelivered_files"], 0)
        self.assertTrue(delivery["store_state"])
        self.assertTrue(delivery["workspace_bound"])
        self.assertEqual(
            set(delivery["block"]),
            {
                "state",
                "threshold_days",
                "anchor_at",
                "anchor_origin",
                "anchor_transport",
                "stale_days",
            },
        )
        self.assertTrue(summary["environment"]["server_version"])
        self.assertEqual(
            summary["environment"]["server_version"], self.server._monitor_context.server_version
        )
        self.assertTrue(summary["environment"]["narrative_logging"])

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
                    "tools_used": [
                        {"tool": "server_health_check", "purpose": "monitor boundary"}
                    ],
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
        checkins = [
            record
            for record in self._ledger_records()
            if record.get("kind") == "checkin"
        ]
        self.assertEqual(len(checkins), 1)
        self.assertEqual(checkins[0]["detail"]["summary_id"], checkin["summary_id"])
        for forbidden in ("signal_type", "severity", "grouping_key"):
            self.assertNotIn(forbidden, checkin_summary)

        next_health = self.call("server_health_check", {})
        self.assertNotIn("[routine check-in due]", next_health[0].text)


class WorkspaceBinding(ServerIntegrationCase):
    def test_handshake_accepts_and_validates_a_workspace_path(self) -> None:
        signature = inspect.signature(self.server.initialization_handshake)
        self.assertIn("workspace_path", signature.parameters)

    def test_invalid_path_is_ignored_not_refused(self) -> None:
        blocks = self.call(
            "initialization_handshake", {"workspace_path": "not/absolute"}
        )
        text = blocks[0].text
        self.assertIn("operating guidance", text)
        self.assertIn("unbound", text)

    def test_nothing_is_written_inside_the_workspace(self) -> None:
        project = Path(tempfile.mkdtemp(prefix="byo-project-"))
        self.addCleanup(shutil.rmtree, project, True)
        self.call("initialization_handshake", {"workspace_path": str(project)})
        self.call("server_health_check")
        stray = [p for p in project.rglob("*") if p.is_file()]
        self.assertEqual(stray, [], f"monitor wrote into the project dir: {stray}")

    def test_store_carries_no_plaintext_project_path(self) -> None:
        blob = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in self.store.rglob("*")
            if path.is_file()
        )
        self.assertNotIn("byo-project-", blob)
        names = " ".join(p.name for p in self.store.rglob("*"))
        self.assertNotIn("byo-project-", names)


class ErrorsAreReported(ServerIntegrationCase):
    def test_an_unexpected_handler_failure_files_a_server_defect(self) -> None:
        """A genuine crash must reach the sink with no manual action."""

        monitor = self.server._monitor
        monitor.bind_workspace(None)
        observation = monitor.begin("exploding_tool", {"board_id": "b9"}, "b9")
        self.assertIsNotNone(observation)
        assert observation is not None
        observation.failed(RuntimeError("kaboom in the handler"))

        reports = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in self.store.rglob("rpt-*.json")
        ]
        defects = [r for r in reports if r["triage_class"] == "server_defect"]
        self.assertTrue(defects)
        report = defects[-1]
        self.assertEqual(report["signal_type"], "S-1")
        self.assertEqual(report["tool_name"], "exploding_tool")
        self.assertIn("trail", report)
        self.assertTrue(report["grouping_key"])

    def test_report_trail_is_board_scoped(self) -> None:
        monitor = self.server._monitor
        monitor.bind_workspace(None)
        alpha = monitor.begin("t_alpha", {"board_id": "alpha"}, "alpha")
        assert alpha is not None
        alpha.completed("ok")
        beta = monitor.begin("t_beta", {"board_id": "beta"}, "beta")
        assert beta is not None
        beta.failed(RuntimeError("beta blew up"))

        reports = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in self.store.rglob("rpt-*.json")
        ]
        beta_reports = [r for r in reports if r.get("tool_name") == "t_beta"]
        self.assertTrue(beta_reports)
        boards = {entry["board"] for entry in beta_reports[-1]["trail"]}
        self.assertEqual(boards, {"beta"})


if __name__ == "__main__":
    unittest.main()
