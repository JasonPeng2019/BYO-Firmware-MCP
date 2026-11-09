"""Shared managed UART setup; no collected cases."""

from __future__ import annotations
from tests.inventory_fixture import _uart_row
import unittest
import inspect
from dataclasses import replace
from unittest.mock import Mock, patch
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.capabilities.policy import Tier
from pyocd_debug_mcp.firmstore.cache import ProbeIdentity, SerialEndpoint
from pyocd_debug_mcp.services import uart_capture
from tests.inventory_fixture import _snapshot, _uart_row
from tests.tiered_acceptance_support import (
    confirmed_lite_policy,
    isolated_project,
    tiered_test_server,
)


class ManagedUartCase(unittest.TestCase):
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

    def resolve(self, port=None):
        return self.server._resolve_serial_port_for_session(self.handle, override=port)

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


def bridge(path):
    # Two indistinguishable CH340-style adapters: no USB serial, same metadata.
    return replace(_uart_row(path), description="USB Serial CH340", vid=0x1A86, pid=0x7523)
