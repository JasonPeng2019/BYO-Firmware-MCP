"""Internal SWD adapter contract for shared target-control services."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.timeouts import ServerTimeoutConfig


@dataclass
class TargetSessionHandle:
    """Open target session plus the board facts and routing used to create it."""

    session: Any
    board: BoardConfig | None
    probe_uid: str | None
    route_used: str
    target_override: str | None


class SWDInterface(ABC):
    """Minimal target-control surface shared by server and Stage 0."""

    @abstractmethod
    def open(
        self,
        *,
        board: BoardConfig | None,
        unique_id: str | None,
        target: str | None,
        server_timeouts: ServerTimeoutConfig | None = None,
    ) -> TargetSessionHandle:
        """Open a live debug session for the requested board or raw target."""

    @abstractmethod
    def close(self, handle: TargetSessionHandle) -> None:
        """Close a previously opened session."""

    @abstractmethod
    def get_state(self, handle: TargetSessionHandle) -> str:
        """Return the target's current run state."""

    @abstractmethod
    def read_memory(self, handle: TargetSessionHandle, address: int, width_bits: int) -> int:
        """Read one memory value."""

    @abstractmethod
    def read_memory_block(
        self, handle: TargetSessionHandle, address: int, length: int
    ) -> list[int]:
        """Read a block of bytes from target memory."""

    @abstractmethod
    def write_memory(
        self,
        handle: TargetSessionHandle,
        address: int,
        value: int,
        width_bits: int,
    ) -> None:
        """Write one memory value."""

    @abstractmethod
    def read_core_register(self, handle: TargetSessionHandle, name: str) -> int:
        """Read one core register."""

    @abstractmethod
    def write_core_register(self, handle: TargetSessionHandle, name: str, value: int) -> None:
        """Write one core register."""

    @abstractmethod
    def halt(self, handle: TargetSessionHandle) -> None:
        """Halt the target core."""

    @abstractmethod
    def resume(self, handle: TargetSessionHandle) -> None:
        """Resume the target core."""

    @abstractmethod
    def step(self, handle: TargetSessionHandle) -> None:
        """Single-step one instruction."""

    @abstractmethod
    def reset(self, handle: TargetSessionHandle) -> None:
        """Reset and run the target."""

    @abstractmethod
    def reset_and_halt(self, handle: TargetSessionHandle) -> None:
        """Reset and halt the target."""

    @abstractmethod
    def flash(
        self,
        handle: TargetSessionHandle,
        firmware: Path,
        *,
        halt_after_reset: bool,
    ) -> None:
        """Flash a target artifact using the backend's native path."""

    @abstractmethod
    def recover(self, handle: TargetSessionHandle) -> None:
        """Run the backend's native recover/unlock path."""

    @abstractmethod
    def set_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        """Set a breakpoint."""

    @abstractmethod
    def remove_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        """Remove a breakpoint."""
