from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any

from pyocd_debug_mcp.discovery_failures import UART_OPEN_FAILED
from pyocd_debug_mcp.kernel.operations import OperationCancelledError
from pyocd_debug_mcp.services.uart_capture import (
    UARTCaptureResult,
    UARTExchangeResult,
    UARTExchangeStepResult,
)
from pyocd_debug_mcp.tools.serial import (
    SerialToolServices,
    read_serial,
    serial_exchange,
    write_serial,
)

import pytest


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
        format_refusal=lambda *args, **kwargs: "refused",
        handle_for=lambda _board_id: handle,
        resolve_port=lambda _handle, **_kwargs: SimpleNamespace(device="COM_TEST"),
        capture_uart=lambda *args, **kwargs: capture,
        write_uart=lambda *args, **kwargs: None,
        exchange_uart=lambda *args, **kwargs: exchange,
        reset_target=lambda _handle: None,
        no_board_config_message="no board",
    )


class UARTCaptureEvidenceTests(unittest.TestCase):
    @pytest.mark.mvp
    def test_read_serial_returns_complete_reversibly_serialized_capture(self) -> None:
        captured = 'prefix\\nliteral "quote"\r\n' + ("A" * 400) + "\x00TAIL"
        capture = UARTCaptureResult(captured, "TAIL", 0, 0.25)
        exchange = UARTExchangeResult("", "unused", 0, 0.0, 1)

        result = read_serial(_services(capture=capture, exchange=exchange), "board", "TAIL")

        serialized = json.dumps(captured, ensure_ascii=True)
        self.assertIn(f"captured_text={serialized}", result)
        self.assertNotIn("excerpt=", result)
        self.assertGreater(len(serialized), 300)

    @pytest.mark.mvp
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
        capture = UARTCaptureResult("", None, 0, 0.0)
        services = _services(capture=capture, exchange=exchange)
        step_args: list[dict[str, Any]] = [
            {"text": "one", "expected_text": "first", "line_ending": "lf"},
            {"text": "two", "expected_text": "DONE", "line_ending": "lf"},
        ]

        result = serial_exchange(services, "board", step_args, 1.0)

        self.assertIn(
            f"captured_text={json.dumps(first + second, ensure_ascii=True)}",
            result,
        )
        self.assertIn(
            f"step_captured_texts={json.dumps([first, second], ensure_ascii=True)}",
            result,
        )
        self.assertNotIn("excerpt=", result)


def _services_for_failure(
    *,
    write_uart: Any = None,
    capture_uart: Any = None,
    exchange_uart: Any = None,
) -> SerialToolServices:
    """A `SerialToolServices` whose non-relevant UART calls assert if reached."""

    handle = SimpleNamespace(
        board=SimpleNamespace(default_baudrate=115200),
        metadata=SimpleNamespace(route_used="test-route"),
    )

    def _unused(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("not exercised by this test")

    return SerialToolServices(
        runtime_for=lambda _board_id: None,
        active_session_id=lambda _board_id: None,
        duration_ms=lambda _started: 0,
        record_event=lambda *args, **kwargs: SimpleNamespace(),
        format_refusal=lambda *args, **kwargs: "refused",
        handle_for=lambda _board_id: handle,
        resolve_port=lambda _handle, **_kwargs: SimpleNamespace(device="COM_TEST"),
        capture_uart=capture_uart or _unused,
        write_uart=write_uart or _unused,
        exchange_uart=exchange_uart or _unused,
        reset_target=lambda _handle: None,
        no_board_config_message="no board",
    )


class UartOpenFailureTests(unittest.TestCase):
    """D27: a genuine port I/O failure must surface the `uart/open-failed` payload,
    while cooperative cancellation (which the operation timeout machinery relies on
    catching by type) must keep propagating unchanged, not be relabeled as one.
    """

    @pytest.mark.mvp
    def test_write_serial_reports_a_genuine_port_failure_as_uart_open_failed(self) -> None:
        # Mirrors what `write_uart_output` itself actually raises: a plain
        # RuntimeError wrapping the real serial backend error.
        def _write_uart(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(
                "Unable to write COM_TEST at 115200 baud: [Errno 13] Permission denied"
            )

        services = _services_for_failure(write_uart=_write_uart)

        with self.assertRaises(RuntimeError) as ctx:
            write_serial(services, "board", "hello")

        message = str(ctx.exception)
        self.assertIn(UART_OPEN_FAILED, message)
        self.assertIn("Permission denied", message)
        self.assertIn("no other process holds the serial port open", message)
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)

    def test_a_cancelled_write_is_never_relabeled_as_an_open_failure(self) -> None:
        """Cooperative cancellation must keep its identity, not become open-failed.

        This fixture raises a `RuntimeError` whose `__cause__` is an
        `OperationCancelledError`. Since the M9 fix that is no longer the shape
        `uart_capture` produces -- it re-raises the cancellation unchanged, which is
        what `UartCapturePreservesCancellationTests` covers. This test remains as the
        defence for any *other* layer that wraps a cancellation on its way here: the
        `uart/open-failed` relabel must not fire on a wrapped cancellation either.
        """

        def _write_uart(*_args: Any, **_kwargs: Any) -> Any:
            try:
                raise OperationCancelledError("operation cancelled")
            except OperationCancelledError as exc:
                raise RuntimeError(
                    "Unable to write COM_TEST at 115200 baud: operation cancelled"
                ) from exc

        services = _services_for_failure(write_uart=_write_uart)

        with self.assertRaises(RuntimeError) as ctx:
            write_serial(services, "board", "hello")

        # Not relabeled: the uart/open-failed code and its remedies never appear,
        # and the original cancellation is still reachable as the cause.
        self.assertNotIn(UART_OPEN_FAILED, str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, OperationCancelledError)


if __name__ == "__main__":
    unittest.main()
