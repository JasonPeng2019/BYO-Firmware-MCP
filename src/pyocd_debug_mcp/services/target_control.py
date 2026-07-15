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
    RECOVER_MODE_NRF_PYOCD_UNLOCK,
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
) -> TargetSessionHandle:
    return _BACKEND.open(
        board=board,
        unique_id=unique_id,
        target=target,
        server_timeouts=server_timeouts,
    )


def close_session(handle: TargetSessionHandle) -> None:
    _BACKEND.close(handle)


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


def recover_target(handle: TargetSessionHandle) -> str:
    board = handle.board
    if board is None:
        raise RuntimeError("Recover requires a board config with a recover_mode.")
    if not board.recover_mode:
        raise RuntimeError(f"{board.display_name} does not define a recover mode.")
    if board.recover_mode == RECOVER_MODE_MANUAL_ONLY:
        raise RuntimeError(
            f"{board.display_name} requires a manual recover procedure for this family; this repo does not automate recover_mode=manual_only."
        )
    if board.recover_mode == RECOVER_MODE_NRF_PYOCD_UNLOCK:
        _BACKEND.recover(handle)
        return "pyOCD API mass erase"
    raise RuntimeError(f"Unsupported recover mode: {board.recover_mode}")


def set_breakpoint(handle: TargetSessionHandle, address: int) -> None:
    _BACKEND.set_breakpoint(handle, address)


def remove_breakpoint(handle: TargetSessionHandle, address: int) -> None:
    _BACKEND.remove_breakpoint(handle, address)
