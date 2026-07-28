"""Adversarial H05 contract tests for worker ownership-marker cleanup."""

from __future__ import annotations

import time
import unittest
import sys
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch


class H05MarkerUnlinkSpecTests(unittest.TestCase):
    @staticmethod
    def _dead_worker_client(marker: Path) -> Any:
        from pyocd_debug_mcp.adapters import swd_process

        client = cast(Any, object.__new__(swd_process._WorkerClient))
        client._guard = RLock()
        client._request_id = 0
        client._closed = False
        client._cleanup_confirmed = False
        client._marker = marker
        client._process = SimpleNamespace(stdin=None, stdout=None)
        client._write = Mock()
        client._read = Mock(side_effect=EOFError("worker pipe closed"))
        client._terminate = Mock(return_value=True)
        return client

    def test_healthy_worker_close_keeps_one_request_and_complete_cleanup(self) -> None:
        """CL-001 regression: normal worker close remains successful and single-request."""
        from pyocd_debug_mcp.adapters.swd_process import _WorkerClient

        worker = Path(__file__).with_name("fake_provider_worker.py")
        client = _WorkerClient(
            worker_argv=(sys.executable, str(worker), "good"),
            deadline=time.monotonic() + 5,
        )
        try:
            client.close(deadline=time.monotonic() + 5)
        finally:
            if not client._closed:
                client.close(deadline=time.monotonic() + 5)

        self.assertEqual(client._request_id, 1)
        self.assertTrue(client._cleanup_confirmed)
        self.assertIsNone(client._marker)
        self.assertIsNotNone(client._process.returncode)

    def test_dead_worker_marker_unlink_failure_escapes_and_later_close_retries_marker_only(
        self,
    ) -> None:
        """CL-001: cleanup failure must not be hidden as a successful graceful close."""
        from pyocd_debug_mcp.adapters import swd_process
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        marker = Path("h05-dead-worker-marker.json")
        client = self._dead_worker_client(marker)

        with patch.object(
            swd_process.ProcessMarkerStore,
            "remove",
            side_effect=(OSError("H05_MARKER_UNLINK"), None),
        ) as remove:
            with self.assertRaises(TargetConnectionError) as raised:
                client.close(deadline=time.monotonic() + 1)

            self.assertIn("Recovery marker removal failed", str(raised.exception))
            self.assertIn("H05_MARKER_UNLINK", str(raised.exception))
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertEqual(str(raised.exception.__cause__), "H05_MARKER_UNLINK")
            self.assertTrue(client._closed)
            self.assertTrue(client._cleanup_confirmed)
            self.assertEqual(client._marker, marker)
            self.assertEqual(client._request_id, 1)
            client._write.assert_called_once()
            client._read.assert_called_once()
            client._terminate.assert_called_once_with()
            remove.assert_called_once_with(marker)

            client.close()

        self.assertIsNone(client._marker)
        self.assertEqual(client._request_id, 1)
        client._write.assert_called_once()
        client._read.assert_called_once()
        client._terminate.assert_called_once_with()
        self.assertEqual(remove.call_args_list, [((marker,), {}), ((marker,), {})])

    def test_completed_nested_eof_invalidation_remains_a_successful_close(self) -> None:
        """CL-001 regression: EOF is diagnostic once death and marker cleanup are complete."""
        from pyocd_debug_mcp.adapters import swd_process

        marker = Path("h05-clean-eof-marker.json")
        client = self._dead_worker_client(marker)

        with patch.object(swd_process.ProcessMarkerStore, "remove") as remove:
            client.close(deadline=time.monotonic() + 1)

        self.assertTrue(client._closed)
        self.assertTrue(client._cleanup_confirmed)
        self.assertIsNone(client._marker)
        self.assertEqual(client._request_id, 1)
        client._terminate.assert_called_once_with()
        remove.assert_called_once_with(marker)

    def test_provider_close_diagnostic_is_suppressed_only_after_complete_cleanup(self) -> None:
        """CL-001 regression: a nonterminal graceful-close error remains non-fatal after cleanup."""
        from pyocd_debug_mcp.adapters import swd_process

        marker = Path("h05-provider-diagnostic-marker.json")
        client = self._dead_worker_client(marker)
        client._read = Mock(
            return_value={
                "version": 1,
                "request_id": 1,
                "ok": False,
                "error": {"kind": "provider_failure", "message": "provider already exited"},
            }
        )

        with patch.object(swd_process.ProcessMarkerStore, "remove") as remove:
            client.close(deadline=time.monotonic() + 1)

        self.assertTrue(client._closed)
        self.assertTrue(client._cleanup_confirmed)
        self.assertIsNone(client._marker)
        self.assertEqual(client._request_id, 1)
        client._terminate.assert_called_once_with()
        remove.assert_called_once_with(marker)

    def test_unconfirmed_nested_eof_invalidation_is_actionable_and_retains_marker(self) -> None:
        """CL-001 regression: no marker removal occurs when worker death is unconfirmed."""
        from pyocd_debug_mcp.adapters import swd_process
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        marker = Path("h05-unconfirmed-worker-marker.json")
        client = self._dead_worker_client(marker)
        client._terminate.return_value = False

        with patch.object(swd_process.ProcessMarkerStore, "remove") as remove:
            with self.assertRaisesRegex(TargetConnectionError, "marker retained"):
                client.close(deadline=time.monotonic() + 1)

        self.assertTrue(client._closed)
        self.assertFalse(client._cleanup_confirmed)
        self.assertEqual(client._marker, marker)
        self.assertEqual(client._request_id, 1)
        client._terminate.assert_called_once_with()
        remove.assert_not_called()
