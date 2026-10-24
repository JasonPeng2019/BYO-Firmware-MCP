"""VI06: exercise real managed UART resolution, storage, actions and finalizers.

Only discovery inputs, live debug handles and the serial transport are simulated.
The AttachmentCache, inventory gate, scorer, action handlers and finalizers are real.
"""

from __future__ import annotations

import unittest
import inspect
from dataclasses import replace
from unittest.mock import Mock, patch

from pyocd_debug_mcp import discovery_hooks, serial_resolver
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.capabilities.policy import Tier
from pyocd_debug_mcp.firmstore.cache import AttachmentCache, ProbeIdentity, SerialEndpoint
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.hardware_inventory import HardwareInventoryService
from pyocd_debug_mcp.probe_inventory import EMPTY_NATIVE_PROBE_LISTING
from pyocd_debug_mcp.services import uart_capture
from tests.test_probe_selection_records import _snapshot, _uart_row
from tests.tiered_acceptance_support import (
    confirmed_lite_policy,
    isolated_project,
    public_text,
    tiered_test_server,
)

OBSERVATIONS = {}


class ManagedUartOwnership(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(isolated_project())
        self.server = self.enterContext(tiered_test_server(self.root))
        self.cache = self.server._attachment_cache
        self.board = BoardConfig(
            board_id="board_a",
            display_name="Fixture A",
            mcu_family="fixture",
            probe_family="jlink",
            pyocd_target="fixture",
            probe_type="jlink",
            probe_hint_terms=(),
            serial_hint_terms=(),
            test_addr=0x20000000,
        )
        self.probe = ProbeIdentity("jlink", "probe_a")
        self.handle = TargetSessionHandle(None, self.board, "probe_a", "local", None)
        self.server._promote_open_session("board_a", self.handle, gate_reason="fixture")
        self.cable_b = SerialEndpoint("COM7", "cable_b", 1, 2)
        self.row_b = replace(
            _uart_row("COM7", scope="stable", serial="cable_b"),
            description="USB Serial FTDI",
        )
        self.snapshot = _snapshot(uarts=(self.row_b,))
        self.enterContext(
            patch.object(self.server._hardware_inventory, "uart_snapshot", lambda: self.snapshot)
        )
        self.transport = Mock()
        self.transport.open.return_value = object()
        self.transport.read.return_value = b"ok"
        self.transport.read_with_timeout.return_value = b"ok"
        self.transport.write.side_effect = lambda handle, data: len(data)
        self.enterContext(patch.object(uart_capture, "_BACKEND", self.transport))

    def confirm_b(self):
        self.cache.confirm("board_b", ProbeIdentity("jlink", "probe_b"), self.cable_b)
        # Reload from actual disk, so no fixture can smuggle in in-memory ownership.
        self.server._attachment_cache = AttachmentCache(FirmStore(self.root))

    def resolve(self, port=None):
        return self.server._resolve_serial_port_for_session(self.handle, override=port)

    def observe(self, name, **facts):
        OBSERVATIONS[self.id() + ":" + name] = facts

    def assert_refused(self, operation, *, code="uart/automatic-selection-refused"):
        self.transport.reset_mock()
        try:
            with self.assertRaisesRegex(RuntimeError, code) as caught:
                operation()
        finally:
            self.observe("attempt", serial_calls=[str(call) for call in self.transport.mock_calls])
        message = str(caught.exception)
        self.assertIn("board_setup", message)
        self.assertIn("port", message)
        self.assertIn("plan", message)
        self.assertEqual(self.transport.mock_calls, [], "refusal must precede serial open/write")
        self.observe("refusal", message=message, serial_calls=[])

    def action(self, name, *, port=None):
        arguments = {
            "read_serial": {"expected_text": "ok", "read_seconds": 0.1},
            "write_serial": {"text": "hello", "timeout_seconds": 0.1},
            "serial_exchange": {
                "steps": [{"text": "hello", "expected_text": "ok", "line_ending": "none"}],
                "read_seconds": 0.1,
            },
        }[name]
        return self.server.serial_tool_handlers[name]("board_a", port=port, **arguments)

    def public_plan(self, name, *, port=None, on_exit=None):
        # Use the real supported lite route to avoid unrelated silicon-validation
        # preconditions. Neither the plan nor dispatch/ownership guards are mocked.
        if self.server._capability_policies.resolve("board_a").tier is not Tier.SETUP_LITE:
            self.server._capability_policies.commit(
                "board_a",
                Tier.SETUP_LITE,
                map_snapshot=confirmed_lite_policy("board_a"),
                evidence={"confirmed": True},
            )
        parameters = {
            key: field.default
            for key, field in inspect.signature(
                self.server.serial_tool_handlers[name]
            ).parameters.items()
            if key != "board_id"
        }
        parameters["port"] = port
        if name == "read_serial":
            parameters.update(expected_text="ok", read_seconds=0.1, on_exit=on_exit)
        elif name == "write_serial":
            parameters.update(text="hello", timeout_seconds=0.1, on_exit=on_exit)
        else:
            parameters.update(
                steps=[{"text": "hello", "expected_text": "ok", "line_ending": "none"}],
                read_seconds=0.1,
            )
        self.server.plan_engine.null_response(name + "-plan")
        self.server.plan_engine.submit(
            name + "-plan",
            {
                "board_id": "board_a",
                "hypothesis": "The selected UART is the operator's intended adapter.",
                "strategy": "Resolve fresh inventory before any bounded serial I/O.",
                "hypothesis_made": True,
                "strategy_evaluated": True,
                "expected_fail_return": "Typed refusal and zero serial I/O.",
                "expected_success_return": "One bounded UART operation on the planned port.",
                "max_calls": 1,
                "max_calls_buffer": 0,
                "action_parameters": parameters,
            },
            session_id=self.server._active_session_id("board_a"),
        )
        return {"board_id": "board_a", **parameters}

    def test_public_planned_actions_refuse_automatic_owned_choice(self):
        self.confirm_b()
        for name in ("read_serial", "write_serial", "serial_exchange"):
            with self.subTest(action=name):
                args = self.public_plan(name)
                self.transport.reset_mock()
                with self.assertRaisesRegex(
                    Exception, "uart/automatic-selection-refused"
                ) as caught:
                    public_text(self.server, name, args)
                self.assertEqual(self.transport.mock_calls, [])
                self.observe(name, message=str(caught.exception), serial_calls=[])

    def test_public_on_exit_resolves_fresh_and_refuses_owned_choice(self):
        self.confirm_b()
        args = self.public_plan("read_serial", on_exit={"action": "uart_write", "text": "exit"})
        with patch.object(
            self.server._hardware_inventory, "uart_snapshot", return_value=self.snapshot
        ) as inventory:
            with self.assertRaisesRegex(Exception, "uart/automatic-selection-refused") as caught:
                public_text(self.server, "read_serial", args)
            self.assertEqual(inventory.call_count, 2, "main action and finalizer must each resolve")
        self.assertEqual(self.transport.mock_calls, [])
        self.observe("public-on-exit", message=str(caught.exception), serial_calls=[])

    def test_public_explicit_non_usb_override_requires_same_planned_port(self):
        self.snapshot = _snapshot(
            uarts=(replace(_uart_row("uart-socket"), description="ordinary UART"),)
        )
        args = self.public_plan("write_serial", port="uart-socket")
        with self.assertRaisesRegex(Exception, "immutable plan binding") as caught:
            public_text(self.server, "write_serial", {**args, "port": "other-socket"})
        self.assertEqual(caught.exception.__cause__.code, "plan/parameter-mismatch")
        self.assertEqual(self.transport.mock_calls, [])
        args = self.public_plan("write_serial", port="uart-socket")
        result = public_text(self.server, "write_serial", args)
        self.assertIn("uart-socket", result)
        self.transport.open.assert_called_once()
        self.transport.write.assert_called_once()
        self.transport.close.assert_called_once()
        self.assertEqual(self.cache.load_records(), [])

    def test_public_owned_override_does_not_authorize_automatic_finalizer(self):
        self.confirm_b()
        before = self.server._attachment_cache.path.read_bytes()
        args = self.public_plan(
            "write_serial", port="COM7", on_exit={"action": "uart_write", "text": "exit"}
        )
        with patch.object(
            self.server._hardware_inventory, "uart_snapshot", return_value=self.snapshot
        ) as inventory:
            # Finalizers are best effort: its refusal must not undo the completed
            # explicit write, and must not open/write that adapter a second time.
            result = public_text(self.server, "write_serial", args)
            self.assertEqual(inventory.call_count, 2)
        self.assertIn("COM7", result)
        self.transport.open.assert_called_once()
        self.transport.write.assert_called_once()
        self.transport.close.assert_called_once()
        self.assertEqual(self.transport.write.call_args.args[1], b"hello")
        self.assertEqual(self.server._attachment_cache.path.read_bytes(), before)

    def test_actual_resolver_refuses_other_boards_active_singleton(self):
        self.confirm_b()
        before = self.server._attachment_cache.path.read_bytes()
        self.assert_refused(self.resolve)
        self.assertEqual(self.server._attachment_cache.path.read_bytes(), before)

    def test_matching_probe_metadata_cannot_override_active_ownership(self):
        self.confirm_b()
        self.handle = replace(self.handle, probe_uid="cable_b", metadata=None)
        self.assert_refused(self.resolve)

    def test_board_hint_cannot_override_active_ownership(self):
        self.confirm_b()
        self.handle = replace(
            self.handle, board=replace(self.board, serial_hint_terms=("ftdi",)), metadata=None
        )
        self.assert_refused(self.resolve)

    def test_read_serial_refuses_before_open(self):
        self.confirm_b()
        self.assert_refused(lambda: self.action("read_serial"))

    def test_write_serial_refuses_before_open_or_write(self):
        self.confirm_b()
        self.assert_refused(lambda: self.action("write_serial"))

    def test_serial_exchange_refuses_before_open_or_write(self):
        self.confirm_b()
        self.assert_refused(lambda: self.action("serial_exchange"))

    def test_finalizer_entry_refuses_before_open_or_write(self):
        self.confirm_b()
        finalizer = self.server._resolve_operation_finalizer(
            "read_serial", "board_a", {"on_exit": {"action": "uart_write", "text": "exit"}}
        )
        self.assertIsNotNone(finalizer)
        self.assert_refused(finalizer)

    def test_remote_probe_local_uart_actions_and_finalizer_refuse(self):
        self.confirm_b()
        self.handle = replace(
            self.handle, probe_uid="remote:session-a", route_used="remote", metadata=None
        )
        connection = self.server.connection_manager.connection_for("board_a")
        self.server.connection_manager.clear("board_a")
        self.server.connection_manager.assign("board_a", self.handle, connection.runtime_session)
        for name in ("read_serial", "write_serial", "serial_exchange"):
            with self.subTest(route=name):
                self.assert_refused(lambda: self.action(name))
        self.assert_refused(lambda: self.server._finalizer_uart_write("board_a", "exit", 0.1))

    def test_filter_precedes_vendor_helper_and_preserves_eligible_choice(self):
        self.confirm_b()
        unrelated = replace(_uart_row("COM9"), description="no association")
        self.snapshot = _snapshot(uarts=(self.row_b, unrelated))
        spec = serial_resolver.SerialFallbackSpec(
            "fixture", ("jlink",), "fixture-helper", "FIXTURE_HELPER", (), "nrfjprog_com"
        )
        with (
            patch.object(serial_resolver, "SERIAL_FALLBACKS", (spec,)),
            patch.object(serial_resolver, "resolve_command_path", return_value="fixture-helper"),
            patch.object(
                self.server, "_run_cmd", return_value=(0, "probe_a COM7 VCOM0", "")
            ) as run,
        ):
            self.assert_refused(self.resolve)
            run.assert_called_once()
        # A helper is still allowed to resolve an eligible non-USB endpoint.
        with (
            patch.object(serial_resolver, "SERIAL_FALLBACKS", (spec,)),
            patch.object(serial_resolver, "resolve_command_path", return_value="fixture-helper"),
            patch.object(self.server, "_run_cmd", return_value=(0, "probe_a COM9 VCOM0", "")),
        ):
            self.assertEqual(self.resolve().device, "COM9")

    def test_eligible_adapter_remains_when_higher_scoring_owned_adapter_is_filtered(self):
        self.confirm_b()
        free = replace(_uart_row("COM8", scope="stable", serial="free"), description="USB Serial")
        self.snapshot = _snapshot(uarts=(self.row_b, free))
        self.handle = replace(self.handle, probe_uid="cable_b", metadata=None)
        self.assertEqual(self.resolve().device, "COM8")

    def test_owned_stable_key_is_independent_of_path_and_usb_ids_are_part_of_key(self):
        self.confirm_b()
        self.snapshot = _snapshot(uarts=(replace(self.row_b, port_path="COM99"),))
        self.assert_refused(self.resolve)
        self.snapshot = _snapshot(uarts=(replace(self.row_b, vid=3),))
        self.assertEqual(self.resolve().device, "COM7")

    def test_retired_confirmation_does_not_reserve_a_free_adapter(self):
        self.confirm_b()
        self.server._attachment_cache.revoke(
            "board_b", ProbeIdentity("jlink", "probe_b"), self.cable_b
        )
        self.assertEqual(self.resolve().device, "COM7")

    def test_replacement_releases_old_cable_but_reserves_new_one(self):
        self.confirm_b()
        self.server._attachment_cache.confirm(
            "board_b", ProbeIdentity("jlink", "probe_b"), SerialEndpoint("COM8", "new_b", 1, 2)
        )
        self.assertEqual(self.resolve().device, "COM7")
        self.snapshot = _snapshot(
            uarts=(replace(self.row_b, port_path="COM8", usb_serial="new_b"),)
        )
        self.assert_refused(self.resolve)

    def test_unrelated_virtual_singleton_refuses_with_explicit_selection_guidance(self):
        self.snapshot = _snapshot(
            uarts=(replace(_uart_row("virtual-console"), description="Bluetooth endpoint"),)
        )
        self.assert_refused(self.resolve)
        self.assertIsNone(self.server._session_uart_selections.recorded("board_a"))

    def test_unknown_stable_singleton_is_not_itself_a_board_link(self):
        self.snapshot = _snapshot(uarts=(replace(self.row_b, description="unrelated endpoint"),))
        self.assert_refused(self.resolve)

    def test_first_use_bridge_and_exact_probe_match_still_resolve(self):
        self.assertEqual(self.resolve().device, "COM7")
        self.snapshot = _snapshot(
            uarts=(replace(self.row_b, description="console", usb_serial="probe_a"),)
        )
        self.assertEqual(self.resolve().device, "COM7")

    def test_non_usb_board_hint_is_supported_and_session_local(self):
        self.snapshot = _snapshot(
            uarts=(replace(_uart_row("uart-socket"), description="fixture console"),)
        )
        self.handle = replace(
            self.handle,
            board=replace(self.board, serial_hint_terms=("fixture console",)),
            metadata=None,
        )
        self.assertEqual(self.resolve().device, "uart-socket")
        self.assertEqual(self.cache.load_records(), [])
        self.assertIsNotNone(self.server._session_uart_selections.recorded("board_a"))

    def test_explicit_actions_can_use_owned_adapter_without_reconfirming(self):
        self.confirm_b()
        before = self.server._attachment_cache.path.read_bytes()
        for name in ("read_serial", "write_serial", "serial_exchange"):
            with self.subTest(action=name):
                self.transport.reset_mock()
                self.action(name, port="COM7")
                self.transport.open.assert_called_once()
                self.transport.close.assert_called_once()
                self.assertEqual(self.transport.open.call_args.args[0], "COM7")
        self.assertEqual(self.server._attachment_cache.path.read_bytes(), before)
        self.assert_refused(self.resolve)

    def test_explicit_non_usb_selection_is_not_persisted_and_loss_remains_a_refusal(self):
        self.snapshot = _snapshot(
            uarts=(replace(_uart_row("uart-socket"), description="ordinary UART"),)
        )
        self.action("write_serial", port="uart-socket")
        self.transport.write.assert_called_once()
        self.assertEqual(self.cache.load_records(), [])
        self.assertEqual(self.resolve().device, "uart-socket")
        saved = self.server._session_uart_selections.recorded("board_a")
        self.snapshot = _snapshot(uarts=(self.row_b,))
        for _ in range(2):
            self.assert_refused(self.resolve, code="uart/saved-adapter-unavailable")
        self.assertEqual(self.server._session_uart_selections.recorded("board_a"), saved)

    def test_saved_loss_override_repeated_refusal_reappearance_and_renumbering(self):
        self.cache.confirm("board_a", self.probe, SerialEndpoint("COM1", "saved_a", 1, 2))
        self.confirm_b()
        before = self.server._attachment_cache.path.read_bytes()
        for _ in range(2):
            self.assert_refused(self.resolve, code="uart/saved-adapter-unavailable")
        self.action("write_serial", port="COM7")
        self.assertEqual(self.server._attachment_cache.path.read_bytes(), before)
        self.assert_refused(self.resolve, code="uart/saved-adapter-unavailable")
        self.snapshot = _snapshot(
            uarts=(replace(self.row_b, port_path="COM88", usb_serial="saved_a"),)
        )
        self.assertEqual(self.resolve().device, "COM88")

    def test_native_virtual_endpoint_keeps_documented_hook_gate(self):
        port = serial_resolver.SerialPortInfo("virtual-console", "Bluetooth", "", "", "", "")
        hooks = Mock()
        hooks.has_hooks_for.return_value = True
        run_hooks = Mock(side_effect=AssertionError("native singleton must keep hooks gated"))
        service = HardwareInventoryService(
            native_probes=lambda: EMPTY_NATIVE_PROBE_LISTING,
            native_uarts=lambda: [port],
            hook_snapshot=lambda: hooks,
            run_hooks=run_hooks,
        )
        hooks.manifest_sha256 = None
        with patch.object(self.server, "_hardware_inventory", service):
            self.assert_refused(self.resolve)
        run_hooks.assert_not_called()

    def test_zero_native_inventory_still_invokes_uart_hook_gate(self):
        hooks = Mock(wraps=discovery_hooks.EMPTY_SNAPSHOT)
        hooks.has_hooks_for.return_value = True
        hooks.manifest_sha256 = None
        run_hooks = Mock(return_value=())
        service = HardwareInventoryService(
            native_probes=lambda: EMPTY_NATIVE_PROBE_LISTING,
            native_uarts=lambda: [],
            hook_snapshot=lambda: hooks,
            run_hooks=run_hooks,
        )
        with patch.object(self.server, "_hardware_inventory", service):
            with self.assertRaisesRegex(RuntimeError, "No serial ports detected"):
                self.resolve()
        run_hooks.assert_called_once_with(hooks, "uart")
