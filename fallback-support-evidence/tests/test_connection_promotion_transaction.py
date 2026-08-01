from __future__ import annotations

import inspect
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from pyocd_debug_mcp import server
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.adapters.swd_process import _WorkerClient
from pyocd_debug_mcp.services.connections import ConnectionManager
from pyocd_debug_mcp.services.session_runtime import InMemorySessionStore


def _handle(uid: str, token: str) -> TargetSessionHandle:
    del token
    return TargetSessionHandle(None, None, uid, "worker", None)


class ConnectionPromotionTransactionTests(unittest.TestCase):
    def test_every_open_path_uses_the_shared_promotion_transaction(self) -> None:
        for function in (
            server._connect_impl,
            server._connect_under_reset_impl,
            server._validation_connect,
        ):
            with self.subTest(function=function.__name__):
                self.assertIn("_promote_open_session(", inspect.getsource(function))

    def test_start_session_failure_always_closes_the_new_worker(self) -> None:
        manager = ConnectionManager()
        handle = _handle("probe-new", "token-new")
        store = SimpleNamespace(
            start_session=Mock(side_effect=RuntimeError("summary persistence failed")),
            close_session=Mock(),
        )

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "_session_store", store),
            patch.object(server, "gate_manager", SimpleNamespace(clear=Mock())),
            patch.object(server.target_control, "close_session") as close_worker,
        ):
            with self.assertRaisesRegex(RuntimeError, "summary persistence failed"):
                server._promote_open_session(
                    "board-new", handle, gate_reason="validation required"
                )

        self.assertIsNone(manager.maybe_connection("board-new"))
        store.close_session.assert_not_called()
        close_worker.assert_called_once_with(handle)

    def test_start_session_failure_releases_real_worker_marker(self) -> None:
        worker = Path(__file__).with_name("fake_provider_worker.py")
        client = _WorkerClient(
            worker_argv=(sys.executable, str(worker), "good"),
            deadline=time.monotonic() + 10,
        )
        marker = cast(Path, client._marker)
        self.assertTrue(marker.is_file())
        handle = TargetSessionHandle(
            None,
            None,
            "probe-new",
            "worker",
            None,
            worker=client,
        )
        store = SimpleNamespace(
            start_session=Mock(side_effect=RuntimeError("summary persistence failed")),
            close_session=Mock(),
        )

        with (
            patch.object(server, "connection_manager", ConnectionManager()),
            patch.object(server, "_session_store", store),
            patch.object(server, "gate_manager", SimpleNamespace(clear=Mock())),
        ):
            with self.assertRaisesRegex(RuntimeError, "summary persistence failed"):
                server._promote_open_session(
                    "board-new", handle, gate_reason="validation required"
                )

        self.assertTrue(client._closed)
        self.assertFalse(marker.exists())
        client._process.wait(timeout=2)
        store.close_session.assert_not_called()

    def test_failed_summary_write_does_not_publish_a_partial_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = InMemorySessionStore(Path(temporary))
            with patch.object(
                store,
                "_write_summary",
                side_effect=OSError("summary persistence failed"),
            ):
                with self.assertRaisesRegex(OSError, "summary persistence failed"):
                    store.start_session(
                        board_id="board-new",
                        connection_id="probe:probe-new",
                        probe_uid="probe-new",
                        route_used="worker",
                    )

            self.assertEqual(store._sessions, {})

    def test_failed_close_summary_still_unpublishes_the_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = InMemorySessionStore(Path(temporary))
            runtime = store.start_session(
                board_id="board-new",
                connection_id="probe:probe-new",
                probe_uid="probe-new",
                route_used="worker",
            )
            with patch.object(
                store,
                "_write_summary",
                side_effect=OSError("close summary failed"),
            ):
                with self.assertRaisesRegex(OSError, "close summary failed"):
                    store.close_session(runtime)

            self.assertNotIn(runtime.session_id, store._sessions)

    def test_duplicate_physical_assignment_preserves_owner_and_releases_new_resources(self) -> None:
        manager = ConnectionManager()
        existing_handle = _handle("same-probe", "token-existing")
        existing_runtime = Mock(name="existing_runtime")
        existing = manager.assign("board-existing", existing_handle, existing_runtime)
        new_handle = _handle("same-probe", "token-new")
        new_runtime = Mock(name="new_runtime")
        store = SimpleNamespace(
            start_session=Mock(return_value=new_runtime),
            close_session=Mock(),
        )

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "_session_store", store),
            patch.object(server, "gate_manager", SimpleNamespace(clear=Mock())),
            patch.object(server.target_control, "close_session") as close_worker,
        ):
            with self.assertRaisesRegex(
                server.ConnectionAssignmentError,
                "already assigned to board 'board-existing'",
            ):
                server._promote_open_session(
                    "board-new", new_handle, gate_reason="validation required"
                )

        self.assertIs(manager.maybe_connection("board-existing"), existing)
        self.assertIsNone(manager.maybe_connection("board-new"))
        store.close_session.assert_called_once_with(new_runtime)
        close_worker.assert_called_once_with(new_handle)

    def test_gate_failure_rolls_back_exact_assignment_and_preserves_other_board(self) -> None:
        manager = ConnectionManager()
        other_handle = _handle("probe-other", "token-other")
        other = manager.assign("board-other", other_handle, Mock(name="other_runtime"))
        new_handle = _handle("probe-new", "token-new")
        new_runtime = Mock(name="new_runtime")
        store = SimpleNamespace(
            start_session=Mock(return_value=new_runtime),
            close_session=Mock(),
        )
        gate = SimpleNamespace(clear=Mock(side_effect=ValueError("gate persistence failed")))

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "_session_store", store),
            patch.object(server, "gate_manager", gate),
            patch.object(server.target_control, "close_session") as close_worker,
        ):
            with self.assertRaisesRegex(ValueError, "gate persistence failed"):
                server._promote_open_session(
                    "board-new", new_handle, gate_reason="validation required"
                )

        self.assertIsNone(manager.maybe_connection("board-new"))
        self.assertIs(manager.maybe_connection("board-other"), other)
        store.close_session.assert_called_once_with(new_runtime)
        close_worker.assert_called_once_with(new_handle)

    def test_gate_failure_does_not_remove_concurrent_replacement(self) -> None:
        manager = ConnectionManager()
        other = manager.assign(
            "board-other",
            _handle("probe-other", "token-other"),
            Mock(name="other_runtime"),
        )
        new_handle = _handle("probe-new", "token-new")
        new_runtime = Mock(name="new_runtime")
        replacement_handle = _handle("probe-replacement", "token-replacement")
        replacement_runtime = Mock(name="replacement_runtime")
        replacement = None

        def replace_then_fail(board_id: str, reason: str) -> None:
            nonlocal replacement
            self.assertEqual((board_id, reason), ("board-new", "validation required"))
            manager.clear("board-new")
            replacement = manager.assign(
                "board-new", replacement_handle, replacement_runtime
            )
            raise ValueError("gate persistence failed")

        store = SimpleNamespace(
            start_session=Mock(return_value=new_runtime),
            close_session=Mock(),
        )

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "_session_store", store),
            patch.object(
                server,
                "gate_manager",
                SimpleNamespace(clear=Mock(side_effect=replace_then_fail)),
            ),
            patch.object(server.target_control, "close_session") as close_worker,
        ):
            with self.assertRaisesRegex(ValueError, "gate persistence failed"):
                server._promote_open_session(
                    "board-new", new_handle, gate_reason="validation required"
                )

        self.assertIsNotNone(replacement)
        self.assertIs(manager.maybe_connection("board-new"), replacement)
        self.assertIs(manager.maybe_connection("board-other"), other)
        store.close_session.assert_called_once_with(new_runtime)
        close_worker.assert_called_once_with(new_handle)

    def test_cleanup_diagnostics_are_chained_without_changing_primary(self) -> None:
        manager = ConnectionManager()
        handle = _handle("probe-new", "token-new")
        runtime = Mock(name="runtime")
        store = SimpleNamespace(
            start_session=Mock(return_value=runtime),
            close_session=Mock(side_effect=OSError("runtime cleanup failed")),
        )
        gate = SimpleNamespace(clear=Mock(side_effect=ValueError("primary gate failure")))

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "_session_store", store),
            patch.object(server, "gate_manager", gate),
            patch.object(
                server.target_control,
                "close_session",
                side_effect=RuntimeError("worker cleanup unconfirmed"),
            ),
        ):
            with self.assertRaises(ValueError) as raised:
                server._promote_open_session(
                    "board-new", handle, gate_reason="validation required"
                )

        self.assertEqual(str(raised.exception), "primary gate failure")
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        cause = str(raised.exception.__cause__)
        self.assertIn("runtime cleanup failed", cause)
        self.assertIn("worker cleanup unconfirmed", cause)
        self.assertIsNone(manager.maybe_connection("board-new"))


if __name__ == "__main__":
    unittest.main()
