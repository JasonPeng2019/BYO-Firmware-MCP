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

    @abstractmethod
    def write(self, handle: UARTPortHandle, data: bytes) -> int:
        """Write bytes to the live port and return the number of bytes accepted."""
