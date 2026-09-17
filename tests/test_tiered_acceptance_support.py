"""Acceptance-support isolation around import-time monitoring storage."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp.monitor.classify import Signal
from pyocd_debug_mcp.monitor.paths import StoreState

from tests.tiered_acceptance_support import (
    MONITOR_RELATIVE,
    isolated_project,
    tiered_test_server,
)


class TieredAcceptanceMonitorIsolationTests(unittest.TestCase):
    """The seven observed acceptance error-path reports must be disposable."""

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
