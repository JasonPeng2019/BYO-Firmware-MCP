"""Independent regressions for H05 worker-marker close behavior."""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from pyocd_debug_mcp.adapters import swd_process
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.target_errors import TargetConnectionError


class H05MarkerUnlinkRegressionTests(unittest.TestCase):
    @staticmethod
    def _client(marker: Path) -> Any:
        client = cast(Any, object.__new__(swd_process._WorkerClient))
        client._guard = RLock()
        client._request_id = 0
        client._closed = False
        client._cleanup_confirmed = False
        client._marker = marker
        client._process = SimpleNamespace(stdin=None, stdout=None)
        client._write = Mock()
        client._terminate = Mock(return_value=True)
        return client

    @staticmethod
    def _handle(client: Any) -> TargetSessionHandle:
        return TargetSessionHandle(None, None, None, "worker", None, worker=client)

    def test_interface_close_propagates_retained_marker_failure_and_retry_is_marker_only(
        self,
    ) -> None:
        marker = Path("regression-h05-retained-marker.json")
        client = self._client(marker)
        client._read = Mock(side_effect=EOFError("worker stopped"))
        interface = swd_process.ProcessIsolatedSWDInterface()

        with patch.object(
            swd_process.ProcessMarkerStore,
            "remove",
            side_effect=(OSError("regression unlink failure"), None),
        ) as remove:
            with self.assertRaises(TargetConnectionError) as raised:
                interface.close(self._handle(client))

            self.assertIn("Recovery marker removal failed", str(raised.exception))
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertEqual(str(raised.exception.__cause__), "regression unlink failure")
            self.assertTrue(client._cleanup_confirmed)
            self.assertEqual(client._marker, marker)
            self.assertEqual(client._request_id, 1)
            client._terminate.assert_called_once_with()

            interface.close(self._handle(client))

        self.assertIsNone(client._marker)
        self.assertEqual(client._request_id, 1)
        client._write.assert_called_once()
        client._terminate.assert_called_once_with()
        self.assertEqual(remove.call_args_list, [((marker,), {}), ((marker,), {})])

    def test_provider_close_failure_is_diagnostic_after_confirmed_cleanup(self) -> None:
        marker = Path("regression-h05-diagnostic-marker.json")
        client = self._client(marker)
        client._read = Mock(
            return_value={
                "version": 1,
                "request_id": 1,
                "ok": False,
                "error": {"kind": "provider_failure", "message": "already closed"},
            }
        )

        with patch.object(swd_process.ProcessMarkerStore, "remove") as remove:
            client.close(deadline=time.monotonic() + 1)

        self.assertTrue(client._cleanup_confirmed)
        self.assertIsNone(client._marker)
        self.assertEqual(client._request_id, 1)
        client._terminate.assert_called_once_with()
        remove.assert_called_once_with(marker)

    def test_unconfirmed_cleanup_remains_fail_closed_without_marker_removal(self) -> None:
        marker = Path("regression-h05-unconfirmed-marker.json")
        client = self._client(marker)
        client._read = Mock(side_effect=EOFError("worker stopped"))
        client._terminate.return_value = False

        with patch.object(swd_process.ProcessMarkerStore, "remove") as remove:
            with self.assertRaisesRegex(TargetConnectionError, "marker retained"):
                client.close(deadline=time.monotonic() + 1)

        self.assertFalse(client._cleanup_confirmed)
        self.assertEqual(client._marker, marker)
        client._terminate.assert_called_once_with()
        remove.assert_not_called()
