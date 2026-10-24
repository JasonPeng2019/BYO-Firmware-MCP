"""pyserial-backed UART adapter implementation."""

from __future__ import annotations

import os

from pyocd_debug_mcp.adapters.uart_interface import UARTInterface, UARTPortHandle


class PySerialUARTInterface(UARTInterface):
    """Low-level UART adapter using pyserial.

    Native POSIX opens request pyserial's advisory exclusive flock. Other
    programs must cooperate for it to exclude them; it is not a hardware lease.
    Windows COM opens retain the backend's native exclusive-open behavior.
    """

    def open(self, device: str, *, baudrate: int, timeout_seconds: float) -> UARTPortHandle:
        try:
            import serial  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError("pyserial is not installed") from exc

        options = {}
        if os.name == "posix" and serial.Serial.__module__ == "serial.serialposix":
            options["exclusive"] = True
        serial_handle = serial.Serial(
            device,
            baudrate=baudrate,
            timeout=timeout_seconds,
            write_timeout=timeout_seconds,
            **options,
        )
        return UARTPortHandle(
            handle=serial_handle,
            device=device,
            baudrate=baudrate,
            timeout_seconds=timeout_seconds,
        )

    def close(self, handle: UARTPortHandle) -> None:
        handle.handle.close()

    def reset_input_buffer(self, handle: UARTPortHandle) -> None:
        handle.handle.reset_input_buffer()

    def read(self, handle: UARTPortHandle, size: int) -> bytes:
        return bytes(handle.handle.read(size))

    def write(self, handle: UARTPortHandle, data: bytes) -> int:
        bytes_written = int(handle.handle.write(data))
        handle.handle.flush()
        return bytes_written
