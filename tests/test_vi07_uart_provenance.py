"""VI07: actual managed UART routing must reassess inferred session choices."""

from __future__ import annotations
from tests.uart_fixture_support import bridge

from dataclasses import replace

from tests.uart_fixture_support import ManagedUartCase
from tests.inventory_fixture import _snapshot
from tests.tiered_acceptance_support import public_text

OBSERVATIONS = {}


class ManagedUartProvenance(ManagedUartCase):
    def observe(self, name, **facts):
        OBSERVATIONS[self.id() + ":" + name] = facts

    def rows(self, *rows):
        self.snapshot = replace(_snapshot(uarts=rows), snapshot_id=f"snapshot-{id(rows)}")

    def refused(self, operation, code="uart/automatic-selection-refused"):
        self.transport.reset_mock()
        try:
            with self.assertRaisesRegex(Exception, code) as caught:
                operation()
        finally:
            self.observe("last-attempt", serial_calls=[str(c) for c in self.transport.mock_calls])
        self.assertEqual(self.transport.mock_calls, [], "refusal must precede any serial I/O")
        self.observe("refusal", message=str(caught.exception), serial_calls=[])

    def public_action(self, name, **kwargs):
        args = self.public_plan(name, **kwargs)
        return public_text(self.server, name, args)

    def sequence(self, name):
        a, b = bridge("COM41"), bridge("COM42")
        self.rows(a, b)
        self.refused(lambda: self.public_action(name))
        self.rows(b)
        self.transport.reset_mock()
        self.public_action(name)
        self.transport.open.assert_called_once()
        self.assertEqual(self.transport.open.call_args.args[0], "COM42")
        self.transport.close.assert_called_once()
        self.observe(
            "singleton", selected="COM42", serial_calls=[str(c) for c in self.transport.mock_calls]
        )
        for rows in ((a, b), (b, a), (a, b)):
            self.rows(*rows)
            self.refused(lambda: self.public_action(name))
        self.assertFalse(self.server._session_uart_selections.recorded("board_a").explicit)
        self.assertEqual(self.cache.load_records(), [])
        self.assertFalse(self.cache.path.exists(), "inference must not create AttachmentCache")

    def test_two_one_two_public_read(self):
        self.sequence("read_serial")

    def test_explicit_loss_refuses_repeatedly_until_reselection(self):
        self.rows(bridge("COM41"), bridge("COM42"))
        self.public_action("write_serial", port="COM41")
        selected = self.server._session_uart_selections.recorded("board_a")
        self.rows(bridge("COM42"))
        for _ in range(2):
            self.refused(
                lambda: self.public_action("write_serial"), "uart/saved-adapter-unavailable"
            )
        self.assertEqual(self.server._session_uart_selections.recorded("board_a"), selected)
        self.public_action("write_serial", port="COM42")
        self.assertEqual(self.resolve().device, "COM42")
