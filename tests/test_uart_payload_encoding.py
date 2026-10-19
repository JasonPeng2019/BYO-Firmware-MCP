"""Focused M18 regressions for exact UART payload bytes through plan validation.

The public MCP plan path validates ``write_serial`` payloads with
``PlanEngine._validate_action_parameters`` and the exchange route validates its
steps with ``validate_serial_exchange_parameters``.  Both judge emptiness on the
encoded payload, so a newline-only or whitespace-only command survives while a
payload that encodes to zero bytes is still refused.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from pyocd_debug_mcp.guardrails.plan_defs import definition_for_action
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine, PlanRefusal
from pyocd_debug_mcp.services.uart_exchange_schema import (
    encode_uart_text,
    validate_serial_exchange_parameters,
)
from pyocd_debug_mcp.tools.serial import SerialToolServices, write_serial


WRITE_SERIAL_FIELDS = ("text", "baudrate", "port", "append_newline", "timeout_seconds", "on_exit")


def write_serial_parameters(text: str, *, append_newline: bool) -> dict[str, object]:
    return {
        "text": text,
        "baudrate": None,
        "port": None,
        "append_newline": append_newline,
        "timeout_seconds": 1.0,
        "on_exit": None,
    }


def exchange_parameters(
    *, text: str, expected_text: str = "OK", line_ending: str = "none"
) -> dict[str, object]:
    return {
        "steps": [
            {"text": text, "expected_text": expected_text, "line_ending": line_ending},
        ],
        "read_seconds": 1.0,
        "baudrate": 115200,
        "port": None,
        "ready_text": None,
        "ready_seconds": 0.0,
        "ready_probe_text": None,
        "ready_probe_line_ending": "none",
        "ready_probe_delay_seconds": 0.0,
        "clear_input": False,
    }


class WriteSerialPayloadPlanTests(unittest.TestCase):
    """The planned write payload keeps every nonempty byte and refuses a truly empty one."""

    def setUp(self) -> None:
        self.definition = definition_for_action("write_serial")

    def assert_accepted(self, text: str, *, append_newline: bool) -> None:
        parameters = write_serial_parameters(text, append_newline=append_newline)
        self.assertEqual(set(parameters), set(WRITE_SERIAL_FIELDS))
        PlanEngine._validate_action_parameters(self.definition, parameters, session_id=None)

    def assert_refused(self, text: str, *, append_newline: bool) -> str:
        parameters = write_serial_parameters(text, append_newline=append_newline)
        with self.assertRaises(PlanRefusal) as raised:
            PlanEngine._validate_action_parameters(self.definition, parameters, session_id=None)
        return raised.exception.message

    def test_lf_only_and_crlf_only_payloads_survive_plan_validation(self) -> None:
        self.assert_accepted("\n", append_newline=False)
        self.assert_accepted("\r\n", append_newline=False)

    def test_whitespace_sensitive_payload_survives_plan_validation(self) -> None:
        self.assert_accepted("   ", append_newline=False)
        self.assert_accepted(" cmd ", append_newline=False)

    def test_empty_text_with_an_appended_newline_is_a_nonempty_payload(self) -> None:
        self.assert_accepted("", append_newline=True)

    def test_a_truly_empty_payload_is_refused(self) -> None:
        message = self.assert_refused("", append_newline=False)
        self.assertIn("at least one byte", message)

    def test_ordinary_command_still_survives(self) -> None:
        self.assert_accepted("status", append_newline=True)


class ExchangePayloadSchemaTests(unittest.TestCase):
    """The exchange route applies the same encoded-payload emptiness rule per step."""

    def test_step_text_with_a_line_ending_is_a_nonempty_payload(self) -> None:
        self.assertIsNone(
            validate_serial_exchange_parameters(exchange_parameters(text="", line_ending="lf"))
        )
        self.assertIsNone(
            validate_serial_exchange_parameters(exchange_parameters(text="", line_ending="crlf"))
        )

    def test_newline_only_crlf_only_and_whitespace_steps_encode_unchanged(self) -> None:
        for text in ("\n", "\r\n", "   ", " cmd "):
            with self.subTest(text=text):
                self.assertIsNone(
                    validate_serial_exchange_parameters(
                        exchange_parameters(text=text, line_ending="none")
                    )
                )

    def test_a_step_that_encodes_to_zero_bytes_is_refused(self) -> None:
        message = validate_serial_exchange_parameters(
            exchange_parameters(text="", line_ending="none")
        )
        self.assertIsNotNone(message)
        self.assertIn("encode an empty payload", message or "")

    def test_expected_text_matching_rule_is_unchanged(self) -> None:
        message = validate_serial_exchange_parameters(
            exchange_parameters(text="status", expected_text="")
        )
        self.assertIsNotNone(message)
        self.assertIn("expected_text must be non-empty text", message or "")


class EncodedPayloadSharingTests(unittest.TestCase):
    """One definition of the encoding fact backs both routes and the write handler."""

    def test_shared_encoder_matches_the_established_line_ending_contract(self) -> None:
        self.assertEqual(encode_uart_text("", "none"), b"")
        self.assertEqual(encode_uart_text("", "lf"), b"\n")
        self.assertEqual(encode_uart_text("", "cr"), b"\r")
        self.assertEqual(encode_uart_text("", "crlf"), b"\r\n")
        self.assertEqual(encode_uart_text("cmd", "crlf"), b"cmd\r\n")

    def test_shared_encoder_rejects_an_unknown_line_ending(self) -> None:
        with self.assertRaises(ValueError):
            encode_uart_text("cmd", "bogus")


class WriteSerialByteBoundaryTests(unittest.TestCase):
    """Record the exact bytes the handler hands to the transport, with no hidden normalization."""

    def build(self) -> tuple[SerialToolServices, list[bytes]]:
        written: list[bytes] = []
        handle = SimpleNamespace(
            board=SimpleNamespace(default_baudrate=115200),
            metadata=SimpleNamespace(route_used="test-route"),
        )
        services = SerialToolServices(
            runtime_for=lambda _board_id: None,
            active_session_id=lambda _board_id: None,
            duration_ms=lambda _started: 0,
            record_event=lambda *args, **kwargs: SimpleNamespace(),
            format_refusal=lambda *args, **kwargs: "refused",
            handle_for=lambda _board_id: handle,
            resolve_port=lambda _handle, **_kwargs: SimpleNamespace(device="COM_TEST"),
            capture_uart=lambda *args, **kwargs: None,
            write_uart=lambda _device, _baud, payload, **_kwargs: (
                written.append(payload)
                or SimpleNamespace(bytes_written=len(payload), duration_seconds=0.0)
            ),
            exchange_uart=lambda *args, **kwargs: None,
            reset_target=lambda _handle: None,
            no_board_config_message="no board",
        )
        return services, written

    def test_lf_only_crlf_only_and_whitespace_payloads_reach_the_port_unchanged(self) -> None:
        for text, expected in (("\n", b"\n"), ("\r\n", b"\r\n"), ("  ", b"  ")):
            with self.subTest(text=text):
                services, written = self.build()
                write_serial(services, "board", text)
                self.assertEqual(written, [expected])

    def test_append_newline_adds_exactly_one_lf_byte(self) -> None:
        services, written = self.build()
        write_serial(services, "board", "", append_newline=True)
        self.assertEqual(written, [b"\n"])

    def test_no_payload_byte_is_stripped_from_an_ordinary_command(self) -> None:
        services, written = self.build()
        write_serial(services, "board", " cmd ")
        self.assertEqual(written, [b" cmd "])
