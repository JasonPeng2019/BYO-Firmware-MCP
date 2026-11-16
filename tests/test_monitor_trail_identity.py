"""VI11: inspect whole retained, persisted and locally delivered report records."""

from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import time

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
