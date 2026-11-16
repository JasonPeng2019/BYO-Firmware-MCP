from __future__ import annotations

import sys
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


def _handle(uid: str, token: str) -> TargetSessionHandle:
    del token
    return TargetSessionHandle(None, None, uid, "worker", None)


class ConnectionPromotionTransactionTests(unittest.TestCase):

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
                server._promote_open_session("board-new", handle, gate_reason="validation required")

        self.assertTrue(client._closed)
        self.assertFalse(marker.exists())
        client._process.wait(timeout=2)
        store.close_session.assert_not_called()


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


if __name__ == "__main__":
    unittest.main()
