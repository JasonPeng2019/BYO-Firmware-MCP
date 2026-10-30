"""Acceptance-support isolation around import-time monitoring storage."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp.monitor.classify import Signal
from pyocd_debug_mcp.monitor.paths import StoreState

from tests.tiered_acceptance_support import (
    MONITOR_RELATIVE,
    isolated_project,
    tiered_test_server,
    _remove_monitor_root,
)


class TieredAcceptanceMonitorIsolationTests(unittest.TestCase):
    """The seven observed acceptance error-path reports must be disposable."""

    def test_acknowledged_segment_can_disappear_during_fixture_removal(self) -> None:
        """A real concurrent unlink after enumeration must leave no owned data."""

        with tempfile.TemporaryDirectory(prefix="monitor-removal-") as temporary:
            root = Path(temporary) / "monitor"
            root.mkdir()
            segment = root / "acknowledged.jsonl"
            segment.write_text("sealed\n", encoding="utf-8")
            enumerated, deleted = threading.Event(), threading.Event()
            unlink = os.unlink

            def acknowledge() -> None:
                if enumerated.wait(5):
                    unlink(segment)
                    deleted.set()

            def remove(path, *args, **kwargs):
                if Path(path).name == segment.name:
                    enumerated.set()
                    self.assertTrue(deleted.wait(5), "Concurrent ACK did not unlink its real file")
                return unlink(path, *args, **kwargs)

            worker = threading.Thread(target=acknowledge)
            worker.start()
            try:
                with patch("os.unlink", side_effect=remove):
                    _remove_monitor_root(root)
            finally:
                enumerated.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertTrue(deleted.is_set())
            self.assertFalse(root.exists())

    def test_fixture_removal_does_not_hide_permission_denial(self) -> None:
        with tempfile.TemporaryDirectory(prefix="monitor-denial-") as temporary:
            root = Path(temporary) / "monitor"
            root.mkdir()
            segment = root / "denied.jsonl"
            segment.write_text("sealed\n", encoding="utf-8")
            with patch("os.unlink", side_effect=PermissionError("controlled denial")):
                with self.assertRaisesRegex(PermissionError, "controlled denial"):
                    _remove_monitor_root(root)
            self.assertTrue(segment.is_file())

    def test_fixture_closes_active_ledger_and_delivery_before_removing_data(self) -> None:
        """E01: a fixture must end the real server lifecycle before strict deletion."""

        with isolated_project() as project_root:
            monitor_root = project_root / MONITOR_RELATIVE
            with tiered_test_server(project_root) as server:
                monitor = server._monitor
                monitor.boot()
                monitor.bind_workspace(project_root)
                handle = monitor._ledger._handle
                self.assertIsNotNone(handle)
                self.assertFalse(handle.closed)
                self.assertTrue(Path(handle.name).is_file())
                thread = monitor._delivery._thread
                self.assertIsNotNone(thread)
                self.assertTrue(thread.is_alive())
            self.assertTrue(handle.closed)
            self.assertIsNone(monitor._ledger._handle)
            self.assertFalse(thread.is_alive())
            self.assertFalse(monitor_root.exists())

    def test_tiered_server_reports_stay_project_local_and_teardown_removes_them(self) -> None:
        """An inherited monitor root cannot receive an in-process acceptance report."""

        with tempfile.TemporaryDirectory(prefix="inherited-monitor-") as temporary:
            inherited_root = Path(temporary).resolve() / "must-not-be-used"
            with patch.dict(os.environ, {"BYO_MCP_MONITOR_ROOT": str(inherited_root)}):
                with isolated_project() as project_root:
                    expected_root = project_root / MONITOR_RELATIVE
                    with tiered_test_server(project_root) as server:
                        monitor = server._monitor  # noqa: SLF001 - import-time storage oracle
                        self.assertEqual(monitor._store.state, StoreState.MONITOR_ROOT)
                        self.assertEqual(monitor._store.root, expected_root)
                        self.assertEqual(os.environ["BYO_MCP_MONITOR_ROOT"], str(expected_root))

                        # Delivery is deliberately out of scope: preserve every
                        # report long enough to prove its durable location.
                        with patch.object(monitor._delivery, "enqueue_report"):
                            for number in range(7):
                                report = monitor._file_report(
                                    signal=Signal.RUNTIME_ERROR,
                                    origin="tiered-acceptance-support",
                                    tool="read_memory_raw",
                                    board="monitor_isolation_board",
                                    anchor=f"acceptance-error-path-{number}",
                                    title=f"acceptance error-path {number}",
                                    description="deterministic monitor containment oracle",
                                    refusal_code="test/error-path",
                                    named_remedy="board_setup",
                                    args_fp=None,
                                )
                                self.assertIsNotNone(report)

                        reports = list(expected_root.glob("server_data/**/reports/*.json"))
                        self.assertEqual(len(reports), 7)
                        for report in reports:
                            self.assertTrue(report.resolve().is_relative_to(project_root), report)
                        self.assertFalse(inherited_root.exists())

                    self.assertFalse(expected_root.exists())
                self.assertFalse(inherited_root.exists())


if __name__ == "__main__":
    unittest.main()
