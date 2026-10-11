"""Thin CAN handler boundaries: cancellation and backend-text redaction."""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from pyocd_debug_mcp.can_errors import CanWorkerError
from pyocd_debug_mcp.can_startup import load_can_startup
from pyocd_debug_mcp.services.can_session import CanSessionManager
from pyocd_debug_mcp.tools.can import CanToolServices, build_can_handlers

ROOT = Path(__file__).resolve().parents[1]
TX_PROFILE = ROOT / "fixtures" / "can" / "virtual_tx_allowed.yaml"


class RaisingManager(CanSessionManager):
    def __init__(self, error: BaseException) -> None:
        super().__init__(
            load_can_startup({"PYOCD_CAN_MODE": "available", "PYOCD_CAN_CONFIG": str(TX_PROFILE)})
        )
        self.error = error

    async def connect(self, board_id: str, probe_uid: str | None):  # type: ignore[no-untyped-def]
        del board_id, probe_uid
        raise self.error


class CanToolBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_failure_does_not_expose_backend_text(self) -> None:
        manager = RaisingManager(CanWorkerError("SECRET:/operator/private/device"))
        handlers = build_can_handlers(
            CanToolServices(manager=manager, operational_identity=lambda _board: None)
        )
        try:
            text = await handlers["connect_can"]("nucleo_l476rg")  # type: ignore[misc]
            payload = json.loads(text)
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["code"], "can/worker-failure")
            self.assertEqual(payload["message"], "The isolated CAN worker failed.")
            self.assertNotIn("SECRET", text)
            self.assertNotIn("/operator", text)
        finally:
            manager.shutdown_sync()

    async def test_caller_cancellation_is_not_converted_to_handler_json(self) -> None:
        manager = RaisingManager(asyncio.CancelledError())
        handlers = build_can_handlers(
            CanToolServices(manager=manager, operational_identity=lambda _board: None)
        )
        try:
            with self.assertRaises(asyncio.CancelledError):
                await handlers["connect_can"]("nucleo_l476rg")  # type: ignore[misc]
        finally:
            manager.shutdown_sync()

    async def test_process_control_exception_is_not_converted_to_handler_json(self) -> None:
        manager = RaisingManager(KeyboardInterrupt("SIGTERM"))
        handlers = build_can_handlers(
            CanToolServices(manager=manager, operational_identity=lambda _board: None)
        )
        try:
            with self.assertRaisesRegex(KeyboardInterrupt, "SIGTERM"):
                await handlers["connect_can"]("nucleo_l476rg")  # type: ignore[misc]
        finally:
            manager.shutdown_sync()


if __name__ == "__main__":
    unittest.main()
