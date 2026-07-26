"""Regression coverage for H04's non-authoritative attachment cache."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyocd_debug_mcp.firmstore.cache import AttachmentCache, ProbeIdentity, SerialEndpoint
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.setup_flow.preflight import PreflightDecision, SetupUserInput
from pyocd_debug_mcp.setup_flow.setup import SetupWorkflow


class AttachmentCacheWorkflowRegressionTests(unittest.TestCase):
    @staticmethod
    def _run_workflow(decision: PreflightDecision) -> Mock:
        user_input = SetupUserInput("board", "connection", "board", "part", 115200)
        callback = Mock()
        with tempfile.TemporaryDirectory() as directory:
            workflow = SetupWorkflow(
                ReportWriter(FirmStore(Path(directory))),
                lambda _input: Mock(),
                preflight=Mock(evaluate=Mock(return_value=decision)),
                on_cache_confirmation=callback,
            )
            workflow.begin_plan("allowance", user_input, mode="setup")
            if decision.status == "preflight_ready":
                with patch.object(workflow, "_run_remaining_phases", return_value=("setup_complete", "", ())):
                    workflow.board_setup("allowance", user_input)
            else:
                workflow.board_setup("allowance", user_input)
        return callback

    def test_external_adapter_confirmation_stop_does_not_persist_a_hint(self) -> None:
        """A selected external UART remains unpersisted until preflight is actually ready."""

        decision = PreflightDecision(
            "setup_needs_user_input",
            "setup/confirm-uart-attachment",
            "Confirm the selected external UART.",
            cache_confirmation_required=True,
        )
        self._run_workflow(decision).assert_not_called()

    def test_preflight_ready_without_a_uart_never_calls_the_cache_writer(self) -> None:
        callback = self._run_workflow(PreflightDecision("preflight_ready", "ready", ""))

        callback.assert_not_called()


class AttachmentCacheStatusRegressionTests(unittest.TestCase):
    @staticmethod
    def _status(cache: AttachmentCache) -> dict[str, object]:
        from pyocd_debug_mcp import server

        profile = SimpleNamespace(
            safety_ref="", board=SimpleNamespace(probe_family="generic-probe"), mcu_part_number=None
        )
        connection = SimpleNamespace(
            connection_id="connection", handle=SimpleNamespace(probe_uid="probe-usb-identity")
        )
        with (
            patch.object(server, "_attachment_cache", cache),
            patch.object(server._profile_repository, "load", return_value=profile),
            patch.object(
                server._safety_repository, "load_current", side_effect=server.SafetyMapError("absent")
            ),
            patch.object(server.connection_manager, "maybe_connection", return_value=connection),
            patch.object(server.gate_manager, "snapshot", return_value=None),
            patch.object(server, "_validation_inventory", return_value=SimpleNamespace(serial_ports=())),
        ):
            return dict(server._get_setup_status("board"))

    def test_valid_cache_status_is_strictly_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = AttachmentCache(FirmStore(Path(directory) / "alternate-project-root"))
            cache.confirm(
                "board",
                ProbeIdentity("generic-probe", "probe-usb-identity"),
                SerialEndpoint("portable-port", "uart-usb-identity", 0x1234, 0x5678),
                confirmed_at="2026-01-01T00:00:00Z",
            )
            before = cache.path.read_bytes()

            status = self._status(cache)

            self.assertEqual(cache.path.read_bytes(), before)
            self.assertEqual(status["attachment_cache"]["state"], "valid")
            self.assertEqual(status["attachment_cache"]["record_count"], 1)
