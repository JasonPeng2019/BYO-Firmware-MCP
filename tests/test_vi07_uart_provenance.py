"""VI07: actual managed UART routing must reassess inferred session choices."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from dataclasses import replace

from pyocd_debug_mcp.firmstore.cache import SerialEndpoint
from pyocd_debug_mcp.hardware_inventory import SessionUartSelection, SessionUartSelectionStore
from tests import test_vi06_uart_ownership as vi06
from tests.test_probe_selection_records import _snapshot, _uart_row
from tests.tiered_acceptance_support import public_text

OBSERVATIONS = {}


def bridge(path):
    # Two indistinguishable CH340-style adapters: no USB serial, same metadata.
    return replace(_uart_row(path), description="USB Serial CH340", vid=0x1A86, pid=0x7523)


class ManagedUartProvenance(unittest.TestCase):
    setUp = vi06.ManagedUartOwnership.setUp
    resolve = vi06.ManagedUartOwnership.resolve
    action = vi06.ManagedUartOwnership.action
    public_plan = vi06.ManagedUartOwnership.public_plan

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

    def test_two_one_two_public_write(self):
        self.sequence("write_serial")

    def test_two_one_two_public_exchange(self):
        self.sequence("serial_exchange")

    def test_returned_ambiguity_refuses_automatic_finalizer(self):
        self.rows(bridge("COM42"))
        self.action("write_serial")
        self.rows(bridge("COM41"), bridge("COM42"))
        self.refused(lambda: self.server._finalizer_uart_write("board_a", "exit", 0.1))

    def test_inference_is_reassessed_on_repeated_singleton_snapshots(self):
        for _ in range(3):
            self.rows(bridge("COM42"))
            self.assertEqual(self.resolve().device, "COM42")
        selected = self.server._session_uart_selections.recorded("board_a")
        self.assertFalse(selected.explicit)
        self.rows(bridge("COM41"), bridge("COM42"))
        self.refused(self.resolve)

    def test_same_name_reused_path_cannot_supply_inference_evidence(self):
        self.rows(bridge("COM42"))
        self.assertEqual(self.resolve().device, "COM42")
        self.rows(replace(_uart_row("COM42"), description="Bluetooth endpoint"))
        self.refused(lambda: self.action("write_serial"))
        self.rows(_uart_row("COM42", scope="stable", serial="different-adapter"))
        self.refused(lambda: self.action("write_serial"))

    def test_inferred_disappearance_allows_fresh_legitimate_first_use(self):
        self.rows(bridge("COM42"))
        self.assertEqual(self.resolve().device, "COM42")
        self.rows(bridge("COM43"))
        self.assertEqual(self.resolve().device, "COM43")
        self.assertEqual(self.cache.load_records(), [])

    def test_explicit_choice_remains_sticky_when_indistinguishable_peer_returns(self):
        self.rows(bridge("COM41"), bridge("COM42"))
        self.public_action("write_serial", port="COM41")
        selected = self.server._session_uart_selections.recorded("board_a")
        self.assertTrue(selected.explicit)
        for _ in range(3):
            self.rows(bridge("COM42"), bridge("COM41"))
            self.transport.reset_mock()
            self.public_action("write_serial")
            self.transport.open.assert_called_once()
            self.assertEqual(self.transport.open.call_args.args[0], "COM41")
        self.assertEqual(self.cache.load_records(), [])

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

    def test_explicit_session_identity_is_normalized_path_and_session_scope(self):
        self.rows(bridge("COM41"))
        self.resolve(port=r"\\.\COM41")
        # No physical identity is claimed: same path in the same run is the
        # documented session identity, even if its descriptive label changes.
        self.rows(replace(bridge("COM41"), description="renamed session UART"))
        self.assertEqual(self.resolve().device, "COM41")
        self.rows(bridge("COM99"))
        self.refused(self.resolve, "uart/saved-adapter-unavailable")
        self.rows(_uart_row("COM41", scope="stable", serial="new-device"))
        self.refused(self.resolve, "uart/saved-adapter-unavailable")

    def test_current_probe_link_can_replace_inference_without_persistence(self):
        self.rows(bridge("COM42"))
        self.resolve()
        matched = replace(
            _uart_row("COM43", scope="stable", serial="probe_a"), description="console"
        )
        self.rows(bridge("COM42"), matched)
        self.assertEqual(self.resolve().device, "COM43")
        self.assertEqual(self.cache.load_records(), [])

    def test_current_ownership_still_excludes_a_previously_inferred_adapter(self):
        row = replace(bridge("COM42"), identity_scope="stable", usb_serial="later-confirmed")
        self.rows(bridge("COM42"))
        self.resolve()
        self.cache.confirm(
            "board_b",
            vi06.ProbeIdentity("jlink", "probe_b"),
            SerialEndpoint("COM42", "later-confirmed", row.vid, row.pid),
        )
        self.rows(row)
        before = self.cache.path.read_bytes()
        self.refused(lambda: self.action("write_serial"))
        self.assertEqual(self.cache.path.read_bytes(), before)

    def test_confirmed_stable_identity_renumbers_over_an_inference(self):
        self.rows(bridge("COM42"))
        self.resolve()
        self.cache.confirm("board_a", self.probe, SerialEndpoint("COM1", "confirmed-a", 1, 2))
        before = self.cache.path.read_bytes()
        self.rows(bridge("COM42"), _uart_row("COM99", scope="stable", serial="confirmed-a"))
        self.assertEqual(self.resolve().device, "COM99")
        self.assertEqual(self.cache.path.read_bytes(), before)
        self.rows(bridge("COM42"))
        self.refused(self.resolve, "uart/saved-adapter-unavailable")

    def test_independent_boards_do_not_inherit_explicit_or_inferred_choices(self):
        self.rows(bridge("COM42"))
        self.resolve()
        self.resolve(port="COM42")
        other = replace(self.handle, board=replace(self.board, board_id="board_b"), metadata=None)
        self.rows(bridge("COM41"), bridge("COM42"))
        self.refused(lambda: self.server._resolve_serial_port_for_session(other, override=None))
        self.assertEqual(self.resolve().device, "COM42")
        self.assertIsNone(self.server._session_uart_selections.recorded("board_b"))

    def test_board_hints_remain_run_local_and_are_reassessed(self):
        self.handle = replace(
            self.handle,
            board=replace(self.board, serial_hint_terms=("fixture console",)),
            metadata=None,
        )
        a = replace(_uart_row("socket-a"), description="fixture console")
        b = replace(_uart_row("socket-b"), description="fixture console")
        self.rows(a)
        self.assertEqual(self.resolve().device, "socket-a")
        self.assertIsNotNone(self.server._session_uart_selections.recorded("board_a"))
        self.rows(a, b)
        self.refused(self.resolve)
        self.assertEqual(self.cache.load_records(), [])

    def test_new_process_does_not_inherit_inferred_or_explicit_session_state(self):
        # Run the actual resolver in another interpreter on this same FirmStore.
        child = """
import json, sys
from pathlib import Path
from unittest.mock import Mock, patch
from tests.test_probe_selection_records import _snapshot
from tests.test_vi07_uart_provenance import bridge
from tests.tiered_acceptance_support import tiered_test_server
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.services import uart_capture
with tiered_test_server(Path(sys.argv[1])) as server:
    board = BoardConfig(board_id="board_a", display_name="A", mcu_family="fixture", probe_family="jlink", pyocd_target="fixture", probe_type="jlink", probe_hint_terms=(), serial_hint_terms=(), test_addr=0x20000000)
    handle = TargetSessionHandle(None, board, "probe_a", "local", None)
    server._promote_open_session("board_a", handle, gate_reason="fixture")
    transport = Mock()
    with patch.object(server._hardware_inventory, "uart_snapshot", return_value=_snapshot(uarts=(bridge("COM41"), bridge("COM42")))), patch.object(uart_capture, "_BACKEND", transport):
        try:
            server.serial_tool_handlers["write_serial"]("board_a", text="hello", timeout_seconds=0.1)
        except RuntimeError as error:
            assert "uart/automatic-selection-refused" in str(error), error
        else:
            raise AssertionError("restart reused a session choice")
        assert not transport.mock_calls, transport.mock_calls
        assert server._session_uart_selections.recorded("board_a") is None
        assert not server._attachment_cache.load_records()
    print(json.dumps({"refused": True, "serial_calls": [], "records": []}))
"""
        self.rows(bridge("COM42"))
        for explicit in (False, True):
            self.resolve(port="COM42" if explicit else None)
            result = subprocess.run(
                [sys.executable, "-B", "-c", child, str(self.root)],
                capture_output=True,
                text=True,
                timeout=45,
            )
            self.observe(
                f"restart-explicit-{explicit}",
                exit=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout.splitlines()[-1])["refused"])
        self.assertFalse(self.cache.path.exists())


class SelectionStoreProvenance(unittest.TestCase):
    def test_inferred_observation_is_visible_but_cannot_resolve_as_authority(self):
        store = SessionUartSelectionStore()
        row = bridge("COM42")
        guess = SessionUartSelection(
            "board_a", row.port_path, row.description, row.provenance, explicit=False
        )
        store.record(guess)
        self.assertEqual(store.recorded("board_a"), guess)
        self.assertIsNone(store.resolve("board_a", _snapshot(uarts=(row,))))

    def test_inference_cannot_overwrite_explicit_choice(self):
        store = SessionUartSelectionStore()
        explicit = SessionUartSelection("board_a", "COM41", "UART", ("native",))
        store.record(explicit)
        store.record(SessionUartSelection("board_a", "COM42", "UART", ("native",), explicit=False))
        self.assertEqual(store.recorded("board_a"), explicit)
        store.record(SessionUartSelection("board_b", "COM42", "UART", ("native",), explicit=False))
        self.assertFalse(store.recorded("board_b").explicit)

    def test_inferred_missing_choice_does_not_block_fresh_scoring(self):
        store = SessionUartSelectionStore()
        store.record(SessionUartSelection("board_a", "COM42", "UART", ("native",), explicit=False))
        self.assertIsNone(store.resolve("board_a", _snapshot()))
        store.clear_board("board_a")
        self.assertIsNone(store.recorded("board_a"))
