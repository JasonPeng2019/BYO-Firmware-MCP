"""VI06: exercise real managed UART resolution, storage, actions and finalizers.

Only discovery inputs, live debug handles and the serial transport are simulated.
The AttachmentCache, inventory gate, scorer, action handlers and finalizers are real.
"""

from __future__ import annotations
from tests.uart_fixture_support import ManagedUartCase

from dataclasses import replace
from unittest.mock import patch

from pyocd_debug_mcp.firmstore.cache import AttachmentCache, ProbeIdentity
from pyocd_debug_mcp.firmstore.store import FirmStore
from tests.inventory_fixture import _snapshot, _uart_row
from tests.tiered_acceptance_support import public_text

OBSERVATIONS = {}


class ManagedUartOwnership(ManagedUartCase):
    def confirm_b(self):
        self.cache.confirm("board_b", ProbeIdentity("jlink", "probe_b"), self.cable_b)
        # Reload from actual disk, so no fixture can smuggle in in-memory ownership.
        self.server._attachment_cache = AttachmentCache(FirmStore(self.root))

    def observe(self, name, **facts):
        OBSERVATIONS[self.id() + ":" + name] = facts

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
