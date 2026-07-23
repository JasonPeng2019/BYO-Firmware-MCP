from __future__ import annotations

import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from firmware_mcp.adapters.debug_interface import FlashVerification, TargetSessionHandle
from firmware_mcp.kernel.operations import ManagedOperation
from firmware_mcp.services.connections import ConnectionManager
from firmware_mcp.services.session_runtime import ActionContext, ToolOutcome
from firmware_mcp.target_errors import (
    FlashFinalResetFailed,
    FlashFinalResetUncertain,
    TargetConnectionError,
)
from firmware_mcp.tools.flash import FlashToolServices, build_flash_handlers


class FlashToolFinalResetEvidenceTests(unittest.TestCase):
    @staticmethod
    def _failure(postcondition: str) -> FlashFinalResetFailed | FlashFinalResetUncertain:
        evidence = FlashVerification(
            "firmware.hex",
            2,
            ((0, 2),),
            "a" * 64,
            "a" * 64,
            postcondition,
            "ObservedResetState",
            "halt_after_reset=true; observed_state=RUNNING; expected_state=HALTED",
        )
        return (
            FlashFinalResetFailed(evidence)
            if postcondition == "failed"
            else FlashFinalResetUncertain(
                evidence,
                TargetConnectionError("reset transport disappeared"),
            )
        )

    def _services(
        self,
        events: list[dict[str, object]],
        *,
        postcondition: str,
    ) -> FlashToolServices:
        failure = self._failure(postcondition)
        request = SimpleNamespace(
            artifact_path=Path("firmware.hex"),
            identity=SimpleNamespace(as_log_fields=lambda: {"artifact_sha256": "a" * 64}),
        )
        return FlashToolServices(
            runtime_for=lambda _board: None,
            active_session_id=lambda _board: None,
            duration_ms=lambda _started: 1,
            record_event=lambda *_args, **kwargs: events.append(kwargs),
            format_invalid=lambda invalid, **_kwargs: str(invalid),
            action_context=lambda tool, board: ActionContext("test", tool, board),
            maybe_handle_for=lambda _board: object(),
            handle_for=lambda _board: object(),
            resolve_request=lambda *_args: request,
            execution_file=lambda _name: b":0100000001FE\n:00000001FF\n",
            flash_target=lambda *_args: (_ for _ in ()).throw(failure),
            error_code=lambda _exc: "target/connection",
        )

    def test_flash_firmware_preserves_verified_write_for_both_reset_failures(self) -> None:
        for postcondition, error_type, completion in (
            ("failed", FlashFinalResetFailed, "failed_postcondition"),
            ("unknown", FlashFinalResetUncertain, "uncertain"),
        ):
            for tool_name in ("flash_firmware",):
                with self.subTest(tool_name=tool_name, postcondition=postcondition):
                    events: list[dict[str, object]] = []
                    handler = build_flash_handlers(
                        self._services(events, postcondition=postcondition)
                    )[tool_name]

                    with self.assertRaises(error_type) as raised:
                        handler("board", "firmware.hex", "application")

                    self.assertEqual(
                        isinstance(raised.exception, TargetConnectionError),
                        postcondition == "unknown",
                    )
                    self.assertEqual(len(events), 1)
                    event = events[0]
                    self.assertEqual(event["outcome_kind"], ToolOutcome.FAILED)
                    details = cast(dict[str, object], event["details"])
                    self.assertIsInstance(details, dict)
                    self.assertEqual(details["program_readback"], "verified")
                    self.assertEqual(details["firmware_path"], "firmware.hex")
                    self.assertEqual(details["byte_count"], 2)
                    self.assertEqual(details["verified_ranges"], ((0, 2),))
                    self.assertEqual(details["expected_sha256"], "a" * 64)
                    self.assertEqual(details["observed_sha256"], "a" * 64)
                    self.assertEqual(details["final_reset_postcondition"], postcondition)
                    self.assertEqual(details["final_reset_error_type"], "ObservedResetState")
                    self.assertEqual(
                        details["final_reset_error_message"],
                        "halt_after_reset=true; observed_state=RUNNING; expected_state=HALTED",
                    )
                    self.assertEqual(details["flash_and_run_completion"], completion)

    def test_flash_firmware_preserves_known_and_unknown_final_reset_evidence(self) -> None:
        from firmware_mcp import server

        request = SimpleNamespace(
            artifact_path=Path("firmware.hex"),
            identity=SimpleNamespace(as_log_fields=lambda: {"artifact_sha256": "a" * 64}),
        )
        for postcondition, error_type, completion in (
            ("failed", FlashFinalResetFailed, "failed_postcondition"),
            ("unknown", FlashFinalResetUncertain, "uncertain"),
        ):
            with self.subTest(postcondition=postcondition):
                events: list[dict[str, object]] = []
                failure = self._failure(postcondition)
                with (
                    patch.object(server.connection_manager, "lock_for", return_value=nullcontext()),
                    patch.object(server, "_runtime_for", return_value=None),
                    patch.object(server.connection_manager, "maybe_connection", return_value=None),
                    patch.object(server, "resolve_flash_request", return_value=request),
                    patch.object(server, "_handle", return_value=object()),
                    patch.object(server.target_control, "flash_firmware", side_effect=failure),
                    patch.object(
                        server,
                        "_record_event",
                        side_effect=lambda *_args, **kwargs: events.append(kwargs),
                    ),
                ):
                    with self.assertRaises(error_type):
                        server.flash_firmware("board", "firmware.hex", halt_after_reset=True)

                self.assertEqual(len(events), 1)
                details = cast(dict[str, object], events[0]["details"])
                self.assertIsInstance(details, dict)
                self.assertEqual(details["program_readback"], "verified")
                self.assertEqual(details["firmware_path"], "firmware.hex")
                self.assertEqual(details["final_reset_postcondition"], postcondition)
                self.assertEqual(details["flash_and_run_completion"], completion)

    def test_only_unknown_final_reset_evicts_the_managed_connection(self) -> None:
        from firmware_mcp import server

        for postcondition in ("failed", "unknown"):
            with self.subTest(postcondition=postcondition):
                manager = ConnectionManager()
                handle = TargetSessionHandle(None, None, "probe", "worker", None)
                assignment = manager.assign("board", handle, Mock(name="runtime"))
                operation = ManagedOperation(
                    operation_id="operation",
                    request_id="request",
                    tool_name="flash_firmware",
                    board_id="board",
                    timeout_seconds=1.0,
                    non_interruptible=True,
                    preserve_halt=False,
                )
                operation.error = self._failure(postcondition)
                with (
                    patch.object(server, "connection_manager", manager),
                    patch.object(server.target_control, "release_reset"),
                    patch.object(server.target_control, "close_session") as close_session,
                    patch.object(server._session_store, "close_session") as close_runtime,
                ):
                    server._bind_managed_board_resources(operation)
                    operation.resources.cleanup(preserve_halt=False)

                if postcondition == "unknown":
                    self.assertIsNone(manager.maybe_connection("board"))
                    close_session.assert_called_once_with(handle)
                    close_runtime.assert_called_once_with(assignment.runtime_session)
                else:
                    self.assertIs(manager.maybe_connection("board"), assignment)
                    close_session.assert_not_called()
                    close_runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
