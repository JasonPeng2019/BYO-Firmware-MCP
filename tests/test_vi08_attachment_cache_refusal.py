"""VI08: real cache admission and managed public UART refusal/recovery.

Only hardware inventory, the transport and a targeted filesystem denial are
simulated. FirmStore, AttachmentCache, planning, dispatch and finalizers are real.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.firmstore.cache import AttachmentCacheError, SerialEndpoint
from pyocd_debug_mcp.firmstore.store import PersistedAuthorityError
from pyocd_debug_mcp.kernel.operations import ManagedOperation
from pyocd_debug_mcp.setup_flow.preflight import (
    PreflightDecision,
    ProbeCandidate,
    SerialCandidate,
    SetupUserInput,
)
from tests import test_vi06_uart_ownership as vi06
from tests.test_probe_selection_records import _snapshot, _uart_row
from tests.tiered_acceptance_support import public_text

OBSERVATIONS = {}
CODE = "uart/attachment-cache-unavailable"
VALID_RECORD = {
    "board_id": "board_a",
    "probe_family": "jlink",
    "probe_usb_serial": "probe_a",
    "uart_usb_serial": "cable_b",
    "uart_vid": 1,
    "uart_pid": 2,
    "confirmed": True,
    "confirmed_at": "2026-01-01T00:00:00Z",
    "revoked_at": None,
}


def document(records):
    return json.dumps({"schema_version": 1, "records": records}).encode()


DAMAGES = {
    "malformed_json": b"<<<<<<< attachment merge\n{",
    "invalid_schema": b'{"schema_version": 2, "records": []}',
    "invalid_records_type": b'{"schema_version": 1, "records": {}}',
    "invalid_record": document([None]),
    "invalid_field_type": document([{**VALID_RECORD, "uart_vid": True}]),
    "invalid_timestamp_type": document([{**VALID_RECORD, "confirmed_at": []}]),
    "invalid_unicode": b'\xff{"schema_version":1,"records":[]}',
    # CPython rejects an over-limit JSON integer with ValueError, not JSONDecodeError.
    "integer_decode_type": b'{"schema_version":' + b"9" * 5000 + b',"records":[]}',
    "persisted_authority": b'{"schema_version":1,"records":[],"active_plan":{}}',
    "unreadable": document([VALID_RECORD]),
    "stat_denied": document([VALID_RECORD]),
}


class ManagedCacheRefusal(unittest.TestCase):
    setUp = vi06.ManagedUartOwnership.setUp
    action = vi06.ManagedUartOwnership.action
    public_plan = vi06.ManagedUartOwnership.public_plan

    def observe(self, name, **facts):
        OBSERVATIONS[self.id() + ":" + name] = facts

    def damage(self, kind):
        self.cache.path.parent.mkdir(parents=True, exist_ok=True)
        self.cache.path.write_bytes(DAMAGES[kind])
        if kind in {"unreadable", "stat_denied"}:
            method = "read_text" if kind == "unreadable" else "exists"
            original = getattr(Path, method)
            cache_path = self.cache.path

            def denied(path, *args, **kwargs):
                if path == cache_path:
                    raise PermissionError(13, "simulated cache access denied", str(path))
                return original(path, *args, **kwargs)

            self.enterContext(patch.object(Path, method, denied))

    def assert_diagnostic(self, message):
        self.assertIn(CODE, message)
        self.assertIn(str(self.cache.path), message)
        for fragment in ("non-authority", "Stop processes", "back up", "move aside", "board_setup"):
            self.assertIn(fragment, message)
        self.assertLess(message.index("Stop processes"), message.index("board_setup"))

    def refused(self, operation, error_type):
        before = self.cache.path.read_bytes()
        self.transport.reset_mock()
        try:
            with self.assertRaises(error_type) as caught:
                operation()
        finally:
            self.observe("attempt", serial_calls=[str(c) for c in self.transport.mock_calls])
        self.assert_diagnostic(str(caught.exception))
        self.assertEqual(self.transport.mock_calls, [], "no serial open/read/write on refusal")
        self.assertEqual(self.cache.path.read_bytes(), before)
        # Check actual exception types and chaining, never accept an arbitrary exception.
        chain = []
        error = caught.exception
        while error is not None:
            chain.append(type(error).__name__)
            error = error.__cause__
        self.assertTrue(
            "AttachmentCacheError" in chain or "PersistedAuthorityError" in chain, chain
        )
        self.observe("refusal", message=str(caught.exception), chain=chain, unchanged=True)
        # A public independent call after refusal proves dispatch/server responsiveness.
        self.assertIn(
            "board_a", public_text(self.server, "get_capabilities", {"board_id": "board_a"})
        )

    def public_action(self, name, **kwargs):
        args = self.public_plan(name, **kwargs)
        return public_text(self.server, name, args)

    def public_finalizer_refusal(self):
        # Both the main automatic action and its automatic finalizer must refuse.
        errors = []
        original = ManagedOperation.run_finalizer

        def record(operation, finalizer):
            original(operation, finalizer)
            errors.extend(operation.resources.cleanup_errors)

        with patch.object(ManagedOperation, "run_finalizer", record):
            self.refused(
                lambda: self.public_action(
                    "write_serial", on_exit={"action": "uart_write", "text": "exit"}
                ),
                ToolError,
            )
        self.assertEqual(len(errors), 1, errors)
        self.assert_diagnostic(errors[0])
        self.observe("finalizer", cleanup_errors=errors)

    def test_managed_handlers_share_typed_boundary(self):
        self.damage("malformed_json")
        for name in ("read_serial", "write_serial", "serial_exchange"):
            with self.subTest(action=name):
                self.refused(lambda: self.action(name), RuntimeError)

    def test_cache_load_context_and_json_cause(self):
        self.damage("malformed_json")
        with self.assertRaises(AttachmentCacheError) as caught:
            self.cache.load_records()
        self.assertIn(str(self.cache.path), str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, json.JSONDecodeError)

    def test_authority_admission_remains_specific_and_closed(self):
        self.damage("persisted_authority")
        with self.assertRaises(PersistedAuthorityError):
            self.cache.load_records()
        self.assertEqual(self.cache.path.read_bytes(), DAMAGES["persisted_authority"])

    def test_existing_directory_is_an_io_refusal_without_deletion(self):
        self.cache.path.mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, CODE) as caught:
            self.action("write_serial")
        self.assert_diagnostic(str(caught.exception))
        self.assertTrue(self.cache.path.is_dir())
        self.assertEqual(self.transport.mock_calls, [])

    def test_explicit_plan_overrides_do_not_read_or_change_damaged_hints(self):
        self.damage("unreadable")
        before = self.cache.path.read_bytes()
        for name in ("read_serial", "write_serial", "serial_exchange"):
            with self.subTest(action=name):
                self.transport.reset_mock()
                result = self.public_action(name, port="COM7")
                self.assertIn("COM7", result)
                self.transport.open.assert_called_once()
                self.transport.close.assert_called_once()
                self.assertEqual(self.cache.path.read_bytes(), before)
        # Deliberate main action must not authorize the automatic on_exit choice.
        errors = []
        original = ManagedOperation.run_finalizer

        def record(operation, finalizer):
            original(operation, finalizer)
            errors.extend(operation.resources.cleanup_errors)

        self.transport.reset_mock()
        with patch.object(ManagedOperation, "run_finalizer", record):
            self.public_action(
                "write_serial", port="COM7", on_exit={"action": "uart_write", "text": "exit"}
            )
        self.transport.open.assert_called_once()
        self.transport.write.assert_called_once()
        self.transport.close.assert_called_once()
        self.assertEqual(self.transport.write.call_args.args[1], b"hello")
        self.assertEqual(len(errors), 1)
        self.assert_diagnostic(errors[0])
        self.assertEqual(self.cache.path.read_bytes(), before)
        self.observe("explicit-main-only", cleanup_errors=errors)

    def test_explicit_session_choice_does_not_bypass_later_damaged_ownership(self):
        self.snapshot = _snapshot(uarts=(_uart_row("COM7"),))
        self.public_action("write_serial", port="COM7")
        self.damage("malformed_json")
        self.refused(lambda: self.public_action("write_serial"), ToolError)

    def test_unrelated_board_corruption_still_blocks_unknown_ownership(self):
        self.cache.path.parent.mkdir(parents=True)
        self.cache.path.write_bytes(
            document([{**VALID_RECORD, "board_id": "other", "uart_pid": []}])
        )
        self.refused(lambda: self.public_action("read_serial"), ToolError)

    def test_valid_other_board_records_remain_unchanged(self):
        self.cache.confirm("board_b", vi06.ProbeIdentity("jlink", "probe_b"), self.cable_b)
        self.cache.confirm("board_a", self.probe, SerialEndpoint("COM8", "cable_a", 1, 2))
        self.snapshot = _snapshot(
            uarts=(self.row_b, _uart_row("COM99", scope="stable", serial="cable_a"))
        )
        before = self.cache.path.read_bytes()
        self.assertIn("COM99", self.public_action("write_serial"))
        self.assertEqual(self.transport.open.call_args.args[0], "COM99")
        self.assertEqual(self.cache.path.read_bytes(), before)

    def test_removal_alone_does_not_create_a_pairing_then_setup_reconfirmation_restores(self):
        self.damage("malformed_json")
        self.refused(lambda: self.public_action("write_serial"), ToolError)
        # Simulate the documented operator action while no call is active.
        backup = self.cache.path.with_suffix(".json.backup")
        self.cache.path.rename(backup)
        self.assertEqual(backup.read_bytes(), DAMAGES["malformed_json"])
        self.snapshot = _snapshot(
            uarts=(
                replace(self.row_b, description="console", vid=1, pid=2),
                _uart_row("COM8", scope="stable", serial="other"),
            )
        )
        with self.assertRaisesRegex(ToolError, "uart/automatic-selection-refused"):
            self.public_action("write_serial")
        self.assertFalse(self.cache.path.exists())
        # Use board_setup's real confirmation callback with its real input types.
        self.server._confirm_setup_cache(
            SetupUserInput("board_a", "probe-choice", "Fixture A", "fixture", 115200),
            PreflightDecision(
                "preflight_ready",
                "preflight/ready",
                "operator explicitly reconfirmed",
                selected_probe=ProbeCandidate("probe-choice", "probe", "jlink", "probe_a"),
                selected_serial=SerialCandidate("uart-choice", "COM7", "console", "cable_b", 1, 2),
                cache_confirmation_required=True,
            ),
        )
        self.transport.reset_mock()
        self.assertIn("COM7", self.public_action("write_serial"))
        self.transport.open.assert_called_once()
        self.assertEqual(self.transport.open.call_args.args[0], "COM7")
        self.assertEqual(backup.read_bytes(), DAMAGES["malformed_json"])
        self.assertEqual(len(self.cache.load_records()), 1)
        self.observe("recovered", backup_preserved=True, confirmed=True, selected="COM7")


def refusal_case(kind, route):
    def test(self):
        self.damage(kind)
        if route == "public_on_exit":
            self.public_finalizer_refusal()
        elif route == "finalizer":
            finalizer = self.server._resolve_operation_finalizer(
                "write_serial", "board_a", {"on_exit": {"action": "uart_write", "text": "exit"}}
            )
            self.assertIsNotNone(finalizer)
            self.refused(finalizer, RuntimeError)
        else:
            self.refused(lambda: self.public_action(route), ToolError)

    return test


for _kind in DAMAGES:
    for _route in ("read_serial", "write_serial", "serial_exchange", "finalizer", "public_on_exit"):
        setattr(ManagedCacheRefusal, f"test_{_kind}_{_route}", refusal_case(_kind, _route))
