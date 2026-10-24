"""VI11: inspect whole retained, persisted and locally delivered report records."""

from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch

from pyocd_debug_mcp.monitor.redaction import digest_id
from pyocd_debug_mcp.monitor.transport import NullTransport
from tests.monitor_support import FakeGate, MonitorTestCase, make_context


PROBE = "VI11_PROBE_SERIAL_SENTINEL"
HOST = "VI11_HOST_SENTINEL"
USER = "VI11_USER_SENTINEL"
PROJECT = "VI11_PROJECT_SENTINEL"
CONNECTION = f"probe:{PROBE}@{HOST}/{USER}/{PROJECT}"
RECONNECTED = f"{CONNECTION}:reconnected"
UNC = f"\\\\{HOST}\\share\\{USER}\\{PROJECT}\\private.elf"
CONSOLE = "VI11_CONSOLE_PAYLOAD_SENTINEL"
MEMORY = "VI11_MEMORY_PAYLOAD_SENTINEL"
WRITE = "VI11_WRITE_PAYLOAD_SENTINEL"
SENTINELS = (PROBE, HOST, USER, PROJECT, CONNECTION, RECONNECTED, CONSOLE, MEMORY, WRITE)


class CompleteTrailRecords(MonitorTestCase):
    def export(self, label, data):
        """Optional full-record evidence for the campaign, outside product behavior."""
        destination = os.environ.get("VI11_RECORD_EVIDENCE")
        if destination:
            directory = Path(destination) / self._testMethodName
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{label}.json").write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

    @staticmethod
    def arguments():
        return {"path": UNC, "data": MEMORY.encode(), "text": WRITE, "host": HOST}

    def context(self, connections, run_id="run-identity"):
        return replace(
            make_context(run_id=run_id, gate=FakeGate(PROBE)),
            connection_id=lambda board: connections.get(board),
        )

    def retained_monitor(self, connections, run_id="run-identity"):
        transport = NullTransport()
        monitor = self.make_monitor(context=self.context(connections, run_id), transport=transport)
        # Workspace binding normally replaces the constructor transport.
        monitor._default_transport = lambda workspace=None: transport
        monitor.bind_workspace(self.store_dir / PROJECT)
        return monitor

    def observe(self, monitor, board, tool="read_memory", error=None):
        call = monitor.begin(tool, self.arguments(), board)
        assert call is not None
        if error is None:
            call.completed(CONSOLE)
        else:
            call.failed(error)

    @staticmethod
    def report_paths(monitor):
        directory = monitor._store.server_data / monitor._workspace / "reports"
        return sorted(directory.glob("rpt-*.json"))

    def assert_private(self, records):
        text = json.dumps(records, sort_keys=True)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)

    def store_records(self):
        return {
            str(path.relative_to(self.store_dir)): path.read_text(
                encoding="utf-8", errors="replace"
            )
            for path in self.store_dir.rglob("*")
            if path.is_file() and path.suffix in (".json", ".jsonl", ".txt")
        }

    def test_complete_retained_persisted_and_delivered_records_exclude_sentinels(self):
        monitor = self.retained_monitor({"alpha": CONNECTION})
        for tool in ("read_serial", "read_memory", "write_memory"):
            self.observe(monitor, "alpha", tool)
        for _ in range(50):
            self.observe(monitor, "alpha", error=RuntimeError(f"failed at {UNC} for {USER}"))

        paths = self.report_paths(monitor)
        self.assertEqual(len(paths), 1, "fifty equal failures must emit one report")
        report = json.loads(paths[0].read_text(encoding="utf-8"))
        self.assertEqual(paths[0].stem, report["report_id"])
        retained = [asdict(entry) for entry in monitor._trail.for_board("alpha")]
        before = {"retained_trail": retained, "report": report, "store": self.store_records()}
        self.export("retained-persisted", before)
        self.assert_private(before)
        self.assertTrue(retained)
        self.assertEqual({entry["connection"] for entry in retained}, {digest_id(CONNECTION)})
        self.assertEqual(report["board_scope"]["connection_id"], digest_id(CONNECTION))
        self.assertEqual(report["board_scope"]["probe_uid"], digest_id(PROBE))
        self.assertEqual(monitor._counters.snapshot().total, 53)

        # Exercise real boot recovery of the persisted ID through local simulated
        # delivery, including ACK/unlink and the actual Sentry envelope writer.
        monitor.closeout("retained-phase")
        delivered_monitor = self.make_monitor(
            context=self.context({"alpha": CONNECTION}, "run-recovery")
        )
        delivered_monitor.bind_workspace(self.store_dir / PROJECT)
        delivered = self.simulated_remote / monitor._workspace / "reports" / paths[0].name
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (paths[0].exists() or not delivered.exists()):
            time.sleep(0.01)
        self.assertFalse(paths[0].exists(), "ACK must remove the original spool body")
        self.assertTrue(
            delivered.is_file(), "the same report ID must exist in the local delivery sink"
        )
        delivered_monitor._delivery.stop(timeout=2)
        self.assertEqual(json.loads(delivered.read_text(encoding="utf-8")), report)
        actual = self.store_records()
        self.export("delivered-records", actual)
        self.assert_private(actual)
        self.assertEqual(delivered_monitor._delivery.describe()["state"], "filler_simulated")
        self.assertFalse(delivered_monitor._delivery.describe()["durable_off_box"])
        report_ids = [
            json.loads(path.read_text(encoding="utf-8"))["report_id"]
            for path in self.simulated_remote.rglob("rpt-*.json")
        ]
        self.assertEqual(report_ids, [report["report_id"]])

    def test_board_scoping_reconnect_and_missing_connection_in_complete_reports(self):
        connections = {"alpha": CONNECTION, "beta": f"{CONNECTION}:other-board"}
        monitor = self.retained_monitor(connections)
        self.observe(monitor, "alpha", "read_serial")
        self.observe(monitor, "beta", "write_memory")
        self.observe(monitor, "alpha", "read_memory")
        connections["alpha"] = RECONNECTED
        self.observe(monitor, "alpha", "read_memory")
        connections["alpha"] = None
        self.observe(monitor, "alpha", error=RuntimeError(f"alpha failure {UNC}"))
        self.observe(monitor, "beta", error=ValueError(f"beta failure {UNC}"))
        reports = [
            json.loads(path.read_text(encoding="utf-8")) for path in self.report_paths(monitor)
        ]
        self.assertEqual(len(reports), 2)
        self.export("complete-board-records", {"reports": reports, "store": self.store_records()})
        self.assert_private(reports)
        by_board = {report["board_scope"]["board_id"]: report for report in reports}
        alpha, beta = by_board["alpha"], by_board["beta"]
        self.assertTrue(all(entry["board"] == "alpha" for entry in alpha["trail"]))
        self.assertTrue(all(entry["board"] == "beta" for entry in beta["trail"]))
        tokens = [entry["connection"] for entry in alpha["trail"]]
        self.assertEqual(
            tokens, [digest_id(CONNECTION), digest_id(CONNECTION), digest_id(RECONNECTED), None]
        )
        self.assertNotEqual(tokens[0], tokens[2])
        self.assertIsNone(alpha["board_scope"]["connection_id"])

    def test_grouping_survives_reconnect_run_and_board_changes(self):
        reports = []
        for index, (board, connection) in enumerate((("alpha", CONNECTION), ("beta", RECONNECTED))):
            monitor = self.retained_monitor({board: connection}, f"run-group-{index}")
            for _ in range(50):
                self.observe(monitor, board, error=RuntimeError(f"same failure {UNC}"))
            current = [
                json.loads(path.read_text(encoding="utf-8")) for path in self.report_paths(monitor)
            ]
            this_run = [
                report for report in current if report["session_id"] == f"run-group-{index}"
            ]
            self.assertEqual(len(this_run), 1)
            self.assertEqual(monitor._counters.snapshot().total, 50)
            reports.extend(this_run)
            monitor.closeout("grouping-phase")
        self.export(
            "complete-grouping-records", {"reports": reports, "store": self.store_records()}
        )
        self.assertEqual(reports[0]["grouping_key"], reports[1]["grouping_key"])
        self.assertEqual(reports[0]["error_signature"], reports[1]["error_signature"])
        self.assertNotEqual(reports[0]["report_id"], reports[1]["report_id"])
        self.assert_private(reports)


class SuccessfulDeliveryInvalidatesSpoolPaths(MonitorTestCase):
    def test_successful_async_delivery_removes_a_previously_discovered_report_path(self):
        monitor = self.make_monitor()
        # Observe the persisted path before starting the real background sender.
        enqueued = []
        finished = threading.Event()
        enqueue = monitor._delivery.enqueue_report
        send = monitor._delivery._send_report

        def capture(report):
            path = self.server_data / monitor._workspace / "reports" / f"{report['report_id']}.json"
            enqueued.append((path, json.loads(path.read_text(encoding="utf-8"))))
            enqueue(report)

        def sent(report):
            send(report)
            finished.set()

        with (
            patch.object(monitor._delivery, "enqueue_report", side_effect=capture),
            patch.object(monitor._delivery, "_send_report", side_effect=sent),
        ):
            call = monitor.begin("read_memory", {}, "alpha")
            assert call is not None
            call.failed(RuntimeError("report-reader-race-control"))
            monitor.boot()
            self.assertTrue(finished.wait(5), "delivery did not complete")
        self.assertEqual(len(enqueued), 1)
        path, before = enqueued[0]
        delivered = self.simulated_remote / monitor._workspace / "reports" / path.name
        self.assertEqual(json.loads(delivered.read_text(encoding="utf-8")), before)
        # Reproduce the original readers' failure deterministically, after the
        # real sender has acknowledged and removed a once-valid spool path.
        from tests.test_monitor_behaviour import ReportingThroughTheMonitor

        with self.assertRaises(FileNotFoundError):
            ReportingThroughTheMonitor.read_report(self, path)
