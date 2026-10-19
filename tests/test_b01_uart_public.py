"""Independent M18: public plans execute against the production pySerial service.

Only physical port discovery/open is replaced. loop:// actually buffers bytes;
the receive side, rather than write counts, is the byte oracle. This proves host
transport behavior, not a physical UART or firmware protocol.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import serial

from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.hardware_inventory import UartRow
from pyocd_debug_mcp.tools.raw import MAX_UART_TEXT_BYTES
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    confirmed_lite_policy,
    isolated_project,
    public_call,
    public_text,
    tiered_test_server,
)


BOARD = "b01_uart"
NULL_PLAN = dict.fromkeys(
    (
        "board_id",
        "hypothesis",
        "strategy",
        "hypothesis_made",
        "strategy_evaluated",
        "expected_fail_return",
        "expected_success_return",
        "max_calls",
        "max_calls_buffer",
        "action_parameters",
        "user_permission",
    )
)


def plan(server, action, parameters, **overrides):
    public_text(server, action + "-plan", NULL_PLAN)
    return public_text(
        server,
        action + "-plan",
        {
            "board_id": BOARD,
            "hypothesis": "  The loopback will return the exact command bytes.  ",
            "strategy": "  Send one command and observe its receive buffer.  ",
            "hypothesis_made": True,
            "strategy_evaluated": True,
            "expected_fail_return": "A refusal before transport open.",
            "expected_success_return": "Exact command bytes reach the loopback.",
            "max_calls": 1,
            "max_calls_buffer": 0,
            "action_parameters": parameters,
            **overrides,
        },
    )


def write_parameters(text, append=False):
    return {
        "text": text,
        "append_newline": append,
        "port": "COM_B01",
        "baudrate": 115200,
        "timeout_seconds": 1.0,
        "on_exit": None,
    }


def exchange_parameters(text, ending):
    expected = text + {"none": "", "lf": "\n", "crlf": "\r\n"}[ending]
    return {
        "steps": [{"text": text, "expected_text": expected, "line_ending": ending}],
        "port": "COM_B01",
        "baudrate": 115200,
        "read_seconds": 0.1,
        "ready_text": None,
        "ready_seconds": 0.0,
        "ready_probe_text": None,
        "ready_probe_line_ending": "none",
        "ready_probe_delay_seconds": 0.0,
        "clear_input": False,
    }


class UARTPublicTests(unittest.TestCase):
    def fixture(self):
        project = isolated_project()
        root = project.__enter__()
        self.addCleanup(project.__exit__, None, None, None)
        context = tiered_test_server(root)
        server = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        policies = CapabilityPolicyRepository(root)
        policies.commit(
            BOARD,
            Tier.SETUP_LITE,
            map_snapshot=confirmed_lite_policy(BOARD),
            profile_snapshot={"serial_baudrate": 115200},
        )
        backend = DeterministicTargetBackend()
        server.configure_tiered_test_seams(backend=backend, policy_repository=policies)
        board = BoardConfig(
            BOARD, "B01 UART", "controlled", "controlled", "cortex_m", "controlled", (), (), None
        )
        handle = backend.open(board=board, unique_id="b01-uart", target="cortex_m")
        server._promote_open_session(BOARD, handle, gate_reason="B01 isolated UART fixture")
        self.addCleanup(public_text, server, "disconnect", {"board_id": BOARD})
        snapshot = SimpleNamespace(
            native_uart_available=True,
            uarts=(
                UartRow(
                    "COM_B01",
                    "B01 loopback",
                    "b01-uart",
                    0x1234,
                    1,
                    ("native",),
                    "stable",
                    "b01-uart-row",
                    "b01-snapshot",
                ),
            ),
        )
        resolver = patch.object(server._hardware_inventory, "uart_snapshot", return_value=snapshot)
        resolver.start()
        self.addCleanup(resolver.stop)
        received = []

        def open_loop(device, *, baudrate, timeout, write_timeout):
            self.assertEqual(device, "COM_B01")
            loop = serial.serial_for_url(
                "loop://",
                baudrate=baudrate,
                timeout=timeout,
                write_timeout=write_timeout,
                do_not_open=True,
            )
            # Larger than the raw public cap so this host control does not test
            # loop:// queue capacity or simulated baud timeout instead of size admission.
            loop.buffer_size = 2 * MAX_UART_TEXT_BYTES
            loop.open()
            observation = {"device": device, "baudrate": baudrate, "received": bytearray()}
            received.append(observation)
            original_read, original_close = loop.read, loop.close

            def receive(size=1):
                data = original_read(size)
                observation["received"].extend(data)
                return data

            def close():
                if loop.is_open:
                    observation["received"].extend(original_read(loop.in_waiting))
                    evidence = os.environ.get("BYO_MANAGED_TEST_EVIDENCE")
                    if evidence:
                        output = Path(evidence).parent / "b01-uart-received.jsonl"
                        output.parent.mkdir(parents=True, exist_ok=True)
                        with output.open("a", encoding="utf-8") as stream:
                            stream.write(
                                json.dumps(
                                    {
                                        "test": self.id(),
                                        "device": device,
                                        "baudrate": baudrate,
                                        "received_hex": observation["received"].hex(),
                                    }
                                )
                                + "\n"
                            )
                original_close()

            loop.read, loop.close = receive, close
            return loop

        seam = patch.object(serial, "Serial", side_effect=open_loop)
        seam.start()
        self.addCleanup(seam.stop)
        return server, received

    def execute(self, server, accepted):
        body = json.loads(accepted)
        self.assertEqual(body["status"], "plan_accepted", accepted)
        fallback = body["stable_client_fallback"]
        result = json.loads(
            public_text(server, fallback["tool_name"], fallback["arguments"]).split("\n", 1)[0]
        )
        self.assertEqual(result["status"], "batch_completed", result)
        self.assertEqual(len(result["completed"]), 1)
        return result

    def test_public_write_plan_execute_receives_exact_bytes(self):
        server, received = self.fixture()
        for text, append, expected in (
            ("\n", False, b"\n"),
            ("\r\n", False, b"\r\n"),
            ("   ", False, b"   "),
            (" \t cmd \t ", False, b" \t cmd \t "),
            ("status", False, b"status"),
            ("status", True, b"status\n"),
            ("", True, b"\n"),
            ("caf\u00e9", False, b"caf\xc3\xa9"),
        ):
            with self.subTest(text=text, append=append):
                before = len(received)
                self.execute(server, plan(server, "write_serial", write_parameters(text, append)))
                self.assertEqual(len(received), before + 1)
                self.assertEqual(bytes(received[-1]["received"]), expected)
                self.assertEqual(received[-1]["baudrate"], 115200)

    def test_public_normal_command_control(self):
        server, received = self.fixture()
        self.execute(server, plan(server, "write_serial", write_parameters("status", True)))
        self.assertEqual(bytes(received[0]["received"]), b"status\n")

    def test_public_exchange_receives_exact_bytes_including_empty_text_with_ending(self):
        server, received = self.fixture()
        for text, ending, expected in (
            ("\n", "none", b"\n"),
            ("\r\n", "none", b"\r\n"),
            ("   ", "none", b"   "),
            (" cmd ", "none", b" cmd "),
            ("status", "crlf", b"status\r\n"),
            ("", "lf", b"\n"),
            ("", "crlf", b"\r\n"),
        ):
            with self.subTest(text=text, ending=ending):
                result = self.execute(
                    server, plan(server, "serial_exchange", exchange_parameters(text, ending))
                )
                self.assertEqual(bytes(received[-1]["received"]), expected)
                self.assertIn("exchange matched", json.dumps(result))

    def test_true_empty_and_invalid_transport_plan_fields_never_open_port(self):
        server, received = self.fixture()
        cases = [write_parameters("")]
        for key, value in (("baudrate", 0), ("port", "   "), ("timeout_seconds", 0)):
            cases.append({**write_parameters("hello"), key: value})
        for parameters in cases:
            with self.subTest(parameters=parameters), self.assertRaises(Exception):
                plan(server, "write_serial", parameters)
            self.assertEqual(received, [])
        empty_exchange = exchange_parameters("", "none")
        empty_exchange["steps"][0]["expected_text"] = "OK"
        with self.assertRaises(Exception):
            plan(server, "serial_exchange", empty_exchange)
        self.assertEqual(received, [])

    def test_plan_binding_budget_session_and_prose_rules_remain_effective(self):
        server, received = self.fixture()
        params = write_parameters(" cmd ")
        self.execute(server, plan(server, "write_serial", params))
        # The one-call plan is consumed.
        with self.assertRaisesRegex(Exception, "locked"):
            public_text(
                server,
                "write_serial",
                {"board_id": BOARD, **{k: v for k, v in params.items() if k != "on_exit"}},
            )
        # Each changed argument is tried against a fresh, still unused plan.
        for changes in ({"text": "cmd"}, {"port": "COM_OTHER"}, {"baudrate": 9600}):
            with self.subTest(changes=changes):
                accepted = json.loads(plan(server, "write_serial", params))
                child = accepted["stable_client_fallback"]["arguments"]["actions"][0]
                with self.assertRaisesRegex(
                    Exception, "parameters differ from the immutable plan binding"
                ):
                    public_text(server, child["tool_name"], {**child["arguments"], **changes})
                self.assertEqual(len(received), 1)
        for prose in ("   ", "  TODO  "):
            with self.subTest(prose=prose), self.assertRaises(Exception):
                plan(server, "write_serial", params, hypothesis=prose)
        # A new accepted plan cannot survive explicit disconnection/reassignment.
        plan(server, "write_serial", params)
        public_text(server, "disconnect", {"board_id": BOARD})
        try:
            public_text(
                server,
                "write_serial",
                {"board_id": BOARD, **{k: v for k, v in params.items() if k != "on_exit"}},
            )
        except Exception:
            pass
        self.assertEqual(len(received), 1)

    def test_existing_raw_utf8_size_boundary_is_retained(self):
        server, received = self.fixture()
        too_large = public_call(
            server,
            "write_serial_raw",
            {
                "board_id": BOARD,
                "port": "COM_B01",
                "baudrate": 115200,
                "text": "\u00e9" * (MAX_UART_TEXT_BYTES // 2 + 1),
            },
        )
        self.assertEqual(too_large["status"], "error")
        self.assertEqual(received, [])
        exact = public_call(
            server,
            "write_serial_raw",
            {
                "board_id": BOARD,
                "port": "COM_B01",
                "baudrate": 1000000,
                "text": "x" * MAX_UART_TEXT_BYTES,
            },
        )
        self.assertEqual(exact["status"], "ok", exact)
        self.assertEqual(bytes(received[-1]["received"]), b"x" * MAX_UART_TEXT_BYTES)


if __name__ == "__main__":
    unittest.main()
