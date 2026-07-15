"""Shared UART capture helpers for Stage 0 and later harnesses."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from pyocd_debug_mcp.adapters.uart_interface import UARTInterface
from pyocd_debug_mcp.adapters.uart_pyserial import PySerialUARTInterface

_BACKEND: UARTInterface = PySerialUARTInterface()


@dataclass(frozen=True)
class UARTCaptureResult:
    text: str
    expected_text: str | None
    reopen_count: int
    duration_seconds: float

    @property
    def excerpt(self) -> str:
        excerpt = self.text.strip().replace("\r", "\\r").replace("\n", "\\n")
        return excerpt[:300] if excerpt else ""

    @property
    def has_output(self) -> bool:
        return bool(self.text.strip())

    @property
    def matched(self) -> bool:
        if self.expected_text is None:
            return self.has_output
        return self.expected_text in self.text


@dataclass(frozen=True)
class UARTWriteResult:
    bytes_written: int
    duration_seconds: float


def capture_uart_output(
    device: str,
    baudrate: int,
    read_seconds: float,
    expected_text: str | None,
    *,
    on_port_open: Callable[[], None] | None = None,
    reopen_attempts: int = 1,
    reopen_delay_seconds: float = 0.15,
    per_open_window_seconds: float = 0.75,
    adapter: UARTInterface | None = None,
) -> UARTCaptureResult:
    """Capture UART output with one optional reopen cycle after flash/reset.

    The reopen behavior matters for targets whose first reset after flashing races
    the host's initial port-open. Captured bytes are accumulated across opens so a
    match can span chunk boundaries or a reopen boundary.
    """

    if baudrate <= 0:
        raise ValueError("baudrate must be > 0")
    if read_seconds <= 0:
        raise ValueError("read_seconds must be > 0")
    if reopen_attempts < 0:
        raise ValueError("reopen_attempts must be >= 0")
    if reopen_delay_seconds < 0:
        raise ValueError("reopen_delay_seconds must be >= 0")
    if per_open_window_seconds <= 0:
        raise ValueError("per_open_window_seconds must be > 0")

    backend = adapter or _BACKEND
    started = time.monotonic()
    deadline = started + read_seconds
    captured = bytearray()
    total_attempts = max(1, reopen_attempts + 1)
    reopen_count = 0

    for attempt in range(total_attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        open_deadline = min(deadline, time.monotonic() + min(per_open_window_seconds, remaining))
        timeout = min(0.2, max(0.05, min(per_open_window_seconds, remaining)))
        port_handle = None
        try:
            port_handle = backend.open(device, baudrate=baudrate, timeout_seconds=timeout)
            backend.reset_input_buffer(port_handle)
            if on_port_open is not None:
                on_port_open()
            while time.monotonic() < open_deadline:
                chunk = backend.read(port_handle, 256)
                if chunk:
                    captured.extend(chunk)
                    text = captured.decode("utf-8", errors="replace")
                    if expected_text is None and text.strip():
                        return UARTCaptureResult(
                            text=text,
                            expected_text=expected_text,
                            reopen_count=reopen_count,
                            duration_seconds=time.monotonic() - started,
                        )
                    if expected_text and expected_text in text:
                        return UARTCaptureResult(
                            text=text,
                            expected_text=expected_text,
                            reopen_count=reopen_count,
                            duration_seconds=time.monotonic() - started,
                        )
        except Exception as exc:  # noqa: BLE001 - want the raw serial error
            raise RuntimeError(f"Unable to read {device} at {baudrate} baud: {exc}") from exc
        finally:
            if port_handle is not None:
                backend.close(port_handle)

        if attempt < total_attempts - 1:
            reopen_count += 1
            if reopen_delay_seconds > 0:
                time.sleep(min(reopen_delay_seconds, max(0.0, deadline - time.monotonic())))

    return UARTCaptureResult(
        text=captured.decode("utf-8", errors="replace"),
        expected_text=expected_text,
        reopen_count=reopen_count,
        duration_seconds=time.monotonic() - started,
    )


def write_uart_output(
    device: str,
    baudrate: int,
    payload: bytes,
    *,
    timeout_seconds: float = 1.0,
    adapter: UARTInterface | None = None,
) -> UARTWriteResult:
    """Write bounded UART bytes through the same backend-neutral transport as capture."""

    if baudrate <= 0:
        raise ValueError("baudrate must be > 0")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")

    backend = adapter or _BACKEND
    started = time.monotonic()
    port_handle = None
    try:
        port_handle = backend.open(device, baudrate=baudrate, timeout_seconds=timeout_seconds)
        bytes_written = backend.write(port_handle, payload)
    except Exception as exc:  # noqa: BLE001 - want the raw serial error
        raise RuntimeError(f"Unable to write {device} at {baudrate} baud: {exc}") from exc
    finally:
        if port_handle is not None:
            backend.close(port_handle)
    return UARTWriteResult(
        bytes_written=bytes_written,
        duration_seconds=time.monotonic() - started,
    )
