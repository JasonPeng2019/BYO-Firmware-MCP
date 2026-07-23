from __future__ import annotations

import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import anyio

from firmware_mcp import server
from firmware_mcp.adapters.debug_interface import TargetSessionHandle
from firmware_mcp.adapters.debug_process import _WorkerClient
from firmware_mcp.kernel.operations import OperationCancelledError, OperationManager, dispatch
from firmware_mcp.services.connections import ConnectionManager
from firmware_mcp.services.session_runtime import InMemorySessionStore


def _handle(uid: str, token: str) -> TargetSessionHandle:
    del token
    return TargetSessionHandle(None, None, uid, "worker", None)


class ConnectionPromotionTransactionTests(unittest.TestCase):
    def test_every_open_path_uses_the_shared_promotion_transaction(self) -> None:
        for function in (
            server._connect_impl,
            server._connect_with_wired_reset_impl,
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
            patch.object(server.target_control, "close_session") as close_worker,
        ):
            with self.assertRaisesRegex(RuntimeError, "summary persistence failed"):
                server._promote_open_session("board-new", handle)

        self.assertIsNone(manager.maybe_connection("board-new"))
        store.close_session.assert_not_called()
        close_worker.assert_called_once_with(handle)

    def test_successful_promotion_detaches_worker_bootstrap_ownership(self) -> None:
        manager = ConnectionManager()
        worker = Mock()
        handle = TargetSessionHandle(None, None, "probe-new", "worker", None, worker=worker)
        runtime = Mock(name="runtime")
        store = SimpleNamespace(start_session=Mock(return_value=runtime), close_session=Mock())

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "_session_store", store),
        ):
            assignment = server._promote_open_session("board-new", handle)

        self.assertEqual(assignment.board_id, "board-new")
        worker.promote_to_session.assert_called_once_with()

    def test_start_session_failure_releases_real_worker_marker(self) -> None:
        worker = Path(__file__).with_name("fake_provider_worker.py")
        client = _WorkerClient(
            worker_argv=(sys.executable, str(worker), "good"),
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
        ):
            with self.assertRaisesRegex(RuntimeError, "summary persistence failed"):
                server._promote_open_session("board-new", handle)

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
            patch.object(server.target_control, "close_session") as close_worker,
        ):
            with self.assertRaisesRegex(
                server.ConnectionAssignmentError,
                "already assigned to board 'board-existing'",
            ):
                server._promote_open_session("board-new", new_handle)

        self.assertIs(manager.maybe_connection("board-existing"), existing)
        self.assertIsNone(manager.maybe_connection("board-new"))
        store.close_session.assert_called_once_with(new_runtime)
        close_worker.assert_called_once_with(new_handle)

    def test_cancellation_from_worker_promotion_rolls_back_only_new_session(self) -> None:
        """A cancellation raised inside promotion is before the locked commit."""

        manager = ConnectionManager()
        other_runtime = Mock(name="other_runtime")
        other = manager.assign("board-other", _handle("probe-other", "other"), other_runtime)
        operation_manager = OperationManager()
        runtime = Mock(name="new_runtime")
        worker = Mock()
        handle = TargetSessionHandle(None, None, "probe-new", "worker", None, worker=worker)
        store = SimpleNamespace(start_session=Mock(return_value=runtime), close_session=Mock())

        def cancel_during_promotion() -> None:
            operation_manager.cancel_request("cancel-during-promotion", "cancel during promotion")

        worker.promote_to_session.side_effect = cancel_during_promotion

        async def scenario() -> None:
            with self.assertRaises(OperationCancelledError):
                await dispatch(
                    "connect_board",
                    "board-new",
                    lambda: server._promote_open_session(
                        "board-new", handle, commit_operation=True
                    ),
                    request_id="cancel-during-promotion",
                    manager=operation_manager,
                )

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "_session_store", store),
            patch.object(server.target_control, "close_session") as close_worker,
        ):
            anyio.run(scenario)

        self.assertIsNone(manager.maybe_connection("board-new"))
        self.assertIs(manager.maybe_connection("board-other"), other)
        store.close_session.assert_called_once_with(runtime)
        close_worker.assert_called_once_with(handle)

    def test_cancellation_immediately_before_commit_rolls_back_published_facts(self) -> None:
        """A cancellation at the commit edge rolls back the staged publication."""

        manager = ConnectionManager()
        other = manager.assign("board-other", _handle("probe-other", "other"), Mock())
        operation_manager = OperationManager()
        runtime = Mock(name="new_runtime")
        worker = Mock()
        handle = TargetSessionHandle(None, None, "probe-new", "worker", None, worker=worker)
        store = SimpleNamespace(start_session=Mock(return_value=runtime), close_session=Mock())

        resolve_operation = server.current_operation

        def cancel_at_commit_edge() -> object:
            # The assignment has been staged, but commit_completion has not yet
            # acquired its state lock or promoted the worker.
            operation_manager.cancel_request("cancel-before-commit", "cancel before commit")
            return resolve_operation()

        async def scenario() -> None:
            with (
                patch.object(
                    server,
                    "current_operation",
                    side_effect=cancel_at_commit_edge,
                ),
                self.assertRaises(OperationCancelledError),
            ):
                await dispatch(
                    "connect_board",
                    "board-new",
                    lambda: server._promote_open_session(
                        "board-new", handle, commit_operation=True
                    ),
                    request_id="cancel-before-commit",
                    manager=operation_manager,
                )

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "_session_store", store),
            patch.object(server.target_control, "close_session") as close_worker,
        ):
            anyio.run(scenario)

        self.assertIsNone(manager.maybe_connection("board-new"))
        self.assertIs(manager.maybe_connection("board-other"), other)
        store.close_session.assert_called_once_with(runtime)
        close_worker.assert_called_once_with(handle)
        worker.promote_to_session.assert_not_called()

    def test_cancellation_immediately_after_commit_keeps_exact_live_sessions(self) -> None:
        """A later cancellation cannot replace a committed connect result."""

        manager = ConnectionManager()
        other_runtime = Mock(name="other_runtime")
        other = manager.assign("board-other", _handle("probe-other", "other"), other_runtime)
        operation_manager = OperationManager()
        runtime = Mock(name="new_runtime")
        worker = Mock()
        handle = TargetSessionHandle(None, None, "probe-new", "worker", None, worker=worker)
        store = SimpleNamespace(start_session=Mock(return_value=runtime), close_session=Mock())

        async def scenario() -> object:
            def promote_then_cancel() -> object:
                assignment = server._promote_open_session(
                    "board-new", handle, commit_operation=True
                )
                operation_manager.cancel_request("cancel-after-commit", "cancel after commit")
                return assignment

            return await dispatch(
                "connect_board",
                "board-new",
                promote_then_cancel,
                request_id="cancel-after-commit",
                manager=operation_manager,
            )

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "_session_store", store),
            patch.object(server.target_control, "close_session") as close_worker,
        ):
            assignment = anyio.run(scenario)

        self.assertIs(manager.maybe_connection("board-new"), assignment)
        self.assertIs(manager.maybe_connection("board-other"), other)
        self.assertIs(getattr(assignment, "runtime_session"), runtime)
        worker.promote_to_session.assert_called_once_with()
        close_worker.assert_not_called()
        store.close_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
