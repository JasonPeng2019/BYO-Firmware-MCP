"""Shared board-control services used by the MCP server and Stage 0."""

from __future__ import annotations

from pathlib import Path
from threading import RLock

from pyocd_debug_mcp.adapters.target_backend import (
    MaskedWriteResult,
    MemoryAccessCapabilities,
    RegisterDescriptor,
    TargetBackend,
    TargetSessionDescription,
    TargetSessionHandle,
)
from pyocd_debug_mcp.adapters.backend_registry import available_backends as _available_backends
from pyocd_debug_mcp.adapters.backend_registry import configured_backend
from pyocd_debug_mcp.board_config import (
    RECOVER_MODE_MANUAL_ONLY,
    RECOVER_MODE_BACKEND_MASS_ERASE,
    BoardConfig,
)
from pyocd_debug_mcp.timeouts import ServerTimeoutConfig

_BACKEND_GUARD = RLock()
_BACKEND: TargetBackend = configured_backend()
_BACKENDS: dict[str, TargetBackend] = {_BACKEND.backend_name: _BACKEND}


def _backend_for_board(board: BoardConfig | None) -> TargetBackend:
    if board is None:
        return _BACKEND
    with _BACKEND_GUARD:
        backend = _BACKENDS.get(board.debug_backend)
        if backend is None:
            backend = configured_backend(board.debug_backend)
            _BACKENDS[board.debug_backend] = backend
        return backend


def backend_for_name(name: str) -> TargetBackend:
    """Return one installed backend by stable provider identifier."""

    with _BACKEND_GUARD:
        backend = _BACKENDS.get(name)
        if backend is None:
            backend = configured_backend(name)
            _BACKENDS[name] = backend
        return backend


def _owner(handle: TargetSessionHandle) -> TargetBackend:
    if not isinstance(handle, TargetSessionHandle):
        return _BACKEND
    return handle.backend_owner or _backend_for_board(handle.board)


def configure_backend(backend: TargetBackend) -> TargetBackend:
    """Install an explicit target backend and return the previous implementation."""

    if not isinstance(backend, TargetBackend):
        raise TypeError("backend must implement TargetBackend")
    global _BACKEND
    with _BACKEND_GUARD:
        previous = _BACKEND
        _BACKEND = backend
        _BACKENDS[backend.backend_name] = backend
        return previous


def current_backend() -> TargetBackend:
    """Return the configured target backend without inferring it from an MCU name."""

    with _BACKEND_GUARD:
        return _BACKEND


def available_backends() -> tuple[TargetBackend, ...]:
    """Return configured and installed backends without vendor assumptions."""

    with _BACKEND_GUARD:
        discovered = {backend.backend_name: backend for backend in _available_backends()}
        discovered.update(_BACKENDS)
        return tuple(discovered[name] for name in sorted(discovered))


def build_session_options(
    board: BoardConfig | None,
    target: str | None,
    server_timeouts: ServerTimeoutConfig | None = None,
) -> dict[str, object] | None:
    """Expose the backend option builder for tests and wrapper compatibility."""

    return _backend_for_board(board).build_session_options(board, target, server_timeouts)


def open_session(
    *,
    board: BoardConfig | None,
    unique_id: str | None = None,
    target: str | None = None,
    server_timeouts: ServerTimeoutConfig | None = None,
    connect_mode: str | None = None,
) -> TargetSessionHandle:
    backend = _backend_for_board(board)
    handle = backend.open(
        board=board,
        unique_id=unique_id,
        target=target,
        server_timeouts=server_timeouts,
        connect_mode=connect_mode,
    )
    handle.backend_owner = backend
    return handle


def close_session(handle: TargetSessionHandle) -> None:
    _owner(handle).close(handle)


def connect_under_reset(
    *,
    board: BoardConfig | None,
    unique_id: str | None = None,
    target: str | None = None,
    server_timeouts: ServerTimeoutConfig | None = None,
) -> TargetSessionHandle:
    backend = _backend_for_board(board)
    handle = backend.connect_under_reset(
        board=board,
        unique_id=unique_id,
        target=target,
        server_timeouts=server_timeouts,
    )
    handle.backend_owner = backend
    return handle


def get_state(handle: TargetSessionHandle) -> str:
    return _owner(handle).get_state(handle)


def describe_session(handle: TargetSessionHandle) -> TargetSessionDescription:
    return _owner(handle).describe_session(handle)


def read_memory(handle: TargetSessionHandle, address: int, width_bits: int = 32) -> int:
    return _owner(handle).read_memory(handle, address, width_bits)


def read_memory_block(handle: TargetSessionHandle, address: int, length: int) -> list[int]:
    return _owner(handle).read_memory_block(handle, address, length)


def write_memory(
    handle: TargetSessionHandle, address: int, value: int, width_bits: int = 32
) -> None:
    _owner(handle).write_memory(handle, address, value, width_bits)


def read_core_register(handle: TargetSessionHandle, name: str) -> int:
    return _owner(handle).read_core_register(handle, name)


def write_core_register(handle: TargetSessionHandle, name: str, value: int) -> None:
    _owner(handle).write_core_register(handle, name, value)


def supported_core_registers(handle: TargetSessionHandle) -> tuple[str, ...]:
    return _owner(handle).supported_core_registers(handle)


def describe_core_register(
    handle: TargetSessionHandle, name: str
) -> RegisterDescriptor | None:
    return _owner(handle).describe_core_register(handle, name)


def memory_access_capabilities(handle: TargetSessionHandle) -> MemoryAccessCapabilities:
    return _owner(handle).memory_access_capabilities(handle)


def masked_register_write(
    handle: TargetSessionHandle, address: int, mask: int, value: int
) -> MaskedWriteResult:
    return _owner(handle).masked_register_write(handle, address, mask, value)


def halt(handle: TargetSessionHandle) -> None:
    _owner(handle).halt(handle)


def resume(handle: TargetSessionHandle) -> None:
    _owner(handle).resume(handle)


def step(handle: TargetSessionHandle) -> int:
    return _owner(handle).step(handle)


def reset(handle: TargetSessionHandle, *, halt_after: bool = True) -> None:
    if halt_after:
        _owner(handle).reset_and_halt(handle)
    else:
        _owner(handle).reset(handle)


def release_reset(handle: TargetSessionHandle) -> None:
    _owner(handle).release_reset(handle)


def flash_firmware(
    handle: TargetSessionHandle,
    firmware: Path,
    *,
    halt_after_reset: bool = False,
) -> Path:
    path = Path(firmware).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Firmware artifact does not exist: {path}")
    _owner(handle).flash(handle, path, halt_after_reset=halt_after_reset)
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
        if not _owner(handle).supports_recovery(handle, selected):
            raise RuntimeError("The connected target backend does not support typed mass erase.")
        _owner(handle).recover(handle)
        return "typed backend mass erase"
    raise RuntimeError(f"Unsupported recover mode: {selected}")


def supports_recovery(handle: TargetSessionHandle, mechanism: str) -> bool:
    """Check a destructive capability on the live typed backend before authorization."""

    return _owner(handle).supports_recovery(handle, mechanism)


def set_breakpoint(handle: TargetSessionHandle, address: int) -> None:
    _owner(handle).set_breakpoint(handle, address)


def breakpoint_memory_span_bytes(handle: TargetSessionHandle, address: int) -> int:
    """Return and validate the live backend's conservative breakpoint check span."""

    span = _owner(handle).breakpoint_memory_span_bytes(handle, address)
    if isinstance(span, bool) or not isinstance(span, int) or span <= 0:
        raise ValueError("target backend returned an invalid breakpoint memory span")
    return span


def remove_breakpoint(handle: TargetSessionHandle, address: int) -> None:
    _owner(handle).remove_breakpoint(handle, address)

