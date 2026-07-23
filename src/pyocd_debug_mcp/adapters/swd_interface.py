"""Internal SWD adapter contract for shared target-control services."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.timeouts import ServerTimeoutConfig


@dataclass(frozen=True, slots=True)
class TargetSessionMetadata:
    """Serializable facts for one live target session.

    ``runtime_token`` identifies only this live session. It is deliberately
    separate from optional hardware identity and is not stable across reconnects.
    """

    board_name: str
    probe_description: str
    probe_family: str
    probe_uid: str | None
    live_part_number: str | None
    route_used: str
    target_override: str | None
    runtime_token: str


@dataclass(frozen=True, slots=True)
class TargetSessionHandle:
    """Open target session plus the board facts and routing used to create it."""

    session: Any
    board: BoardConfig | None
    probe_uid: str | None
    route_used: str
    target_override: str | None
    worker: Any | None = None
    metadata: TargetSessionMetadata | None = None

    def __post_init__(self) -> None:
        if self.metadata is not None:
            return
        board_name = self.board.display_name if self.board is not None else ""
        probe_family = self.board.probe_family if self.board is not None else "unknown"
        object.__setattr__(
            self,
            "metadata",
            TargetSessionMetadata(
                board_name=board_name,
                probe_description="",
                probe_family=probe_family,
                probe_uid=self.probe_uid,
                live_part_number=None,
                route_used=self.route_used,
                target_override=self.target_override,
                runtime_token=uuid4().hex,
            ),
        )


def session_metadata(handle: TargetSessionHandle) -> TargetSessionMetadata:
    """Return the immutable metadata record established for every live handle."""

    if handle.metadata is None:
        raise RuntimeError("Target session has no immutable metadata record.")
    return handle.metadata


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
        protocol: str | None = None,
        connect_mode: str | None = None,
        pack_path: Path | None = None,
        pack_sha256: str | None = None,
        pdsc_device: str | None = None,
        frequency_hz: int | None = None,
        operation_timeout_seconds: float | None = None,
    ) -> TargetSessionHandle:
        """Open a live debug session, optionally with one quarantined pack candidate."""

    @abstractmethod
    def close(self, handle: TargetSessionHandle) -> None:
        """Close a previously opened session."""

    @abstractmethod
    def connect_under_reset(
        self,
        *,
        board: BoardConfig | None,
        unique_id: str | None,
        target: str | None,
        server_timeouts: ServerTimeoutConfig | None = None,
        pack_path: Path | None = None,
        pack_sha256: str | None = None,
        pdsc_device: str | None = None,
        operation_timeout_seconds: float | None = None,
    ) -> TargetSessionHandle:
        """Assert physical reset, attach and halt, then release reset."""

    @abstractmethod
    def get_state(self, handle: TargetSessionHandle) -> str:
        """Return the target's current run state."""

    @abstractmethod
    def read_memory(
        self,
        handle: TargetSessionHandle,
        address: int,
        width_bits: int,
        *,
        operation_timeout_seconds: float | None = None,
    ) -> int:
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
    def supported_core_registers(self, handle: TargetSessionHandle) -> tuple[str, ...]:
        """Return register names discovered from the connected core at runtime."""

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
    def release_reset(self, handle: TargetSessionHandle) -> None:
        """Deassert the connected probe's wired reset line."""

    @abstractmethod
    def flash(
        self,
        handle: TargetSessionHandle,
        firmware: Path,
        *,
        halt_after_reset: bool,
    ) -> str:
        """Flash and return an observed or unconfirmed post-reset target state."""

    @abstractmethod
    def recover(self, handle: TargetSessionHandle) -> None:
        """Run the backend's native recover/unlock path."""

    def supports_recovery(self, handle: TargetSessionHandle, mechanism: str) -> bool:
        """Return whether this live backend exposes the requested typed recovery primitive."""

        del handle, mechanism
        return False

    @abstractmethod
    def set_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        """Set a breakpoint."""

    @abstractmethod
    def remove_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        """Remove a breakpoint."""
