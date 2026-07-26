"""Adversarial specifications for H04 attachment-cache behavior."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

# The neutral command uses the checked-out source, not a similarly named installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyocd_debug_mcp.firmstore.cache import AttachmentCache, ProbeIdentity, SerialEndpoint
from pyocd_debug_mcp.firmstore.reports import ReportWriter
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.setup_flow.preflight import (
    PreflightEngine,
    PreflightInventory,
    PreflightDecision,
    ProbeCandidate,
    SerialCandidate,
    SetupUserInput,
)
from pyocd_debug_mcp.setup_flow.setup import SetupWorkflow


def _probe() -> ProbeIdentity:
    return ProbeIdentity("generic-probe", "probe-usb-identity")


def _uart() -> SerialEndpoint:
    return SerialEndpoint("portable-port", "uart-usb-identity", 0x1234, 0x5678)


class AttachmentCachePersistenceSpecTests(unittest.TestCase):
    """CL-001 and CL-004: stable hints persist once and never become authority."""

    def test_confirmation_is_byte_idempotent_for_the_same_stable_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = AttachmentCache(FirmStore(Path(directory)))
            cache.confirm("board", _probe(), _uart(), confirmed_at="2026-01-01T00:00:00Z")
            before = cache.path.read_bytes()

            cache.confirm("board", _probe(), _uart(), confirmed_at="2026-01-02T00:00:00Z")

            self.assertEqual(cache.path.read_bytes(), before)
            self.assertEqual(len(cache.load_records()), 1)

    def test_ready_workflow_persists_direct_uart_but_not_missing_uart_or_research_stop(self) -> None:
        user_input = SetupUserInput("board", "connection", "board", "part", 115200)
        probe = ProbeCandidate("probe", "probe", "generic-probe", "probe-usb-identity")
        uart = SerialCandidate(
            "uart",
            port_path="portable-port",
            description="uart",
            usb_serial="uart-usb-identity",
            vid=0x1234,
            pid=0x5678,
        )

        def invoke(decision: PreflightDecision) -> Mock:
            callback = Mock()
            with tempfile.TemporaryDirectory() as directory:
                workflow = SetupWorkflow(
                    ReportWriter(FirmStore(Path(directory))),
                    lambda _input: Mock(),
                    preflight=Mock(evaluate=Mock(return_value=decision)),
                    on_cache_confirmation=callback,
                )
                workflow.begin_plan("allowance", user_input, mode="setup")
                with patch.object(workflow, "_run_remaining_phases", return_value=("setup_complete", "", ())):
                    workflow.board_setup("allowance", user_input)
            return callback

        direct_ready = PreflightDecision(
            "preflight_ready", "ready", "", selected_probe=probe, selected_serial=uart
        )
        self.assertEqual(invoke(direct_ready).call_count, 1)

        no_uart = PreflightDecision("preflight_ready", "ready", "", selected_probe=probe)
        self.assertEqual(invoke(no_uart).call_count, 0)

        research_stop = PreflightDecision(
            "setup_research_required", "research", "", selected_probe=probe, selected_serial=uart
        )
        self.assertEqual(invoke(research_stop).call_count, 0)

    def test_unconfirmed_external_adapter_stops_before_a_ready_decision(self) -> None:
        decision = PreflightEngine().evaluate(
            SetupUserInput("board", "connection", "board", "part", 115200),
            PreflightInventory(
                probes=(ProbeCandidate("probe", "probe", "generic-probe", "probe-usb-identity"),),
                serial_ports=(
                    SerialCandidate(
                        "external-uart",
                        "portable-port",
                        "external adapter",
                        "uart-usb-identity",
                        0x1234,
                        0x5678,
                        external_adapter=True,
                        provably_mapped=False,
                    ),
                ),
                exact_detected_targets=("generic-target",),
            ),
        )

        self.assertEqual(decision.status, "setup_needs_user_input")
        self.assertEqual(decision.code, "setup/external-adapter-confirmation-required")
        self.assertEqual(decision.selected_serial.serial_id, "external-uart")


class AttachmentCacheStatusSpecTests(unittest.TestCase):
    """CL-002 and CL-003: status reports a portable hint without granting authority."""

    @staticmethod
    def _status(cache: AttachmentCache, serial_ports: tuple[object, ...]) -> dict[str, object]:
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
            patch.object(server._safety_repository, "load_current", side_effect=server.SafetyMapError("absent")),
            patch.object(server.connection_manager, "maybe_connection", return_value=connection),
            patch.object(server.gate_manager, "snapshot", return_value=None),
            patch.object(
                server,
                "_validation_inventory",
                return_value=SimpleNamespace(serial_ports=serial_ports),
            ),
        ):
            return dict(server._get_setup_status("board"))

    @staticmethod
    def _current_uart(port_path: str = "portable-port") -> object:
        return SimpleNamespace(
            serial_id="current-uart",
            port_path=port_path,
            usb_serial="probe-usb-identity",
            vid=0x1234,
            pid=0x5678,
        )

    def test_missing_diagnostic_is_portable_and_status_does_not_create_a_cache_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "non-default-project-root"
            cache = AttachmentCache(FirmStore(root))
            status = self._status(cache, ())

            diagnostic = status["attachment_cache"]
            self.assertEqual(
                diagnostic,
                {
                    "record_kind": "attachment_cache",
                    "authority": "non_authoritative_hint_only",
                    "record_path": ".firm/cache/attachments.json",
                    "present": False,
                    "state": "missing",
                    "remedy": "Complete setup with a stable selected probe and UART pair to record a hint.",
                },
            )
            self.assertFalse(cache.path.exists())
            self.assertNotIn(str(root), str(diagnostic["record_path"]))

    def test_corrupt_or_authority_shaped_cache_cannot_suppress_a_unique_direct_match(self) -> None:
        documents = ("{not json", '{"schema_version": 1, "records": [], "active_gate": true}')
        for document in documents:
            with self.subTest(document=document), tempfile.TemporaryDirectory() as directory:
                cache = AttachmentCache(FirmStore(Path(directory)))
                cache.path.parent.mkdir(parents=True)
                cache.path.write_text(document, encoding="utf-8")

                status = self._status(cache, (self._current_uart(),))

                self.assertEqual(status["attachment_cache"]["state"], "corrupt")
                self.assertTrue(status["uart_attachment_ready"])
                self.assertEqual(status["resolved_uart"]["port_path"], "portable-port")
                self.assertFalse(status["configuration_ready"])
                self.assertFalse(status["live_session_ready"])
                self.assertFalse(status["ready_for_flash_planning"])

    def test_corrupt_hint_cannot_resolve_absent_or_ambiguous_direct_hardware(self) -> None:
        for serial_ports in ((), (self._current_uart("first"), self._current_uart("second"))):
            with self.subTest(serial_ports=serial_ports), tempfile.TemporaryDirectory() as directory:
                cache = AttachmentCache(FirmStore(Path(directory)))
                cache.path.parent.mkdir(parents=True)
                cache.path.write_text("{not json", encoding="utf-8")

                status = self._status(cache, serial_ports)

                self.assertEqual(status["attachment_cache"]["state"], "corrupt")
                self.assertFalse(status["uart_attachment_ready"])
                self.assertIsNone(status["resolved_uart"])

    def test_valid_exact_hint_reports_reuse_without_adding_setup_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = AttachmentCache(FirmStore(Path(directory)))
            cache.confirm(
                "board",
                _probe(),
                SerialEndpoint("portable-port", "probe-usb-identity", 0x1234, 0x5678),
                confirmed_at="2026-01-01T00:00:00Z",
            )

            status = self._status(cache, (self._current_uart(),))

            diagnostic = status["attachment_cache"]
            self.assertEqual(diagnostic["state"], "valid")
            self.assertEqual(diagnostic["record_count"], 1)
            self.assertEqual(diagnostic["resolution_reason"], "exact_match")
            self.assertTrue(diagnostic["reused"])
            self.assertTrue(status["uart_attachment_ready"])
            self.assertFalse(status["ready_for_flash_planning"])

    def test_unavailable_public_handler_has_an_honest_non_service_owned_diagnostic(self) -> None:
        from pyocd_debug_mcp.tools.setup import SetupToolServices, build_setup_handlers

        services = SetupToolServices(
            loader=Mock(),
            plan_engine=Mock(),
            workflow=Mock(),
            validator=Mock(),
            safety_setup=Mock(),
            safety_refresh=Mock(),
            setup_status=None,
        )
        status = json.loads(build_setup_handlers(services)["get_setup_status"]("board"))

        self.assertEqual(status["status"], "setup_status_unavailable")
        self.assertFalse(status["configuration_ready"])
        self.assertFalse(status["live_session_ready"])
        self.assertFalse(status["ready_for_code"])
        self.assertFalse(status["uart_attachment_ready"])
        self.assertFalse(status["ready_for_uart_work"])
        self.assertEqual(
            status["attachment_cache"],
            {
                "record_kind": "attachment_cache",
                "authority": "non_authoritative_hint_only",
                "record_path": None,
                "present": False,
                "state": "unavailable",
                "remedy": "Setup status service is unavailable; stop before hardware access.",
            },
        )
