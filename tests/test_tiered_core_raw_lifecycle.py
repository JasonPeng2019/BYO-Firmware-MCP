"""Raw recovery revokes an invalidated session even when its backend fails."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository
from pyocd_debug_mcp.capabilities.routing import TierRouter
from pyocd_debug_mcp.services.uart_exchange_schema import MAX_UART_TEXT_BYTES
from pyocd_debug_mcp.tools.raw import RawToolServices, build_raw_handlers


class RawRecoveryLifecycleTests(unittest.TestCase):
    def _handlers(self, recover, finalize=None, capture=None):  # type: ignore[no-untyped-def]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        router = TierRouter(CapabilityPolicyRepository(Path(temporary.name)))
        router.state_for("raw_board")
        finalized: list[str] = []
        handlers = build_raw_handlers(
            RawToolServices(
                router=router,
                global_stop=lambda: None,
                handle_for=lambda _board: object(),
                read_memory=lambda *_args: 0,
                read_block=lambda *_args: [],
                write_memory=lambda *_args: None,
                write_register=lambda *_args: None,
                set_breakpoint=lambda *_args: None,
                reset=lambda *_args: None,
                flash=lambda *_args: (Path("artifact"), "halted"),
                recover=recover,
                capture_uart=capture or (lambda *_args, **_kwargs: None),
                write_uart=lambda *_args, **_kwargs: None,
                exchange_uart=lambda *_args, **_kwargs: None,
                finalize_recovery=finalize or finalized.append,
            )
        )
        return handlers, finalized

    def test_raw_recovery_finalizes_after_success(self) -> None:
        handlers, finalized = self._handlers(lambda *_args: "recovered")
        result = json.loads(handlers["target_unlock_raw"]("raw_board", "backend_mass_erase"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(finalized, ["raw_board"])

    def test_raw_recovery_finalizes_after_backend_error(self) -> None:
        def fail(*_args: object) -> str:
            raise RuntimeError("backend reset during erase")

        handlers, finalized = self._handlers(fail)
        result = json.loads(handlers["target_unlock_raw"]("raw_board", "backend_mass_erase"))
        self.assertEqual(result["code"], "backend/recovery-failed")
        self.assertEqual(finalized, ["raw_board"])

    def test_raw_recovery_reports_cleanup_failure_after_a_successful_backend(self) -> None:
        """Cleanup uncertainty is an error only after a successful raw recovery."""

        finalized: list[str] = []

        def fail_cleanup(board_id: str) -> None:
            finalized.append(board_id)
            raise RuntimeError("disconnect failed")

        handlers, _ = self._handlers(lambda *_args: "recovered", fail_cleanup)
        result = json.loads(handlers["target_unlock_raw"]("raw_board", "backend_mass_erase"))
        self.assertEqual(result["code"], "backend/recovery-failed")
        self.assertIn("disconnect failed", result["message"])
        self.assertEqual(finalized, ["raw_board"])

    def test_raw_recovery_preserves_backend_error_when_cleanup_also_fails(self) -> None:
        """A cleanup exception must not replace the uncertain backend outcome."""

        finalized: list[str] = []

        def fail_backend(*_args: object) -> str:
            raise RuntimeError("backend erase failed")

        def fail_cleanup(board_id: str) -> None:
            finalized.append(board_id)
            raise RuntimeError("disconnect failed")

        handlers, _ = self._handlers(fail_backend, fail_cleanup)
        result = json.loads(handlers["target_unlock_raw"]("raw_board", "backend_mass_erase"))
        self.assertEqual(result["code"], "backend/recovery-failed")
        self.assertIn("backend erase failed", result["message"])
        self.assertNotIn("disconnect failed", result["message"])
        self.assertEqual(finalized, ["raw_board"])

    def test_raw_serial_expected_text_cap_rejects_before_transport_open(self) -> None:
        """The raw matcher is bounded alongside payload and captured output."""

        calls: list[object] = []
        handlers, _ = self._handlers(
            lambda *_args: "recovered",
            capture=lambda *_args, **_kwargs: calls.append("capture"),
        )
        result = json.loads(
            handlers["read_serial_raw"](
                "raw_board",
                "COM_TEST",
                115200,
                expected_text="x" * (MAX_UART_TEXT_BYTES + 1),
            )
        )
        self.assertEqual(result["code"], "backend/serial-read-failed")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
