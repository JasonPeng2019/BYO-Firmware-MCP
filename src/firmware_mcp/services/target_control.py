"""Shared board-control services used by the MCP server and Stage 0."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence

from firmware_mcp.adapters.debug_interface import (
    FlashVerification,
    PhysicalMemoryRegion,
    RecoveryCapability,
    RecoveryResult,
    TargetSessionHandle,
)
from firmware_mcp.adapters.debug_process import ProcessIsolatedDebugInterface
from firmware_mcp.board_config import BoardConfig
from firmware_mcp.target_errors import RecoveryPostDispatchError, TargetStateError

_BACKEND = ProcessIsolatedDebugInterface()


@dataclass(frozen=True, slots=True)
class RecoveryDispatch:
    """The exact selected descriptor and the provider's unmodified result."""

    selected_capability: RecoveryCapability
    result: RecoveryResult

    def to_record(self) -> dict[str, object]:
        return {
            "selected_capability": self.selected_capability.to_record(),
            "mechanism": self.result.mechanism,
            "accepted": self.result.accepted,
            "verification": self.result.verification,
            "observed_session_postcondition": self.result.observed_session_postcondition,
        }


def open_session(
    *,
    board: BoardConfig | None,
    unique_id: str | None = None,
    target: str | None = None,
    protocol: str | None = None,
    connect_mode: str | None = None,
    pack_path: Path | None = None,
    pack_sha256: str | None = None,
    pdsc_device: str | None = None,
    frequency_hz: int | None = None,
    worker_argv: Sequence[str] | None = None,
) -> TargetSessionHandle:
    return _BACKEND.open(
        board=board,
        unique_id=unique_id,
        target=target,
        protocol=protocol,
        connect_mode=connect_mode,
        pack_path=pack_path,
        pack_sha256=pack_sha256,
        pdsc_device=pdsc_device,
        frequency_hz=frequency_hz,
        worker_argv=worker_argv,
    )


def close_session(handle: TargetSessionHandle) -> dict[str, object] | None:
    return _BACKEND.close(handle)


def connect_under_reset(
    *,
    board: BoardConfig | None,
    unique_id: str | None = None,
    target: str | None = None,
    pack_path: Path | None = None,
    pack_sha256: str | None = None,
    pdsc_device: str | None = None,
    worker_argv: Sequence[str] | None = None,
) -> TargetSessionHandle:
    return _BACKEND.connect_under_reset(
        board=board,
        unique_id=unique_id,
        target=target,
        pack_path=pack_path,
        pack_sha256=pack_sha256,
        pdsc_device=pdsc_device,
        worker_argv=worker_argv,
    )


def get_state(handle: TargetSessionHandle) -> str:
    return _BACKEND.get_state(handle)


def read_memory(
    handle: TargetSessionHandle,
    address: int,
    width_bits: int = 32,
) -> int:
    return _BACKEND.read_memory(
        handle,
        address,
        width_bits,
    )


def release_reset(handle: TargetSessionHandle) -> None:
    _BACKEND.release_reset(handle)


def read_memory_block(handle: TargetSessionHandle, address: int, length: int) -> list[int]:
    return _BACKEND.read_memory_block(handle, address, length)


def physical_memory_regions(handle: TargetSessionHandle) -> tuple[PhysicalMemoryRegion, ...]:
    """Return live, handle-bound provider physical-memory facts."""

    return _BACKEND.physical_memory_regions(handle)


def write_memory(
    handle: TargetSessionHandle, address: int, value: int, width_bits: int = 32
) -> None:
    _BACKEND.write_memory(handle, address, value, width_bits)


def read_core_register(handle: TargetSessionHandle, name: str) -> int:
    return _BACKEND.read_core_register(handle, name)


def write_core_register(handle: TargetSessionHandle, name: str, value: int) -> None:
    _BACKEND.write_core_register(handle, name, value)


def supported_core_registers(handle: TargetSessionHandle) -> tuple[str, ...]:
    return _BACKEND.supported_core_registers(handle)


def halt(handle: TargetSessionHandle) -> str:
    _BACKEND.halt(handle)
    observed = get_state(handle)
    if observed.casefold() != "halted":
        raise TargetStateError(
            f"Halt command completed but observed state is {observed}, not HALTED. Reconnect and retry."
        )
    return observed


def resume(handle: TargetSessionHandle) -> str:
    _BACKEND.resume(handle)
    # RUNNING is not required: a breakpoint, fault, or self-halt can legitimately
    # take effect between command acceptance and the immediate observation.
    return get_state(handle)


def step(handle: TargetSessionHandle) -> tuple[str, int]:
    _BACKEND.step(handle)
    observed = get_state(handle)
    if observed.casefold() != "halted":
        raise TargetStateError(
            f"Step command completed but observed state is {observed}, not HALTED. Reconnect and retry."
        )
    return observed, read_core_register(handle, "pc")


def reset(handle: TargetSessionHandle, *, halt_after: bool = True) -> str:
    if halt_after:
        _BACKEND.reset_and_halt(handle)
    else:
        _BACKEND.reset(handle)
    observed = get_state(handle)
    if halt_after and observed.casefold() != "halted":
        raise TargetStateError(
            "Reset command completed; halt_after_reset=true; "
            f"observed_state={observed}; expected_state=HALTED. Reconnect and retry."
        )
    return observed


def flash_firmware(
    handle: TargetSessionHandle,
    firmware: Path,
    *,
    halt_after_reset: bool = False,
) -> FlashVerification:
    path = Path(firmware).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Firmware artifact does not exist: {path}")
    return _BACKEND.flash(handle, path, halt_after_reset=halt_after_reset)


def recover_target(
    handle: TargetSessionHandle,
    *,
    mechanism: str,
) -> RecoveryDispatch:
    """Ask the live backend to run one supported recovery mechanism.

    The backend return only proves command acceptance. It does not prove that a
    destructive recovery changed target state, because providers do not expose a
    common observable postcondition for that operation.
    """

    selected = mechanism
    if not selected or selected != selected.strip():
        raise ValueError("Recovery mechanism must be an exact non-empty provider capability name.")
    capabilities = recovery_capabilities(handle)
    selected_capability = next((item for item in capabilities if item.mechanism == selected), None)
    if selected_capability is None:
        raise RuntimeError(
            f"Recovery mechanism '{selected}' is unavailable from the connected provider. "
            "Reconnect with a provider that exposes it or select a documented live capability."
        )
    # Everything below this call is a destructive post-dispatch fact.  Keep
    # the chosen live descriptor even if the worker reply cannot be decoded.
    try:
        result = _BACKEND.recover(handle, selected)
    except Exception as exc:
        raise RecoveryPostDispatchError(selected_capability, None, exc) from exc
    if result.mechanism != selected_capability.mechanism:
        mismatch = TargetStateError(
            "Provider returned recovery mechanism "
            f"'{result.mechanism}' after dispatching '{selected_capability.mechanism}'."
        )
        raise RecoveryPostDispatchError(selected_capability, result, mismatch) from mismatch
    return RecoveryDispatch(selected_capability, result)


def recovery_capabilities(handle: TargetSessionHandle) -> tuple[RecoveryCapability, ...]:
    """Return current provider-declared recovery operations for this session."""

    return _BACKEND.recovery_capabilities(handle)


def set_breakpoint(handle: TargetSessionHandle, address: int) -> None:
    _BACKEND.set_breakpoint(handle, address)


def remove_breakpoint(handle: TargetSessionHandle, address: int) -> None:
    _BACKEND.remove_breakpoint(handle, address)
