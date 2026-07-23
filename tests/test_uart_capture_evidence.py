from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any

from firmware_mcp.adapters.uart_interface import UARTInterface, UARTPortHandle
from firmware_mcp.services.uart_capture import (
    UARTCaptureResult,
    UARTExchangeResult,
    UARTExchangeStepResult,
    exchange_uart_output,
    write_uart_output,
)
from firmware_mcp.tools.serial import SerialToolServices, exchange_serial, read_serial


class _BufferedUART(UARTInterface):
    def __init__(self, incoming: bytes) -> None:
        self.incoming = bytearray(incoming)
        self.writes: list[bytes] = []

    def open(self, device: str, *, baudrate: int, timeout_seconds: float) -> UARTPortHandle:
        transport = SimpleNamespace(timeout=timeout_seconds)
        return UARTPortHandle(transport, device, baudrate, timeout_seconds)

    def close(self, handle: UARTPortHandle) -> None:
        del handle

    def reset_input_buffer(self, handle: UARTPortHandle) -> None:
        del handle

    def read(self, handle: UARTPortHandle, size: int) -> bytes:
        del handle
        chunk = bytes(self.incoming[:size])
        del self.incoming[:size]
        return chunk

    def write(self, handle: UARTPortHandle, data: bytes) -> int:
        del handle
        self.writes.append(data)
        return len(data)


def _services(*, capture: UARTCaptureResult, exchange: UARTExchangeResult) -> SerialToolServices:
    handle = SimpleNamespace(
        board=SimpleNamespace(default_baudrate=115200),
        metadata=SimpleNamespace(route_used="test-route"),
    )
    return SerialToolServices(
        runtime_for=lambda _board_id: None,
        active_session_id=lambda _board_id: None,
        duration_ms=lambda _started: 0,
        record_event=lambda *args, **kwargs: SimpleNamespace(),
        format_invalid=lambda *args, **kwargs: "invalid",
        handle_for=lambda _board_id: handle,
        resolve_port=lambda _handle, **_kwargs: SimpleNamespace(device="COM_TEST"),
        capture_uart=lambda *args, **kwargs: capture,
        write_uart=lambda *args, **kwargs: None,
        exchange_uart=lambda *args, **kwargs: exchange,
        reset_target=lambda _handle: None,
        no_board_config_message="no board",
    )


class UARTCaptureEvidenceTests(unittest.TestCase):
    def test_exchange_capture_is_not_silently_capped_at_65536_bytes(self) -> None:
        payload = (b"x" * 70_000) + b"DONE"
        adapter = _BufferedUART(payload)

        result = exchange_uart_output(
            "COM_TEST",
            115200,
            b"status\n",
            "DONE",
            1.0,
            adapter=adapter,
        )

        self.assertTrue(result.matched)
        self.assertEqual(result.text, payload.decode("ascii"))
        self.assertEqual(result.steps[0].text, payload.decode("ascii"))
        self.assertEqual(result.raw_bytes, payload)
        self.assertEqual(result.steps[0].raw_bytes, payload)
        self.assertGreater(len(result.text), 65_536)

    def test_read_serial_returns_complete_reversibly_serialized_capture(self) -> None:
        captured = 'prefix\\nliteral "quote"\r\n' + ("A" * 400) + "\x00TAIL"
        capture = UARTCaptureResult(captured, "TAIL", 0.25)
        exchange = UARTExchangeResult("", "unused", 0, 0.0, 1)

        result = read_serial(
            _services(capture=capture, exchange=exchange),
            "board",
            0.25,
            "TAIL",
        )

        serialized = json.dumps(captured, ensure_ascii=True)
        self.assertIn(f"captured_text={serialized}", result)
        self.assertNotIn("excerpt=", result)
        self.assertGreater(len(serialized), 300)

    def test_empty_exploratory_capture_is_transport_success_with_lossless_bytes(self) -> None:
        capture = UARTCaptureResult("", None, 0.25, b"")
        exchange = UARTExchangeResult("", "unused", 0, 0.0, 1)

        result = read_serial(
            _services(capture=capture, exchange=exchange),
            "board",
            0.25,
        )

        self.assertIn("UART completed", result)
        self.assertIn("captured_bytes=0", result)
        self.assertIn('captured_hex=""', result)

    def test_partial_uart_write_fails_with_actual_and_expected_counts(self) -> None:
        class PartialUART(_BufferedUART):
            def write(self, handle: UARTPortHandle, data: bytes) -> int:
                super().write(handle, data)
                return len(data) - 1

        with self.assertRaisesRegex(RuntimeError, r"wrote 2 of 3 byte\(s\)"):
            write_uart_output(
                "COM_TEST", 115200, b"abc", timeout_seconds=1.0, adapter=PartialUART(b"")
            )

    def test_serial_exchange_returns_complete_aggregate_and_per_step_text(self) -> None:
        first = 'first\\n"quoted"\r\n' + ("B" * 350)
        second = "second\x00\r\nDONE"
        steps = (
            UARTExchangeStepResult("first", first, 4),
            UARTExchangeStepResult("DONE", second, 4),
        )
        exchange = UARTExchangeResult(
            first + second,
            "first",
            8,
            0.5,
            2,
            steps=steps,
        )
        capture = UARTCaptureResult("", None, 0.0)
        services = _services(capture=capture, exchange=exchange)
        step_args: list[dict[str, Any]] = [
            {"text": "one", "expected_text": "first", "line_ending": "lf"},
            {"text": "two", "expected_text": "DONE", "line_ending": "lf"},
        ]

        result = exchange_serial(services, "board", step_args, 1.0)

        self.assertIn(
            f"captured_text={json.dumps(first + second, ensure_ascii=True)}",
            result,
        )
        self.assertIn(
            f"step_captured_texts={json.dumps([first, second], ensure_ascii=True)}",
            result,
        )
        self.assertIn('captured_hex=""', result)
        self.assertNotIn("excerpt=", result)


if __name__ == "__main__":
    unittest.main()
