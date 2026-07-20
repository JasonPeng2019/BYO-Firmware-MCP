"""Shared board-control services used by the MCP server and Stage 0."""

from __future__ import annotations

from pathlib import Path

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.adapters.swd_pyocd import (
    PyOCDSWDInterface,
    build_session_options as _build_session_options,
)
from pyocd_debug_mcp.board_config import (
    RECOVER_MODE_MANUAL_ONLY,
    RECOVER_MODE_BACKEND_MASS_ERASE,
    BoardConfig,
)
from pyocd_debug_mcp.timeouts import ServerTimeoutConfig

_BACKEND = PyOCDSWDInterface()


def build_session_options(
    board: BoardConfig | None,
    target: str | None,
    server_timeouts: ServerTimeoutConfig | None = None,
) -> dict[str, object] | None:
    """Expose the backend option builder for tests and wrapper compatibility."""

    return _build_session_options(board, target, server_timeouts)


def open_session(
    *,
    board: BoardConfig | None,
    unique_id: str | None = None,
    target: str | None = None,
    server_timeouts: ServerTimeoutConfig | None = None,
    protocol: str | None = None,
    connect_mode: str | None = None,
    pack_path: Path | None = None,
    pack_sha256: str | None = None,
    pdsc_device: str | None = None,
    frequency_hz: int | None = None,
) -> TargetSessionHandle:
    return _BACKEND.open(
        board=board,
        unique_id=unique_id,
        target=target,
        server_timeouts=server_timeouts,
        protocol=protocol,
        connect_mode=connect_mode,
        pack_path=pack_path,
        pack_sha256=pack_sha256,
        pdsc_device=pdsc_device,
        frequency_hz=frequency_hz,
    )


def close_session(handle: TargetSessionHandle) -> None:
    _BACKEND.close(handle)


def connect_under_reset(
    *,
    board: BoardConfig | None,
    unique_id: str | None = None,
    target: str | None = None,
    server_timeouts: ServerTimeoutConfig | None = None,
    pack_path: Path | None = None,
    pack_sha256: str | None = None,
    pdsc_device: str | None = None,
) -> TargetSessionHandle:
    return _BACKEND.connect_under_reset(
        board=board,
        unique_id=unique_id,
        target=target,
        server_timeouts=server_timeouts,
        pack_path=pack_path,
        pack_sha256=pack_sha256,
        pdsc_device=pdsc_device,
    )


def get_state(handle: TargetSessionHandle) -> str:
    return _BACKEND.get_state(handle)


def read_memory(handle: TargetSessionHandle, address: int, width_bits: int = 32) -> int:
    return _BACKEND.read_memory(handle, address, width_bits)


def read_memory_block(handle: TargetSessionHandle, address: int, length: int) -> list[int]:
    return _BACKEND.read_memory_block(handle, address, length)


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


def halt(handle: TargetSessionHandle) -> None:
    _BACKEND.halt(handle)


def resume(handle: TargetSessionHandle) -> None:
    _BACKEND.resume(handle)


def step(handle: TargetSessionHandle) -> int:
    _BACKEND.step(handle)
    return read_core_register(handle, "pc")


def reset(handle: TargetSessionHandle, *, halt_after: bool = True) -> None:
    if halt_after:
        _BACKEND.reset_and_halt(handle)
    else:
        _BACKEND.reset(handle)


def flash_firmware(
    handle: TargetSessionHandle,
    firmware: Path,
    *,
    halt_after_reset: bool = False,
) -> Path:
    path = Path(firmware).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Firmware artifact does not exist: {path}")
    _BACKEND.flash(handle, path, halt_after_reset=halt_after_reset)
    return path


def recover_target(
    handle: TargetSessionHandle,
    *,
    recover_mode: str | None = None,
) -> str:
    """Run one typed vendor recovery primitive, never an arbitrary target write."""

    board = handle.board
    configured = board.recover_mode if board is not None else None
    if configured == RECOVER_MODE_MANUAL_ONLY:
        display_name = board.display_name if board is not None else "This board"
        raise RuntimeError(
            f"{display_name} requires a manual recover procedure for this family; this repo "
            "does not automate recover_mode=manual_only."
        )
    selected = (recover_mode or configured or "").strip()
    if not selected:
        display_name = board.display_name if board is not None else "This target"
        raise RuntimeError(f"{display_name} does not define a recover mode.")
    if configured and configured != selected:
        raise RuntimeError(
            f"Requested recover mode {selected!r} does not match configured mode {configured!r}."
        )
    if selected == RECOVER_MODE_BACKEND_MASS_ERASE:
        if not _BACKEND.supports_recovery(handle, selected):
            raise RuntimeError("The connected target backend does not support typed mass erase.")
        _BACKEND.recover(handle)
        return "typed backend mass erase"
    raise RuntimeError(f"Unsupported recover mode: {selected}")


def supports_recovery(handle: TargetSessionHandle, mechanism: str) -> bool:
    """Check a destructive capability on the live typed backend before authorization."""

    return _BACKEND.supports_recovery(handle, mechanism)


def set_breakpoint(handle: TargetSessionHandle, address: int) -> None:
    _BACKEND.set_breakpoint(handle, address)


def remove_breakpoint(handle: TargetSessionHandle, address: int) -> None:
    _BACKEND.remove_breakpoint(handle, address)
