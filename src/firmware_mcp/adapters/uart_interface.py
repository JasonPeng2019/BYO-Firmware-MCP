"""Low-level UART adapter contract used by shared capture services."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class UARTPortHandle:
    """Live UART port handle plus the opening parameters used."""

    handle: Any
    device: str
    baudrate: int
    timeout_seconds: float


class UARTInterface(ABC):
    """Backend-neutral UART transport contract."""

    @abstractmethod
    def open(self, device: str, *, baudrate: int, timeout_seconds: float) -> UARTPortHandle:
        """Open a UART transport and return a live port handle."""

    @abstractmethod
    def close(self, handle: UARTPortHandle) -> None:
        """Close a previously opened UART port."""

    @abstractmethod
    def reset_input_buffer(self, handle: UARTPortHandle) -> None:
        """Clear any buffered UART input before capture starts."""

    @abstractmethod
    def read(self, handle: UARTPortHandle, size: int) -> bytes:
        """Read up to ``size`` bytes from the live port."""

    def read_with_timeout(
        self,
        handle: UARTPortHandle,
        size: int,
        *,
        timeout_seconds: float,
    ) -> bytes:
        """Read with a temporary narrower timeout when the transport supports it."""

        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        previous_recorded_timeout = handle.timeout_seconds
        transport = handle.handle
        transport_timeout_changed = False
        if not hasattr(transport, "timeout"):
            raise RuntimeError(
                "UART adapter cannot narrow a read deadline; override read_with_timeout"
            )
        previous_transport_timeout = transport.timeout
        primary: BaseException | None = None
        try:
            handle.timeout_seconds = timeout_seconds
            try:
                transport.timeout = timeout_seconds
                transport_timeout_changed = True
            except (AttributeError, TypeError) as exc:
                raise RuntimeError(
                    "UART adapter cannot narrow a read deadline; override read_with_timeout"
                ) from exc
            return self.read(handle, size)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                if transport_timeout_changed:
                    try:
                        transport.timeout = previous_transport_timeout
                    except Exception as cleanup:
                        if primary is not None:
                            primary.add_note(
                                "UART timeout restoration failed: "
                                f"{type(cleanup).__name__}: {cleanup}"
                            )
                        else:
                            raise
            finally:
                handle.timeout_seconds = previous_recorded_timeout

    @abstractmethod
    def write(self, handle: UARTPortHandle, data: bytes) -> int:
        """Write bytes to the live port and return the number of bytes accepted."""
