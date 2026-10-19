"""Raw recovery needs fresh human authority and revokes its session after any attempt."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository
from pyocd_debug_mcp.capabilities.routing import TierRouter
from pyocd_debug_mcp.flash_image import FlashResult
from pyocd_debug_mcp.services.uart_exchange_schema import MAX_UART_TEXT_BYTES
from pyocd_debug_mcp.tools.raw import (
    RawRecoveryRefusal,
    RawToolServices,
    build_raw_handlers,
)

_PERMIT = object()


class RawRecoveryLifecycleTests(unittest.TestCase):
    def _handlers(self, recover, finalize=None, capture=None, authorize=_PERMIT):  # type: ignore[no-untyped-def]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        router = TierRouter(CapabilityPolicyRepository(Path(temporary.name)))
        router.state_for("raw_board")
        finalized: list[str] = []
        self.authorized: list[tuple[str, str]] = []
        if authorize is _PERMIT:
            authorize = lambda board_id, mechanism: self.authorized.append((board_id, mechanism))  # noqa: E731
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
                flash=lambda *_args: (Path("artifact"), FlashResult(((0, 1),), "halted")),
                recover=recover,
                capture_uart=capture or (lambda *_args, **_kwargs: None),
                write_uart=lambda *_args, **_kwargs: None,
                exchange_uart=lambda *_args, **_kwargs: None,
                finalize_recovery=finalize or finalized.append,
                authorize_recovery=authorize,
            )
        )
        return handlers, finalized

    def test_raw_recovery_finalizes_after_success(self) -> None:
        handlers, finalized = self._handlers(lambda *_args: "recovered")
        result = json.loads(handlers["target_unlock_raw"]("raw_board", "backend_mass_erase"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.authorized, [("raw_board", "backend_mass_erase")])
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

    def test_raw_recovery_without_an_authorizer_fails_closed_before_backend(self) -> None:
        """A server build that forgets the human-grant gate must not erase."""

        recovered: list[object] = []
        handlers, finalized = self._handlers(
            lambda *args: recovered.append(args) or "recovered", authorize=None
        )
        result = json.loads(handlers["target_unlock_raw"]("raw_board", "backend_mass_erase"))
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["code"], "manual/unavailable")
        self.assertFalse(result["attempted"])
        self.assertEqual(recovered, [])
        self.assertEqual(finalized, [], "a refused request must keep the healthy session")

    def test_raw_recovery_refused_grant_makes_no_backend_call_and_keeps_session(self) -> None:
        """Absent, stale, replayed or changed-binding grants all refuse before I/O."""

        recovered: list[object] = []

        def refuse(board_id: str, mechanism: str) -> None:
            raise RawRecoveryRefusal(
                "manual/binding-mismatch",
                "manual grant does not match the current disclosure",
                {"manual_grant": {"action": "mass-erase", "state": "unlocked"}},
            )

        handlers, finalized = self._handlers(
            lambda *args: recovered.append(args) or "recovered", authorize=refuse
        )
        result = json.loads(handlers["target_unlock_raw"]("raw_board", "backend_mass_erase"))
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["code"], "manual/binding-mismatch")
        self.assertFalse(result["attempted"])
        self.assertEqual(result["manual_grant"]["action"], "mass-erase")
        self.assertIn("$mass-erase", " ".join(result["remedies"]))
        self.assertEqual(recovered, [])
        self.assertEqual(finalized, [])

    def test_raw_recovery_authorizer_fault_is_refused_without_backend_call(self) -> None:
        recovered: list[object] = []

        def broken(board_id: str, mechanism: str) -> None:
            raise OSError("grant store unreadable")

        handlers, finalized = self._handlers(
            lambda *args: recovered.append(args) or "recovered", authorize=broken
        )
        result = json.loads(handlers["target_unlock_raw"]("raw_board", "backend_mass_erase"))
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["code"], "manual/authorization-failed")
        self.assertIn("grant store unreadable", result["message"])
        self.assertEqual(recovered, [])
        self.assertEqual(finalized, [])

    def test_raw_recovery_spends_authority_before_backend_and_finalizes_after_cancellation(
        self,
    ) -> None:
        """Cancellation mid-erase is an uncertain destructive outcome."""

        order: list[str] = []

        def cancelled(*_args: object) -> str:
            order.append("recover")
            raise asyncio.CancelledError

        handlers, _ = self._handlers(
            cancelled,
            finalize=lambda board_id: order.append(f"finalize:{board_id}"),
            authorize=lambda board_id, mechanism: order.append("authorize"),
        )
        with self.assertRaises(asyncio.CancelledError):
            handlers["target_unlock_raw"]("raw_board", "backend_mass_erase")
        self.assertEqual(order, ["authorize", "recover", "finalize:raw_board"])

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
