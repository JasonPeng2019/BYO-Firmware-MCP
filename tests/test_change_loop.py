"""Routing, artifact selection, truthful flash state and recovery cleanup."""

from __future__ import annotations
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.services.connections import BoardNotConnectedError, ConnectionManager
from pyocd_debug_mcp import server
from pyocd_debug_mcp.services.session_runtime import ActionContext, SessionRecord, ToolOutcome
from pyocd_debug_mcp.tools.flash import FlashToolServices, build_flash_handlers


class ChangeLoopSpecTests(unittest.TestCase):

    def test_cl003_flash_tool_logs_and_explains_unconfirmed_state_without_running_claim(
        self,
    ) -> None:
        from pyocd_debug_mcp.services.session_runtime import (
            SessionRecord,
        )

        root = Path("test-run")
        runtime = SessionRecord(
            "session",
            "profile-board",
            "connection",
            "probe",
            "fake",
            "now",
            root,
            root / "events",
            root / "summary",
        )
        events: list[dict[str, object]] = []
        request = SimpleNamespace(
            artifact_path=Path("firmware.elf"),
            identity=SimpleNamespace(as_log_fields=lambda: {"artifact_sha256": "digest"}),
        )
        services = FlashToolServices(
            runtime_for=lambda _board: runtime,
            active_session_id=lambda _board: "session",
            duration_ms=lambda _started: 1,
            record_event=lambda *_args, **kwargs: events.append(kwargs),
            format_refusal=lambda refusal, **_kwargs: str(refusal),
            action_context=lambda tool, board: ActionContext("test", tool, board),
            maybe_handle_for=lambda _board: None,
            handle_for=lambda _board: object(),
            resolve_request=lambda *_args: request,
            flash_target=lambda *_args: (Path("firmware.elf"), "reset_state_unconfirmed"),
            error_code=lambda _exc: "flash/backend",
        )
        reply = build_flash_handlers(services)["flash_application"]("profile-board", "firmware.elf")

        self.assertIn("reset state is unconfirmed", reply)
        self.assertIn("reconnect and check target state", reply)
        self.assertNotIn("target left running", reply)
        self.assertEqual(events[-1]["outcome_kind"], ToolOutcome.SUCCESS)
        self.assertEqual(
            events[-1]["details"],
            {"target_state": "reset_state_unconfirmed", "safety_map_checked": True},
        )


class RecoveryCleanupRegressionTests(unittest.TestCase):
    def test_successful_recovery_closes_both_resources_and_requires_fresh_connection(self) -> None:
        manager = ConnectionManager()
        handle = TargetSessionHandle(None, None, "probe", "route", None)
        root = Path("test-run")
        runtime = SessionRecord(
            "session",
            "board",
            "connection",
            "probe",
            "route",
            "now",
            root,
            root / "events",
            root / "summary",
        )
        manager.assign("board", handle, runtime)
        gate = Mock()
        assignments = Mock()
        plans = Mock()
        sessions = Mock()

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "gate_manager", gate),
            patch.object(server, "assignment_store", assignments),
            patch.object(server, "plan_engine", plans),
            patch.object(server, "_session_store", sessions),
            patch.object(server.target_control, "close_session") as close_target,
        ):
            server._finalize_unlock_recovery("board")

        with self.assertRaises(BoardNotConnectedError):
            manager.handle_for("board")
        close_target.assert_called_once_with(handle)
        sessions.mark_recover_completed.assert_called_once_with(runtime)
        sessions.close_session.assert_called_once_with(runtime)
        gate.clear.assert_called_once_with(
            "board", "target recovery completed; reconnect and validate required"
        )
        assignments.clear_board.assert_called_once_with("board")
        plans.invalidate_board.assert_called_once_with("board", "target recovery completed")

