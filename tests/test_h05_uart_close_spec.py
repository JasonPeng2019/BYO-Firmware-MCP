from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from typing import Callable

from pyocd_debug_mcp.adapters.uart_interface import UARTInterface, UARTPortHandle
from pyocd_debug_mcp.kernel.operations import OperationCancelledError
from pyocd_debug_mcp.services.uart_capture import (
    capture_uart_output,
    exchange_uart_output,
    write_uart_output,
)
from pyocd_debug_mcp.tools.serial import SerialToolServices, read_serial


class _SentinelUART(UARTInterface):
    def __init__(
        self,
        *,
        close_error: Exception | None = None,
        body_error: Exception | None = None,
        incoming: bytes = b"",
    ) -> None:
        self.close_error = close_error
        self.body_error = body_error
        self.incoming = bytearray(incoming)
        self.open_count = 0
        self.close_count = 0
        self.active_error_at_close: BaseException | None = None
        self.active_traceback_at_close = None

    def open(self, device: str, *, baudrate: int, timeout_seconds: float) -> UARTPortHandle:
        self.open_count += 1
        return UARTPortHandle(
            SimpleNamespace(timeout=timeout_seconds), device, baudrate, timeout_seconds
        )

    def close(self, handle: UARTPortHandle) -> None:
        del handle
        self.close_count += 1
        self.active_error_at_close = sys.exc_info()[1]
        self.active_traceback_at_close = sys.exc_info()[2]
        if self.close_error is not None:
            raise self.close_error

    def reset_input_buffer(self, handle: UARTPortHandle) -> None:
        del handle

    def read(self, handle: UARTPortHandle, size: int) -> bytes:
        del handle
        if self.body_error is not None:
            raise self.body_error
        chunk = bytes(self.incoming[:size])
        del self.incoming[:size]
        return chunk

    def write(self, handle: UARTPortHandle, data: bytes) -> int:
        del handle
        if self.body_error is not None:
            raise self.body_error
        return len(data)


def _walk(error: BaseException) -> list[BaseException]:
    seen: set[int] = set()
    result: list[BaseException] = []

    def visit(item: BaseException | None) -> None:
        if item is None or id(item) in seen:
            return
        seen.add(id(item))
        result.append(item)
        visit(item.__cause__)
        visit(item.__context__)

    visit(error)
    return result


def _contains_traceback(error: BaseException, expected: object) -> bool:
    current = error.__traceback__
    while current is not None:
        if current is expected:
            return True
        current = current.tb_next
    return False


def _capture_exception(invoke: Callable[[], object]) -> BaseException:
    try:
        invoke()
    except BaseException as exc:  # noqa: BLE001 - the contract includes cancellation identity
        return exc
    raise AssertionError("expected an exception")


class UARTCloseCompositionSpecTests(unittest.TestCase):
    _DEVICE = "test-uart-device"
    _BAUD = 57600

    def _operations(self, adapter: UARTInterface) -> dict[str, Callable[[], object]]:
        return {
            "read": lambda: capture_uart_output(
                self._DEVICE, self._BAUD, 0.01, None, adapter=adapter
            ),
            "write": lambda: write_uart_output(
                self._DEVICE, self._BAUD, b"request", adapter=adapter
            ),
            "exchange": lambda: exchange_uart_output(
                self._DEVICE, self._BAUD, b"request", "reply", 0.01, adapter=adapter
            ),
        }

    def test_close_only_is_actionable_and_never_reopens_for_every_helper(self) -> None:
        for operation, invoke in self._operations_for_close_only().items():
            with self.subTest(operation=operation):
                raw_close = LookupError("close sentinel")
                adapter = _SentinelUART(close_error=raw_close)

                with self.assertRaises(RuntimeError) as raised:
                    invoke(adapter)

                failure = raised.exception
                self.assertEqual(
                    str(failure),
                    f"Unable to close UART after {operation} on {self._DEVICE} at {self._BAUD} baud; "
                    "handle cleanup is uncertain: LookupError: close sentinel",
                )
                self.assertIs(failure.__cause__, raw_close)
                self.assertIsNone(failure.__context__)
                self.assertTrue(failure.__suppress_context__)
                self.assertIsNone(raw_close.__context__)
                self.assertEqual(_walk(failure), [failure, raw_close])
                self.assertEqual(adapter.open_count, 1)
                self.assertEqual(adapter.close_count, 1)

    def _operations_for_close_only(self) -> dict[str, Callable[[_SentinelUART], object]]:
        return {
            "read": lambda adapter: capture_uart_output(
                self._DEVICE, self._BAUD, 0.01, "never", adapter=adapter
            ),
            "write": lambda adapter: write_uart_output(
                self._DEVICE, self._BAUD, b"request", adapter=adapter
            ),
            "exchange": lambda adapter: exchange_uart_output(
                self._DEVICE, self._BAUD, b"request", "reply", 0.01, adapter=adapter
            ),
        }

    def test_primary_plus_close_keeps_normalized_primary_and_cycle_free_graph(self) -> None:
        expected_messages = {
            "read": f"Unable to read {self._DEVICE} at {self._BAUD} baud: body sentinel",
            "write": f"Unable to write {self._DEVICE} at {self._BAUD} baud: body sentinel",
            "exchange": f"Unable to exchange data on {self._DEVICE} at {self._BAUD} baud: body sentinel",
        }
        for operation, invoke in self._operations_for_close_only().items():
            with self.subTest(operation=operation):
                raw_primary = ValueError("body sentinel")
                raw_close = OSError("close sentinel")
                adapter = _SentinelUART(body_error=raw_primary, close_error=raw_close)

                primary = _capture_exception(lambda: invoke(adapter))
                self.assertIsInstance(primary, RuntimeError)
                close = raw_primary.__context__
                self.assertIs(primary, adapter.active_error_at_close)
                self.assertTrue(_contains_traceback(primary, adapter.active_traceback_at_close))
                self.assertEqual(
                    str(primary),
                    expected_messages[operation]
                    + "; additionally, UART close failed and handle cleanup is uncertain: "
                    "OSError: close sentinel",
                )
                self.assertIs(primary.__cause__, raw_primary)
                self.assertIsInstance(close, RuntimeError)
                assert close is not None
                self.assertEqual(
                    str(close),
                    f"Unable to close UART after {operation} on {self._DEVICE} at {self._BAUD} baud; "
                    "handle cleanup is uncertain: OSError: close sentinel",
                )
                self.assertIs(close.__cause__, raw_close)
                self.assertIsNone(close.__context__)
                self.assertIsNone(raw_close.__context__)
                self.assertTrue(primary.__suppress_context__)
                self.assertTrue(close.__suppress_context__)
                self.assertEqual(_walk(primary), [primary, raw_primary, close, raw_close])
                self.assertEqual(len(_walk(primary)), 4)
                self.assertEqual(adapter.open_count, 1)
                self.assertEqual(adapter.close_count, 1)

    def test_cancellation_plus_close_keeps_exact_cancellation_principal(self) -> None:
        for operation, invoke in self._operations_for_close_only().items():
            with self.subTest(operation=operation):
                cancellation = OperationCancelledError("cancel sentinel")
                raw_close = ConnectionError("close sentinel")
                adapter = _SentinelUART(body_error=cancellation, close_error=raw_close)

                principal = _capture_exception(lambda: invoke(adapter))
                self.assertIsInstance(principal, OperationCancelledError)
                close = principal.__context__
                self.assertIs(principal, cancellation)
                self.assertIs(principal, adapter.active_error_at_close)
                self.assertTrue(_contains_traceback(principal, adapter.active_traceback_at_close))
                self.assertEqual(str(principal), "cancel sentinel")
                self.assertIsNone(principal.__cause__)
                self.assertIsInstance(close, RuntimeError)
                assert close is not None
                self.assertIs(close.__cause__, raw_close)
                self.assertIsNone(close.__context__)
                self.assertIsNone(raw_close.__context__)
                self.assertTrue(close.__suppress_context__)
                self.assertEqual(_walk(principal), [principal, close, raw_close])
                self.assertEqual(adapter.open_count, 1)
                self.assertEqual(adapter.close_count, 1)

    def test_primary_only_preserves_existing_normalization_for_every_helper(self) -> None:
        expected_messages = {
            "read": f"Unable to read {self._DEVICE} at {self._BAUD} baud: body sentinel",
            "write": f"Unable to write {self._DEVICE} at {self._BAUD} baud: body sentinel",
            "exchange": f"Unable to exchange data on {self._DEVICE} at {self._BAUD} baud: body sentinel",
        }
        for operation, invoke in self._operations_for_close_only().items():
            with self.subTest(operation=operation):
                raw_primary = ValueError("body sentinel")
                adapter = _SentinelUART(body_error=raw_primary)

                with self.assertRaises(RuntimeError) as raised:
                    invoke(adapter)

                failure = raised.exception
                self.assertEqual(str(failure), expected_messages[operation])
                self.assertIs(failure, adapter.active_error_at_close)
                self.assertIs(failure.__cause__, raw_primary)
                self.assertEqual(_walk(failure), [failure, raw_primary])
                self.assertEqual(adapter.open_count, 1)
                self.assertEqual(adapter.close_count, 1)

    def test_cancellation_only_is_re_raised_unchanged_for_every_helper(self) -> None:
        for operation, invoke in self._operations_for_close_only().items():
            with self.subTest(operation=operation):
                cancellation = OperationCancelledError("cancel sentinel")
                adapter = _SentinelUART(body_error=cancellation)

                principal = _capture_exception(lambda: invoke(adapter))
                self.assertIsInstance(principal, OperationCancelledError)
                self.assertIs(principal, cancellation)
                self.assertIs(cancellation, adapter.active_error_at_close)
                self.assertTrue(
                    _contains_traceback(cancellation, adapter.active_traceback_at_close)
                )
                self.assertEqual(str(cancellation), "cancel sentinel")
                self.assertIsNone(cancellation.__cause__)
                self.assertIsNone(cancellation.__context__)
                self.assertEqual(_walk(cancellation), [cancellation])
                self.assertEqual(adapter.open_count, 1)
                self.assertEqual(adapter.close_count, 1)

    def test_capture_on_open_cancellation_closes_once_and_preserves_traceback(self) -> None:
        cancellation = OperationCancelledError("open callback cancelled")
        adapter = _SentinelUART()

        def cancel_on_open() -> None:
            raise cancellation

        principal = _capture_exception(
            lambda: capture_uart_output(
                self._DEVICE,
                self._BAUD,
                0.1,
                None,
                on_port_open=cancel_on_open,
                adapter=adapter,
            )
        )

        self.assertIsInstance(principal, OperationCancelledError)
        self.assertIs(principal, cancellation)
        self.assertIs(principal, adapter.active_error_at_close)
        self.assertTrue(_contains_traceback(principal, adapter.active_traceback_at_close))
        self.assertEqual(str(principal), "open callback cancelled")
        self.assertIsNone(principal.__cause__)
        self.assertIsNone(principal.__context__)
        self.assertEqual(adapter.open_count, 1)
        self.assertEqual(adapter.close_count, 1)

    def test_capture_early_returns_close_once_without_implicit_reopen(self) -> None:
        for expected_text, max_bytes, incoming in (
            ("done", None, b"done"),
            (None, 3, b"abcdef"),
        ):
            with self.subTest(expected_text=expected_text, max_bytes=max_bytes):
                adapter = _SentinelUART(incoming=incoming)
                result = capture_uart_output(
                    self._DEVICE,
                    self._BAUD,
                    0.1,
                    expected_text,
                    max_bytes=max_bytes,
                    reopen_attempts=3,
                    adapter=adapter,
                )

                self.assertEqual(result.text, "done" if expected_text else "abc")
                self.assertEqual(result.reopen_count, 0)
                self.assertEqual(adapter.open_count, 1)
                self.assertEqual(adapter.close_count, 1)

    def test_capture_requested_reopen_and_healthy_results_remain_intact(self) -> None:
        adapter = _SentinelUART()
        capture = capture_uart_output(
            self._DEVICE,
            self._BAUD,
            0.03,
            None,
            reopen_attempts=1,
            per_open_window_seconds=0.005,
            reopen_delay_seconds=0,
            adapter=adapter,
        )
        self.assertEqual(capture.text, "")
        self.assertEqual(capture.reopen_count, 1)
        self.assertEqual(adapter.open_count, 2)
        self.assertEqual(adapter.close_count, 2)

        self.assertEqual(
            write_uart_output(
                self._DEVICE, self._BAUD, b"ok", adapter=_SentinelUART()
            ).bytes_written,
            2,
        )
        exchange = exchange_uart_output(
            self._DEVICE, self._BAUD, b"ask", "reply", 0.1, adapter=_SentinelUART(incoming=b"reply")
        )
        self.assertTrue(exchange.matched)
        self.assertEqual(exchange.text, "reply")
        self.assertEqual(exchange.bytes_written, 3)

    def test_read_serial_delegate_propagates_close_failure(self) -> None:
        raw_close = RuntimeError("close sentinel")
        adapter = _SentinelUART(close_error=raw_close)
        handle = SimpleNamespace(
            board=SimpleNamespace(default_baudrate=self._BAUD),
            metadata=SimpleNamespace(route_used="test-route"),
        )
        services = SerialToolServices(
            runtime_for=lambda _board: None,
            active_session_id=lambda _board: None,
            duration_ms=lambda _started: 0,
            record_event=lambda *args, **kwargs: None,
            format_refusal=lambda *args, **kwargs: "refused",
            handle_for=lambda _board: handle,
            resolve_port=lambda _handle, **_kwargs: SimpleNamespace(device=self._DEVICE),
            capture_uart=lambda *args, **kwargs: capture_uart_output(
                *args, adapter=adapter, **kwargs
            ),
            write_uart=lambda *args, **kwargs: None,
            exchange_uart=lambda *args, **kwargs: None,
            reset_target=lambda _handle: None,
            no_board_config_message="no board",
        )

        with self.assertRaisesRegex(RuntimeError, "Unable to close UART after read") as raised:
            read_serial(services, "board", read_seconds=0.01)

        self.assertIs(raised.exception.__cause__, raw_close)
        self.assertEqual(adapter.open_count, 1)
        self.assertEqual(adapter.close_count, 1)


if __name__ == "__main__":
    unittest.main()
