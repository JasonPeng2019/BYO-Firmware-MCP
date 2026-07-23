from __future__ import annotations

import time
import unittest
from unittest.mock import Mock, patch

from firmware_mcp import server
from firmware_mcp.adapters.debug_interface import TargetSessionHandle
from firmware_mcp.services.connections import ConnectionManager
from firmware_mcp.services.session_runtime import ToolOutcome
from firmware_mcp.target_errors import TargetConnectionError


def _connection():
    manager = ConnectionManager()
    handle = TargetSessionHandle(None, None, "probe-1", "worker", None)
    runtime = Mock(name="runtime")
    return manager.assign("board-1", handle, runtime)


class DisconnectCleanupTests(unittest.TestCase):
    def test_worker_marker_failure_still_closes_runtime_and_records_failure(self) -> None:
        connection = _connection()
        worker_failure = TargetConnectionError(
            "Worker cleanup could not be confirmed; recovery marker retained."
        )
        record = Mock()
        with (
            patch.object(server.target_control, "close_session", side_effect=worker_failure),
            patch.object(server._session_store, "close_session") as close_runtime,
            patch.object(server, "_record_event", record),
        ):
            with self.assertRaises(TargetConnectionError) as raised:
                server._finish_disconnect_cleanup("board-1", connection, started=time.monotonic())

        self.assertIs(raised.exception, worker_failure)
        close_runtime.assert_called_once_with(connection.runtime_session)
        self.assertEqual(record.call_args.kwargs["outcome_kind"], ToolOutcome.FAILED)
        self.assertIn("marker retained", record.call_args.kwargs["details"]["message"])

    def test_runtime_summary_failure_never_records_or_returns_success(self) -> None:
        connection = _connection()
        runtime_failure = OSError("close summary failed")
        record = Mock()
        with (
            patch.object(server.target_control, "close_session") as close_worker,
            patch.object(
                server._session_store,
                "close_session",
                side_effect=runtime_failure,
            ),
            patch.object(server, "_record_event", record),
        ):
            with self.assertRaises(OSError) as raised:
                server._finish_disconnect_cleanup("board-1", connection, started=time.monotonic())

        self.assertIs(raised.exception, runtime_failure)
        close_worker.assert_called_once_with(connection.handle)
        self.assertEqual(record.call_args.kwargs["outcome_kind"], ToolOutcome.FAILED)
        self.assertNotEqual(record.call_args.kwargs["outcome_kind"], ToolOutcome.SUCCESS)

    def test_combined_cleanup_failure_reports_both_and_chains_worker_primary(self) -> None:
        connection = _connection()
        worker_failure = TargetConnectionError("worker marker retained")
        runtime_failure = OSError("runtime summary failed")
        with (
            patch.object(server.target_control, "close_session", side_effect=worker_failure),
            patch.object(
                server._session_store,
                "close_session",
                side_effect=runtime_failure,
            ) as close_runtime,
            patch.object(server, "_record_event") as record,
        ):
            with self.assertRaises(RuntimeError) as raised:
                server._finish_disconnect_cleanup("board-1", connection, started=time.monotonic())

        self.assertIs(raised.exception.__cause__, worker_failure)
        self.assertIn("worker marker retained", str(raised.exception))
        self.assertIn("runtime summary failed", str(raised.exception))
        close_runtime.assert_called_once_with(connection.runtime_session)
        self.assertEqual(record.call_args.kwargs["outcome_kind"], ToolOutcome.FAILED)

    def test_long_combined_failure_event_preserves_both_complete_diagnostics(self) -> None:
        connection = _connection()
        worker_text = "worker-marker-" + ("W" * 420)
        runtime_text = "runtime-summary-" + ("R" * 430)
        record = Mock()
        with (
            patch.object(
                server.target_control,
                "close_session",
                side_effect=TargetConnectionError(worker_text),
            ),
            patch.object(
                server._session_store,
                "close_session",
                side_effect=OSError(runtime_text),
            ),
            patch.object(server, "_record_event", record),
        ):
            with self.assertRaises(RuntimeError):
                server._finish_disconnect_cleanup("board-1", connection, started=time.monotonic())

        recorded = record.call_args.kwargs["details"]["message"]
        self.assertGreater(len(recorded), 300)
        self.assertIn(worker_text, recorded)
        self.assertIn(runtime_text, recorded)

    def test_failure_event_error_cannot_skip_runtime_cleanup_or_mask_single_failure(self) -> None:
        connection = _connection()
        worker_failure = TargetConnectionError("worker marker retained")
        event_failure = OSError("event persistence failed")
        with (
            patch.object(server.target_control, "close_session", side_effect=worker_failure),
            patch.object(server._session_store, "close_session") as close_runtime,
            patch.object(server, "_record_event", side_effect=event_failure),
        ):
            with self.assertRaises(TargetConnectionError) as raised:
                server._finish_disconnect_cleanup("board-1", connection, started=time.monotonic())

        self.assertIs(raised.exception, worker_failure)
        self.assertIs(raised.exception.__cause__, event_failure)
        close_runtime.assert_called_once_with(connection.runtime_session)


if __name__ == "__main__":
    unittest.main()
