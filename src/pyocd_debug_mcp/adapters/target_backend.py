"""Provider-neutral target backend contract (legacy module name retained for imports)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
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
    backend_owner: TargetBackend | None = None


@dataclass(frozen=True, slots=True)
class BackendProbe:
    """Stable provider-neutral probe inventory row."""

    uid: str
    description: str
    family: str


@dataclass(frozen=True, slots=True)
class TargetSessionDescription:
    """Provider-neutral live identity and display facts for one open session."""

    board_name: str
    live_target_part: str
    probe_description: str


class RegisterClass(str, Enum):
    """Backend-declared role of one architecture register."""

    ORDINARY = "ordinary"
    EXECUTION_STATE = "execution_state"
    PROHIBITED = "prohibited"


@dataclass(frozen=True, slots=True)
class RegisterDescriptor:
    """Architecture-neutral register metadata used by the public register tools."""

    name: str
    register_class: RegisterClass
    width_bits: int


@dataclass(frozen=True, slots=True)
class MemoryAccessCapabilities:
    """Widths and alignment supplied by the connected target backend."""

    read_width_bits: tuple[int, ...]
    write_width_bits: tuple[int, ...]
    address_bits: int
    peripheral_width_bits: int | None = None
    peripheral_alignment_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class MaskedWriteResult:
    """Provider-neutral result of one backend-owned masked peripheral write."""

    prior: int
    updated: int
    width_bits: int


class TargetBackend(ABC):
    """Minimal target-control surface shared by Server B and Stage 0.

    The protocol does not require SWD. Implementations may route through SWD, JTAG, a GDB remote,
    or a vendor service as long as they preserve the typed operations and cleanup contract.
    """

    backend_name = "custom"

    def discover_targets(self) -> tuple[str, ...]:
        """Return target identities this backend can resolve without a profile."""

        return ()

    def discover_probes(self) -> tuple[BackendProbe, ...]:
        """Return probes this backend can open, using stable identities."""

        return ()

    def build_session_options(
        self,
        board: BoardConfig | None,
        target: str | None,
        server_timeouts: ServerTimeoutConfig | None = None,
    ) -> dict[str, object] | None:
        """Return backend-native session options, if exposed for diagnostics."""

        del board, target, server_timeouts
        return None

    @abstractmethod
    def open(
        self,
        *,
        board: BoardConfig | None,
        unique_id: str | None,
        target: str | None,
        server_timeouts: ServerTimeoutConfig | None = None,
        connect_mode: str | None = None,
    ) -> TargetSessionHandle:
        """Open a live debug session for the requested board or raw target."""

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
    ) -> TargetSessionHandle:
        """Assert physical reset, attach and halt, then release reset."""

    @abstractmethod
    def get_state(self, handle: TargetSessionHandle) -> str:
        """Return the target's current run state."""

    @abstractmethod
    def describe_session(self, handle: TargetSessionHandle) -> TargetSessionDescription:
        """Return live identity/display facts without exposing backend-native objects."""

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
    def supported_core_registers(self, handle: TargetSessionHandle) -> tuple[str, ...]:
        """Return register names discovered from the connected core at runtime."""

    def describe_core_register(
        self, handle: TargetSessionHandle, name: str
    ) -> RegisterDescriptor | None:
        """Classify a runtime register, or return ``None`` when unsupported.

        Architecture policy belongs to the backend. The common MCP layer never infers an ARM,
        RISC-V, Xtensa, or other register model from a spelling convention.
        """

        del handle, name
        return None

    def memory_access_capabilities(
        self, handle: TargetSessionHandle
    ) -> MemoryAccessCapabilities:
        """Return live scalar-memory and peripheral-access capabilities.

        The empty default fails closed while permitting a read-only or recovery-only backend to
        implement the rest of the target-control contract without advertising memory widths.
        """

        del handle
        return MemoryAccessCapabilities((), (), 0)

    def masked_register_write(
        self,
        handle: TargetSessionHandle,
        address: int,
        mask: int,
        value: int,
    ) -> MaskedWriteResult:
        """Perform one lock-scoped masked peripheral write using backend-declared width."""

        capabilities = self.memory_access_capabilities(handle)
        width_bits = capabilities.peripheral_width_bits
        if width_bits is None or width_bits not in capabilities.read_width_bits:
            raise NotImplementedError("target backend does not support masked peripheral writes")
        if width_bits not in capabilities.write_width_bits:
            raise NotImplementedError("target backend does not support masked peripheral writes")
        maximum = (1 << width_bits) - 1
        prior = self.read_memory(handle, address, width_bits)
        updated = (prior & ~(mask & maximum)) | (value & mask & maximum)
        self.write_memory(handle, address, updated, width_bits)
        return MaskedWriteResult(prior, updated, width_bits)

    @abstractmethod
    def halt(self, handle: TargetSessionHandle) -> None:
        """Halt the target core."""

    @abstractmethod
    def resume(self, handle: TargetSessionHandle) -> None:
        """Resume the target core."""

    @abstractmethod
    def step(self, handle: TargetSessionHandle) -> int:
        """Single-step one instruction and return the architecture's program counter."""

    @abstractmethod
    def reset(self, handle: TargetSessionHandle) -> None:
        """Reset and run the target."""

    @abstractmethod
    def reset_and_halt(self, handle: TargetSessionHandle) -> None:
        """Reset and halt the target."""

    @abstractmethod
    def release_reset(self, handle: TargetSessionHandle) -> None:
        """Best-effort deassertion of the backend's physical reset line."""

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

    def supports_recovery(self, handle: TargetSessionHandle, mechanism: str) -> bool:
        """Return whether this live backend exposes the requested typed recovery primitive."""

        del handle, mechanism
        return False

    @abstractmethod
    def breakpoint_memory_span_bytes(
        self, handle: TargetSessionHandle, address: int
    ) -> int:
        """Return the conservative instruction-memory span checked before a breakpoint.

        Backends that may choose a software breakpoint must return the maximum bytes they
        can replace at this address. A hardware-only backend returns one so executable
        address containment is still checked without claiming an instruction width.
        """

    @abstractmethod
    def set_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        """Set a breakpoint."""

    @abstractmethod
    def remove_breakpoint(self, handle: TargetSessionHandle, address: int) -> None:
        """Remove a breakpoint."""
