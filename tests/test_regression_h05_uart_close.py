from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace

from pyocd_debug_mcp.adapters.uart_interface import UARTInterface, UARTPortHandle
from pyocd_debug_mcp.services.uart_capture import (
    capture_uart_output,
    exchange_uart_output,
    write_uart_output,
)
from pyocd_debug_mcp.tools.serial import (
    SerialToolServices,
    serial_exchange,
    write_serial,
)


class _RegressionUART(UARTInterface):
    def __init__(
        self,
        *,
        incoming: tuple[bytes, ...] = (),
        open_error: Exception | None = None,
        read_error: Exception | None = None,
        write_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.incoming = list(incoming)
        self.open_error = open_error
        self.read_error = read_error
        self.write_error = write_error
        self.close_error = close_error
        self.open_count = 0
        self.close_count = 0
        self.writes: list[bytes] = []
        self.active_traceback_at_close = None

    def open(self, device: str, *, baudrate: int, timeout_seconds: float) -> UARTPortHandle:
        self.open_count += 1
        if self.open_error is not None:
            raise self.open_error
        return UARTPortHandle(
            SimpleNamespace(timeout=timeout_seconds), device, baudrate, timeout_seconds
        )

    def close(self, handle: UARTPortHandle) -> None:
        del handle
        self.close_count += 1
        self.active_traceback_at_close = sys.exc_info()[2]
        if self.close_error is not None:
            raise self.close_error

    def reset_input_buffer(self, handle: UARTPortHandle) -> None:
        del handle

    def read(self, handle: UARTPortHandle, size: int) -> bytes:
        del handle, size
        if self.read_error is not None:
            raise self.read_error
        return self.incoming.pop(0) if self.incoming else b""

    def write(self, handle: UARTPortHandle, data: bytes) -> int:
        del handle
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(data)
        return len(data)


def _services(adapter: _RegressionUART) -> SerialToolServices:
    handle = SimpleNamespace(
        board=SimpleNamespace(default_baudrate=57600),
        metadata=SimpleNamespace(route_used="test-route"),
    )
    return SerialToolServices(
        runtime_for=lambda _board: None,
        active_session_id=lambda _board: None,
        duration_ms=lambda _started: 0,
        record_event=lambda *args, **kwargs: None,
        format_refusal=lambda *args, **kwargs: "refused",
        handle_for=lambda _board: handle,
        resolve_port=lambda _handle, **_kwargs: SimpleNamespace(device="test-uart-device"),
        capture_uart=lambda *args, **kwargs: capture_uart_output(*args, adapter=adapter, **kwargs),
        write_uart=lambda *args, **kwargs: write_uart_output(*args, adapter=adapter, **kwargs),
        exchange_uart=lambda *args, **kwargs: exchange_uart_output(
            *args, adapter=adapter, **kwargs
        ),
        reset_target=lambda _handle: None,
        no_board_config_message="no board",
    )


def _contains_traceback(error: BaseException, expected: object) -> bool:
    current = error.__traceback__
    while current is not None:
        if current is expected:
            return True
        current = current.tb_next
    return False


def _capture_exception(invoke: object) -> BaseException:
    assert callable(invoke)
    try:
        invoke()
    except BaseException as exc:
        return exc
    raise AssertionError("expected an exception")


class UARTCloseRegressionTests(unittest.TestCase):
    _DEVICE = "test-uart-device"
    _BAUD = 57600

    def test_open_failure_is_normalized_without_attempting_close(self) -> None:
        for operation, invoke, expected in (
            (
                "read",
                lambda adapter: capture_uart_output(
                    self._DEVICE, self._BAUD, 0.01, None, adapter=adapter
                ),
                "Unable to read test-uart-device at 57600 baud: open sentinel",
            ),
            (
                "write",
                lambda adapter: write_uart_output(
                    self._DEVICE, self._BAUD, b"request", adapter=adapter
                ),
                "Unable to write test-uart-device at 57600 baud: open sentinel",
            ),
            (
                "exchange",
                lambda adapter: exchange_uart_output(
                    self._DEVICE, self._BAUD, b"request", "reply", 0.01, adapter=adapter
                ),
                "Unable to exchange data on test-uart-device at 57600 baud: open sentinel",
            ),
        ):
            with self.subTest(operation=operation):
                raw_open = OSError("open sentinel")
                adapter = _RegressionUART(open_error=raw_open)
                with self.assertRaises(RuntimeError) as raised:
                    invoke(adapter)
                self.assertEqual(str(raised.exception), expected)
                self.assertIs(raised.exception.__cause__, raw_open)
                self.assertEqual(adapter.open_count, 1)
                self.assertEqual(adapter.close_count, 0)

    def test_healthy_helpers_preserve_results_and_exactly_once_cleanup(self) -> None:
        capture_adapter = _RegressionUART(incoming=(b"prefix DONE suffix",))
        capture = capture_uart_output(
            self._DEVICE, self._BAUD, 0.1, "DONE", adapter=capture_adapter
        )
        self.assertEqual(capture.text, "prefix DONE suffix")
        self.assertTrue(capture.matched)
        self.assertEqual((capture_adapter.open_count, capture_adapter.close_count), (1, 1))

        write_adapter = _RegressionUART()
        write = write_uart_output(self._DEVICE, self._BAUD, b"request", adapter=write_adapter)
        self.assertEqual(write.bytes_written, len(b"request"))
        self.assertEqual(write_adapter.writes, [b"request"])
        self.assertEqual((write_adapter.open_count, write_adapter.close_count), (1, 1))

        exchange_adapter = _RegressionUART(incoming=(b"first", b"second"))
        exchange = exchange_uart_output(
            self._DEVICE,
            self._BAUD,
            b"one",
            "first",
            0.1,
            followup_steps=((b"two", "second"),),
            adapter=exchange_adapter,
        )
        self.assertTrue(exchange.matched)
        self.assertEqual(exchange.text, "firstsecond")
        self.assertEqual([step.text for step in exchange.steps], ["first", "second"])
        self.assertEqual(exchange_adapter.writes, [b"one", b"two"])
        self.assertEqual((exchange_adapter.open_count, exchange_adapter.close_count), (1, 1))

    def test_write_and_exchange_delegates_preserve_close_failure(self) -> None:
        for operation, invoke, expected in (
            (
                "write",
                lambda services: write_serial(services, "board", "request"),
                "Unable to close UART after write on test-uart-device at 57600 baud",
            ),
            (
                "exchange",
                lambda services: serial_exchange(
                    services,
                    "board",
                    [{"text": "request", "expected_text": "reply", "line_ending": "none"}],
                    0.01,
                ),
                "Unable to close UART after exchange on test-uart-device at 57600 baud",
            ),
        ):
            with self.subTest(operation=operation):
                raw_close = ConnectionError("close sentinel")
                adapter = _RegressionUART(incoming=(b"reply",), close_error=raw_close)
                with self.assertRaisesRegex(RuntimeError, expected) as raised:
                    invoke(_services(adapter))
                self.assertIs(raised.exception.__cause__, raw_close)
                self.assertEqual((adapter.open_count, adapter.close_count), (1, 1))

    def test_primary_traceback_survives_close_failure(self) -> None:
        raw_primary = ValueError("write sentinel")
        adapter = _RegressionUART(
            write_error=raw_primary,
            close_error=ConnectionError("close sentinel"),
        )

        primary = _capture_exception(
            lambda: write_uart_output(self._DEVICE, self._BAUD, b"request", adapter=adapter)
        )

        self.assertIsInstance(primary, RuntimeError)
        self.assertIs(primary.__cause__, raw_primary)
        self.assertTrue(_contains_traceback(primary, adapter.active_traceback_at_close))
        self.assertEqual((adapter.open_count, adapter.close_count), (1, 1))


if __name__ == "__main__":
    unittest.main()
