"""Minimal MCP server exposing pyOCD debug control to an LLM client.

Design notes
------------
* A debug session is *stateful* (halt state, breakpoints, and the live target
  connection all persist across calls), so we hold a single pyOCD ``Session``
  open for the lifetime of the server rather than opening one per tool call.
  Connecting is therefore an explicit ``connect`` tool, mirroring how an
  operator selects a specific probe and target.
* pyOCD's target access is blocking and **not thread-safe**. FastMCP may invoke
  tools concurrently, so every probe access is serialized behind a single lock.
* pyOCD calls block; for fast operations (register/memory reads) that is fine.
  Long operations such as flashing should be offloaded (e.g. ``anyio.to_thread``)
  so they don't stall the event loop — left out here to keep the starter small.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

from mcp.server.fastmcp import FastMCP

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.board_config import (
    DEFAULT_BOARD_CONFIG_DIR,
    BoardConfig,
    load_selected_board_configs,
)
from pyocd_debug_mcp.guardrails.flash_gate import resolve_flash_request
from pyocd_debug_mcp.guardrails.recover_gate import authorize_recover
from pyocd_debug_mcp.local_env import load_local_env
from pyocd_debug_mcp.probe_inventory import resolve_probe_for_board
from pyocd_debug_mcp.serial_resolver import (
    BoardLike,
    ProbeLike,
    SerialPortInfo,
    list_serial_ports,
    resolve_serial_port,
)
from pyocd_debug_mcp.services.convergence_watcher import (
    ConvergenceWatcher,
    FLASH_TOOL,
    RECOVER_TOOL,
    UART_TOOL,
)
from pyocd_debug_mcp.services.session_runtime import (
    ActionContext,
    InMemorySessionStore,
    PolicyRefusal,
    SessionRecord,
    ToolEvent,
    ToolOutcome,
    WatcherBlocked,
    utc_now_text,
)
from pyocd_debug_mcp.services import target_control
from pyocd_debug_mcp.services.symbols import read_symbol_u32 as read_symbol_u32_from_elf
from pyocd_debug_mcp.services.uart_capture import capture_uart_output, write_uart_output
from pyocd_debug_mcp.target_errors import (
    LockedTargetError,
    ProbeNotFoundError,
    ReferenceArtifactError,
    SymbolLookupError,
    TargetConnectionError,
    UnsupportedArtifactError,
)
from pyocd_debug_mcp.timeouts import (
    DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
    default_server_timeout_config,
    subprocess_timeout_stream_text,
)

load_local_env()

mcp = FastMCP("pyocd-debug")

# pyOCD is not thread-safe: serialize all probe access.
_lock = threading.Lock()
# The single live session, or None when disconnected.
_session_handle: TargetSessionHandle | None = None
_runtime_session: SessionRecord | None = None
_session_store = InMemorySessionStore()
_watcher = ConvergenceWatcher()
_staged_server_timeouts = default_server_timeout_config()
NO_BOARD_CONFIG_MESSAGE = (
    "No board config loaded for this session. Pass `board_id` to `connect` "
    "(or set PYOCD_BOARD_ID) to load boards/<board>.yaml facts."
)


class _ProbeHint:
    def __init__(self, uid: str) -> None:
        self.uid = uid


def _next_event_id() -> str:
    return f"evt-{secrets.token_hex(6)}"


def _duration_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


def _jsonable_args(values: Mapping[str, object]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in values.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            output[key] = value
        elif isinstance(value, Path):
            output[key] = str(value)
        else:
            output[key] = str(value)
    return output


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ProbeNotFoundError):
        return "probe/not-found"
    if isinstance(exc, LockedTargetError):
        return "target/locked"
    if isinstance(exc, TargetConnectionError):
        return "target/connection-failure"
    if isinstance(exc, UnsupportedArtifactError):
        return "flash/unsupported-artifact"
    if isinstance(exc, ReferenceArtifactError):
        return "flash/reference-artifact"
    if isinstance(exc, SymbolLookupError):
        return "symbols/lookup-failure"
    if (
        isinstance(exc, RuntimeError)
        and str(exc) == "Not connected to a probe. Call `connect` first."
    ):
        return "server/not-connected"
    return f"runtime/{type(exc).__name__}"


def _active_session_id() -> str | None:
    return _runtime_session.session_id if _runtime_session is not None else None


def _action_context(tool_name: str) -> ActionContext:
    return ActionContext(source="server", action_name=tool_name, session_id=_active_session_id())


def _record_event(
    tool_name: str,
    normalized_args: Mapping[str, object],
    *,
    outcome_kind: ToolOutcome,
    error_code: str | None,
    duration_ms: int,
    details: dict[str, object] | None = None,
    session: SessionRecord | None = None,
    board_id: str | None = None,
    probe_uid: str | None = None,
    route_used: str | None = None,
) -> ToolEvent:
    runtime = session or _runtime_session
    event = ToolEvent(
        event_id=_next_event_id(),
        session_id=runtime.session_id if runtime is not None else None,
        timestamp=utc_now_text(),
        tool_name=tool_name,
        board_id=board_id
        if board_id is not None
        else (runtime.board_id if runtime is not None else None),
        probe_uid=probe_uid
        if probe_uid is not None
        else (runtime.probe_uid if runtime is not None else None),
        route_used=route_used
        if route_used is not None
        else (runtime.route_used if runtime is not None else None),
        normalized_args=_jsonable_args(normalized_args),
        outcome_kind=outcome_kind,
        error_code=error_code,
        duration_ms=duration_ms,
        details=details or {},
    )
    if runtime is None:
        _session_store.append_global_event(event)
    else:
        _session_store.append_event(runtime, event)
    return event


def _format_refusal(refusal: PolicyRefusal, *, session_id: str | None) -> str:
    return f"Refused [{refusal.code}]: {refusal.message} session_id={session_id or '(none)'}"


def _format_block(blocked: WatcherBlocked, *, session_id: str | None) -> str:
    return f"Blocked [{blocked.code}]: {blocked.message} session_id={session_id or '(none)'}"


def _refuse_invalid_argument(
    tool_name: str,
    normalized_args: Mapping[str, object],
    *,
    code: str,
    message: str,
    started: float,
    session: SessionRecord | None,
) -> str:
    refusal = PolicyRefusal(code, message)
    _record_event(
        tool_name,
        normalized_args,
        outcome_kind=ToolOutcome.REFUSED,
        error_code=refusal.code,
        duration_ms=_duration_ms(started),
        details={"message": refusal.message},
        session=session,
    )
    return _format_refusal(refusal, session_id=_active_session_id())


def _record_blocked_event(
    tool_name: str,
    normalized_args: Mapping[str, object],
    blocked: WatcherBlocked,
    *,
    started: float,
    session: SessionRecord | None,
) -> ToolEvent:
    return _record_event(
        tool_name,
        normalized_args,
        outcome_kind=ToolOutcome.BLOCKED,
        error_code=blocked.code,
        duration_ms=_duration_ms(started),
        details={"message": blocked.message},
        session=session,
    )


def _parse_int(text: str) -> int:
    """Parse an int from a string, accepting hex (0x...), binary, or decimal."""
    return int(text, 0)


def _word_size_is_valid(word_size: int) -> bool:
    return word_size in {8, 16, 32}


def _run_cmd(
    cmd: list[str],
    timeout_seconds: float = DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        executable = cmd[0] if cmd else "<unknown>"
        return 127, "", f"command not found: {executable}"
    except subprocess.TimeoutExpired as exc:
        return (
            124,
            subprocess_timeout_stream_text(exc.stdout),
            f"command timed out after {timeout_seconds:.0f}s: {' '.join(cmd)}",
        )
    return result.returncode, result.stdout or "", result.stderr or ""


def resolve_board_config(board_id: str | None, board_config: str | None) -> BoardConfig | None:
    """Load one board definition through the shared loader, or None if unselected.

    This is the server's single path to ``boards/<board>.yaml`` — the same loader
    the Stage 0 CLI uses — so a custom ST/nRF board's facts (pyOCD target, recover
    policy, silicon id, baud) reach the MCP tools, not just the CLI.

    ``board_id``/``board_config`` fall back to the ``PYOCD_BOARD_ID`` /
    ``PYOCD_BOARD_CONFIG`` environment variables (the stdio-launch config
    channel). Returns ``None`` when no board is named, so ``connect`` still works
    with a raw target. Raises ``ConfigError`` if a named board cannot be found or
    a config file is malformed.
    """
    bid = (board_id or os.environ.get("PYOCD_BOARD_ID") or "").strip()
    if not bid:
        return None
    extra = board_config or os.environ.get("PYOCD_BOARD_CONFIG") or None
    extra_paths = [Path(extra)] if extra else []
    boards = load_selected_board_configs(
        DEFAULT_BOARD_CONFIG_DIR,
        extra_paths=extra_paths,
        requested_ids=[bid],
    )
    return boards[0]


def format_board_info(b: BoardConfig) -> str:
    """Render a loaded board definition's facts as a stable text block."""
    lines = [
        f"board_id: {b.board_id}",
        f"display_name: {b.display_name}",
        f"mcu_family: {b.mcu_family}",
        f"probe_family: {b.probe_family}",
        f"pyocd_target: {b.pyocd_target}",
        f"default_baudrate: {b.default_baudrate}",
        f"test_read_address: 0x{b.test_addr:08X}",
        f"requires_recover_validation: {b.requires_recover_validation}",
        f"recover_mode: {b.recover_mode or '(none)'}",
    ]
    if b.silicon_id_addr is not None and b.silicon_id_expected is not None:
        width_nibbles = b.silicon_id_width_bits // 4
        lines.append(
            f"silicon_id: addr=0x{b.silicon_id_addr:08X} "
            f"expected=0x{b.silicon_id_expected:0{width_nibbles}X} "
            f"({b.silicon_id_label or 'silicon identity'})"
        )
    if b.uart_note:
        lines.append(f"uart_note: {b.uart_note}")
    return "\n".join(lines)


def build_session_options(
    board: BoardConfig | None, target: str | None
) -> dict[str, object] | None:
    """Compatibility wrapper around the shared target-control option builder."""
    return target_control.build_session_options(board, target)


def _should_bypass_jlink_probe_resolution(
    board: BoardConfig | None,
    *,
    platform_name: str | None = None,
) -> bool:
    if board is None or board.probe_family != "jlink":
        return False
    current_platform = platform_name or sys.platform
    return current_platform.startswith("win")


def _resolve_probe_uid_for_connect(
    board: BoardConfig | None,
    unique_id: str | None,
) -> str | None:
    if unique_id is not None:
        return unique_id
    env_uid = os.environ.get("PYOCD_PROBE_UID") or None
    if env_uid is not None:
        return env_uid
    if board is None:
        return None

    allow_subprocess_fallback = True
    if _should_bypass_jlink_probe_resolution(board):
        # On this Windows host, the risky path is the subprocess fallback
        # behind probe resolution, not the direct pyOCD API enumeration. Keep
        # using API-derived UIDs when available so J-Link stdio attaches still
        # work on boards like nrf52840dk, but never pre-run the subprocess
        # probe-listing path for implicit J-Link selection.
        allow_subprocess_fallback = False

    resolution = resolve_probe_for_board(
        board,
        run_cmd=_run_cmd,
        allow_single_fallback=True,
        allow_subprocess_fallback=allow_subprocess_fallback,
    )
    if resolution.probe is None:
        if not allow_subprocess_fallback:
            return None
        raise RuntimeError(f"Probe resolution failed for {board.display_name}: {resolution.note}")
    if not resolution.probe.uid:
        raise RuntimeError(
            f"Probe resolution for {board.display_name} did not yield a unique id. "
            "Rerun with unique_id=... or set PYOCD_PROBE_UID."
        )
    return resolution.probe.uid


def _handle() -> TargetSessionHandle:
    """Return the live session handle or raise if not connected."""
    if _session_handle is None:
        raise RuntimeError("Not connected to a probe. Call `connect` first.")
    return _session_handle


@mcp.tool()
def connect(
    unique_id: str | None = None,
    target: str | None = None,
    board_id: str | None = None,
    board_config: str | None = None,
) -> str:
    """Open a debug session to a connected probe.

    Args:
        unique_id: Whole or partial probe serial/unique ID to select a specific
            probe. Omit when exactly one probe is attached. Defaults to the
            ``PYOCD_PROBE_UID`` environment variable if unset.
        target: Target type override, e.g. "stm32f407vg" or "nrf52833". Takes
            precedence over a board config. Omit to use the selected board's
            target (when ``board_id`` is given), else the ``PYOCD_TARGET``
            environment variable, else pyOCD auto-detection.
        board_id: Load facts from ``boards/<board_id>.yaml`` through the shared
            board-config loader — pyOCD target, recover policy, silicon id, baud.
            Lets a custom ST/nRF board connect by id with no hand-passed target;
            call ``get_board_info`` to see the loaded facts. Defaults to the
            ``PYOCD_BOARD_ID`` environment variable.
        board_config: Path to an extra board-config file outside the tracked
            ``boards/`` directory, for a custom board. Defaults to the
            ``PYOCD_BOARD_CONFIG`` environment variable.
    """
    global _session_handle, _runtime_session
    with _lock:
        started = time.monotonic()
        normalized_args: dict[str, object] = {
            "unique_id": unique_id,
            "target": target,
            "board_id": board_id,
            "board_config": board_config,
        }
        if _session_handle is not None:
            result = "Already connected. Call `disconnect` first to switch probes."
            _record_event(
                "connect",
                normalized_args,
                outcome_kind=ToolOutcome.SUCCESS,
                error_code=None,
                duration_ms=_duration_ms(started),
                details={"status": "already-connected"},
            )
            return result

        board = None
        uid = None
        tgt = None
        try:
            board = resolve_board_config(board_id, board_config)
            uid = _resolve_probe_uid_for_connect(board, unique_id)
            tgt = (
                target
                or (board.pyocd_target if board else None)
                or os.environ.get("PYOCD_TARGET")
                or None
            )
            handle = target_control.open_session(
                board=board,
                unique_id=uid,
                target=tgt,
                server_timeouts=_staged_server_timeouts,
            )
        except Exception as exc:  # noqa: BLE001 - preserve the original connect error
            _record_event(
                "connect",
                normalized_args,
                outcome_kind=ToolOutcome.FAILED,
                error_code=_error_code(exc),
                duration_ms=_duration_ms(started),
                details={"message": str(exc)[:300]},
                board_id=board.board_id if board else None,
                probe_uid=uid,
                route_used=None,
            )
            raise

        _session_handle = handle
        _runtime_session = _session_store.start_session(
            board_id=board.board_id if board else None,
            probe_uid=handle.probe_uid,
            route_used=handle.route_used,
        )
        suffix = f" [board config: {board.board_id}]" if board else ""
        board_name = handle.session.board.name if handle.session.board is not None else "<unknown>"
        result = (
            f"Connected to board '{board_name}' via probe "
            f"{handle.probe_uid or '(unknown)'} via {handle.route_used}.{suffix} "
            f"session_id={_runtime_session.session_id}"
        )
        _record_event(
            "connect",
            normalized_args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=_duration_ms(started),
            details={"board_name": board_name},
            session=_runtime_session,
        )
        return result


@mcp.tool()
def disconnect() -> str:
    """Close the active debug session and release the probe."""
    global _session_handle, _runtime_session
    with _lock:
        if _session_handle is None:
            started = time.monotonic()
            result = "Not connected."
            _record_event(
                "disconnect",
                {},
                outcome_kind=ToolOutcome.SUCCESS,
                error_code=None,
                duration_ms=_duration_ms(started),
                details={"status": "not-connected"},
            )
            return result

        started = time.monotonic()
        handle = _session_handle
        runtime_session = _runtime_session
        try:
            target_control.close_session(handle)
        except Exception as exc:  # noqa: BLE001 - preserve the original disconnect error
            _record_event(
                "disconnect",
                {},
                outcome_kind=ToolOutcome.FAILED,
                error_code=_error_code(exc),
                duration_ms=_duration_ms(started),
                details={"message": str(exc)[:300]},
                session=runtime_session,
            )
            raise

        _record_event(
            "disconnect",
            {},
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=_duration_ms(started),
            session=runtime_session,
        )
        if runtime_session is not None:
            _session_store.close_session(runtime_session)
        _session_handle = None
        _runtime_session = None
        return "Disconnected."


@mcp.tool()
def get_board_info() -> str:
    """Return the facts from the board config the session was opened with.

    Reports the ``boards/<board>.yaml`` definition active for this session —
    pyOCD target, MCU and probe family, recover policy, silicon-id expectation,
    default UART baud, and the smoke-test read address. Returns a notice when
    ``connect`` was called without a ``board_id`` (raw-target mode), where these
    facts were not loaded.
    """
    with _lock:

        def operation() -> str:
            handle = _session_handle
            if handle is None:
                return "Not connected. Call `connect` first."
            b = handle.board
            if b is None:
                return NO_BOARD_CONFIG_MESSAGE
            return format_board_info(b)

        return _run_logged_tool("get_board_info", {}, operation)


def _require_loaded_board(handle: TargetSessionHandle) -> BoardConfig:
    if handle.board is None:
        raise RuntimeError(NO_BOARD_CONFIG_MESSAGE)
    return handle.board


def _resolve_serial_port_for_session(
    handle: TargetSessionHandle,
    *,
    override: str | None,
) -> SerialPortInfo:
    board = _require_loaded_board(handle)
    ports = list_serial_ports()
    if ports is None:
        raise RuntimeError("pyserial is not installed")
    if not ports:
        raise RuntimeError("No serial ports detected")

    probe = _ProbeHint(handle.probe_uid) if handle.probe_uid else None
    resolution = resolve_serial_port(
        board=cast(BoardLike, board),
        ports=ports,
        probe=cast(ProbeLike | None, probe),
        override=override,
        allow_single_fallback=len(ports) == 1,
        run_cmd=_run_cmd,
        interactive=False,
    )
    if resolution.port is None:
        raise RuntimeError(f"Serial port resolution failed: {resolution.note}")
    return resolution.port


def _handle_mutation_event(event: ToolEvent) -> None:
    if _runtime_session is None:
        return
    decision = _watcher.observe_event(_runtime_session, event)
    if decision is not None:
        _session_store.set_block(
            _runtime_session,
            decision.action_family,
            decision.code,
            decision.message,
        )


def _run_logged_tool(
    tool_name: str,
    normalized_args: Mapping[str, object],
    operation: Callable[[], str],
) -> str:
    started = time.monotonic()
    try:
        result = operation()
    except Exception as exc:  # noqa: BLE001 - preserve the original tool failure
        _record_event(
            tool_name,
            normalized_args,
            outcome_kind=ToolOutcome.FAILED,
            error_code=_error_code(exc),
            duration_ms=_duration_ms(started),
            details={"message": str(exc)[:300]},
        )
        raise

    _record_event(
        tool_name,
        normalized_args,
        outcome_kind=ToolOutcome.SUCCESS,
        error_code=None,
        duration_ms=_duration_ms(started),
    )
    return result


def _complete_effect(effect: Callable[[], None], result: str) -> str:
    effect()
    return result


@mcp.tool()
def get_state() -> str:
    """Return the current core run state (e.g. HALTED, RUNNING, RESET)."""
    with _lock:

        def operation() -> str:
            return target_control.get_state(_handle())

        return _run_logged_tool("get_state", {}, operation)


@mcp.tool()
def halt() -> str:
    """Halt the core."""
    with _lock:

        def operation() -> str:
            return _complete_effect(lambda: target_control.halt(_handle()), "Halted.")

        return _run_logged_tool("halt", {}, operation)


@mcp.tool()
def resume() -> str:
    """Resume execution of the core."""
    with _lock:

        def operation() -> str:
            return _complete_effect(lambda: target_control.resume(_handle()), "Resumed.")

        return _run_logged_tool("resume", {}, operation)


@mcp.tool()
def step() -> str:
    """Single-step one instruction and return the new program counter."""
    with _lock:

        def operation() -> str:
            return f"Stepped. pc=0x{target_control.step(_handle()):08X}"

        return _run_logged_tool("step", {}, operation)


@mcp.tool()
def reset(halt_after: bool = True) -> str:
    """Reset the target.

    Args:
        halt_after: If True, halt at the reset vector (reset-and-halt).
            If False, reset and let the target run.
    """
    with _lock:

        def operation() -> str:
            return _complete_effect(
                lambda: target_control.reset(_handle(), halt_after=halt_after),
                "Reset and halted." if halt_after else "Reset and running.",
            )

        return _run_logged_tool("reset", {"halt_after": halt_after}, operation)


@mcp.tool()
def read_core_register(name: str) -> str:
    """Read a core register by name (e.g. "pc", "sp", "r0", "xpsr").

    Returns the value as a hex string.
    """
    with _lock:

        def operation() -> str:
            return f"0x{target_control.read_core_register(_handle(), name):08X}"

        return _run_logged_tool("read_core_register", {"name": name}, operation)


@mcp.tool()
def write_core_register(name: str, value: str) -> str:
    """Write a core register by name. ``value`` may be hex (0x...) or decimal."""
    with _lock:

        def operation() -> str:
            return _complete_effect(
                lambda: target_control.write_core_register(_handle(), name, _parse_int(value)),
                f"Wrote {value} to {name}.",
            )

        return _run_logged_tool("write_core_register", {"name": name, "value": value}, operation)


@mcp.tool()
def read_memory(address: str, word_size: int = 32) -> str:
    """Read a single value from memory.

    Args:
        address: Memory address, hex (0x...) or decimal.
        word_size: Transfer size in bits: 8, 16, or 32.
    """
    with _lock:
        started = time.monotonic()
        normalized_args = {"address": address, "word_size": word_size}
        if not _word_size_is_valid(word_size):
            return _refuse_invalid_argument(
                "read_memory",
                normalized_args,
                code="memory/invalid-word-size",
                message="word_size must be one of: 8, 16, 32.",
                started=started,
                session=_runtime_session,
            )

        def operation() -> str:
            value = target_control.read_memory(_handle(), _parse_int(address), word_size)
            return f"0x{value:0{word_size // 4}X}"

        return _run_logged_tool(
            "read_memory",
            normalized_args,
            operation,
        )


@mcp.tool()
def read_memory_block(address: str, length: int) -> str:
    """Read ``length`` bytes from memory starting at ``address``.

    Returns the bytes as a space-separated hex string.
    """
    with _lock:
        started = time.monotonic()
        normalized_args = {"address": address, "length": length}
        if length <= 0:
            return _refuse_invalid_argument(
                "read_memory_block",
                normalized_args,
                code="memory/invalid-length",
                message="length must be > 0.",
                started=started,
                session=_runtime_session,
            )

        def operation() -> str:
            values = target_control.read_memory_block(_handle(), _parse_int(address), length)
            return " ".join(f"{byte:02X}" for byte in values)

        return _run_logged_tool("read_memory_block", normalized_args, operation)


@mcp.tool()
def read_symbol_u32(elf_path: str, symbol_name: str) -> str:
    """Resolve ``symbol_name`` in ``elf_path`` and read its 32-bit value from target memory."""
    with _lock:
        normalized_args = {"elf_path": elf_path, "symbol_name": symbol_name}

        def operation() -> str:
            resolved = read_symbol_u32_from_elf(_handle(), elf_path, symbol_name)
            if resolved.value_u32 is None:  # pragma: no cover - service always populates this field
                raise RuntimeError(
                    f"Resolved symbol '{symbol_name}' did not produce a 32-bit value."
                )
            resolved_path = Path(elf_path).expanduser().resolve()
            return (
                f"Symbol {resolved.name} from {resolved_path} "
                f"@0x{resolved.address:08X} size={resolved.size} type={resolved.type} "
                f"value_u32=0x{resolved.value_u32:08X}"
            )

        return _run_logged_tool("read_symbol_u32", normalized_args, operation)


@mcp.tool()
def write_memory(address: str, value: str, word_size: int = 32) -> str:
    """Write a single value to memory.

    Args:
        address: Memory address, hex (0x...) or decimal.
        value: Value to write, hex (0x...) or decimal.
        word_size: Transfer size in bits: 8, 16, or 32.
    """
    with _lock:
        started = time.monotonic()
        normalized_args = {"address": address, "value": value, "word_size": word_size}
        if not _word_size_is_valid(word_size):
            return _refuse_invalid_argument(
                "write_memory",
                normalized_args,
                code="memory/invalid-word-size",
                message="word_size must be one of: 8, 16, 32.",
                started=started,
                session=_runtime_session,
            )

        def operation() -> str:
            return _complete_effect(
                lambda: target_control.write_memory(
                    _handle(),
                    _parse_int(address),
                    _parse_int(value),
                    word_size,
                ),
                f"Wrote {value} to {address}.",
            )

        return _run_logged_tool(
            "write_memory",
            normalized_args,
            operation,
        )


@mcp.tool()
def set_breakpoint(address: str) -> str:
    """Set a hardware/software breakpoint at ``address``."""
    with _lock:

        def operation() -> str:
            return _complete_effect(
                lambda: target_control.set_breakpoint(_handle(), _parse_int(address)),
                f"Breakpoint set at {address}.",
            )

        return _run_logged_tool("set_breakpoint", {"address": address}, operation)


@mcp.tool()
def remove_breakpoint(address: str) -> str:
    """Remove the breakpoint at ``address``."""
    with _lock:

        def operation() -> str:
            return _complete_effect(
                lambda: target_control.remove_breakpoint(_handle(), _parse_int(address)),
                f"Breakpoint removed at {address}.",
            )

        return _run_logged_tool("remove_breakpoint", {"address": address}, operation)


@mcp.tool()
def flash_firmware(path: str | None = None, halt_after_reset: bool = False) -> str:
    """Flash firmware through the shared target-control service layer.

    Args:
        path: Optional explicit artifact path. When omitted, resolve the default
            flash artifact for the connected session's loaded board config.
            Returns the resolved path in the success text.
        halt_after_reset: If True, leave the target halted after flashing.
            If False, reset and let it run.
    """
    with _lock:
        started = time.monotonic()
        runtime = _runtime_session
        normalized_args: dict[str, object] = {
            "path": path,
            "halt_after_reset": halt_after_reset,
            "artifact_source": "default" if path is None else "explicit",
            "artifact_path": path,
        }
        if runtime is not None:
            try:
                _watcher.ensure_allowed(runtime, FLASH_TOOL)
            except WatcherBlocked as blocked:
                _record_blocked_event(
                    "flash_firmware",
                    normalized_args,
                    blocked,
                    started=started,
                    session=runtime,
                )
                return _format_block(blocked, session_id=runtime.session_id)

        handle = _session_handle
        try:
            request = resolve_flash_request(
                handle,
                explicit_path=path,
                action_context=_action_context("flash_firmware"),
            )
            active_handle = _handle()
            normalized_args.update(request.identity.as_log_fields())
            flashed = target_control.flash_firmware(
                active_handle,
                request.artifact_path,
                halt_after_reset=halt_after_reset,
            )
        except PolicyRefusal as exc:
            event = _record_event(
                "flash_firmware",
                normalized_args,
                outcome_kind=ToolOutcome.REFUSED,
                error_code=exc.code,
                duration_ms=_duration_ms(started),
                details={"message": exc.message},
                session=runtime,
            )
            if runtime is not None:
                _handle_mutation_event(event)
            return _format_refusal(exc, session_id=_active_session_id())
        except Exception as exc:  # noqa: BLE001 - preserve backend failure text
            event = _record_event(
                "flash_firmware",
                normalized_args,
                outcome_kind=ToolOutcome.FAILED,
                error_code=_error_code(exc),
                duration_ms=_duration_ms(started),
                details={"message": str(exc)[:300]},
                session=runtime,
            )
            if runtime is not None:
                _handle_mutation_event(event)
            raise

        state = "halted" if halt_after_reset else "running"
        event = _record_event(
            "flash_firmware",
            normalized_args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=_duration_ms(started),
            details={"target_state": state},
            session=runtime,
        )
        if runtime is not None:
            _handle_mutation_event(event)
        return f"Flashed {flashed} via {active_handle.route_used}; target left {state}."


@mcp.tool()
def read_serial(
    expected_text: str | None = None,
    read_seconds: float = 3.0,
    baudrate: int | None = None,
    port: str | None = None,
    reset_on_open: bool = False,
) -> str:
    """Capture bounded UART output through the shared serial-resolution and UART services.

    Defaults come from the connected board config only for baudrate and
    serial-port selection heuristics. ``expected_text=None`` means no explicit
    text expectation: any observed UART output counts as a match.
    Set ``port`` to override serial resolution explicitly. Set ``reset_on_open``
    to trigger a target reset immediately after the UART port opens so early
    boot text is captured deterministically.
    """
    with _lock:
        started = time.monotonic()
        runtime = _runtime_session
        normalized_args: dict[str, object] = {
            "port": port,
            "baudrate": baudrate,
            "expected_text": expected_text,
            "read_seconds": read_seconds,
            "reset_on_open": reset_on_open,
        }
        if read_seconds <= 0:
            refusal = PolicyRefusal("uart/invalid-read-seconds", "read_seconds must be > 0.")
            _record_event(
                "read_serial",
                normalized_args,
                outcome_kind=ToolOutcome.REFUSED,
                error_code=refusal.code,
                duration_ms=_duration_ms(started),
                details={"message": refusal.message},
                session=runtime,
            )
            return _format_refusal(refusal, session_id=_active_session_id())
        if baudrate is not None and baudrate <= 0:
            refusal = PolicyRefusal("uart/invalid-baudrate", "baudrate must be > 0.")
            _record_event(
                "read_serial",
                normalized_args,
                outcome_kind=ToolOutcome.REFUSED,
                error_code=refusal.code,
                duration_ms=_duration_ms(started),
                details={"message": refusal.message},
                session=runtime,
            )
            return _format_refusal(refusal, session_id=_active_session_id())
        if runtime is not None:
            try:
                _watcher.ensure_allowed(runtime, UART_TOOL)
            except WatcherBlocked as blocked:
                _record_blocked_event(
                    "read_serial",
                    normalized_args,
                    blocked,
                    started=started,
                    session=runtime,
                )
                return _format_block(blocked, session_id=runtime.session_id)

        handle = _handle()
        if handle.board is None:
            return NO_BOARD_CONFIG_MESSAGE

        board = handle.board
        resolved_port = _resolve_serial_port_for_session(handle, override=port)
        resolved_baudrate = baudrate or board.default_baudrate
        resolved_expected_text = expected_text
        normalized_args = {
            "port": resolved_port.device,
            "baudrate": resolved_baudrate,
            "expected_text": resolved_expected_text,
            "read_seconds": read_seconds,
            "reset_on_open": reset_on_open,
        }

        on_port_open: Callable[[], None] | None = None
        if reset_on_open:

            def reset_on_open_callback() -> None:
                target_control.reset(handle, halt_after=False)

            on_port_open = reset_on_open_callback

        capture = capture_uart_output(
            resolved_port.device,
            resolved_baudrate,
            read_seconds,
            resolved_expected_text,
            on_port_open=on_port_open,
        )
        expectation_label = (
            f"expected='{resolved_expected_text}'"
            if resolved_expected_text is not None
            else "expected=(none)"
        )
        verdict = "matched" if capture.matched else "did not match"
        excerpt = capture.excerpt or "(none)"
        result = (
            f"UART {verdict} on {resolved_port.device} at {resolved_baudrate} baud via {handle.route_used}; "
            f"{expectation_label}; reopen_count={capture.reopen_count}; "
            f"duration={capture.duration_seconds:.2f}s; excerpt={excerpt}"
        )
        event = _record_event(
            "read_serial",
            normalized_args,
            outcome_kind=ToolOutcome.SUCCESS if capture.matched else ToolOutcome.FAILED,
            error_code=None if capture.matched else "uart/no-match",
            duration_ms=_duration_ms(started),
            details={
                "matched": capture.matched,
                "reopen_count": capture.reopen_count,
                "capture_duration_seconds": round(capture.duration_seconds, 3),
                "excerpt": excerpt,
            },
            session=runtime,
        )
        if runtime is not None:
            _handle_mutation_event(event)
        return result


@mcp.tool()
def write_serial(
    text: str,
    baudrate: int | None = None,
    port: str | None = None,
    append_newline: bool = False,
    timeout_seconds: float = 1.0,
) -> str:
    """Write bounded UTF-8 text to the connected board UART.

    The tool uses the same board-aware serial-port resolution as ``read_serial``.
    It never executes host commands and only writes bytes to the resolved UART
    transport for the active connected session. Set ``port`` to override serial
    resolution explicitly. Set ``append_newline`` when the target firmware
    expects line-oriented UART input.
    """
    with _lock:
        started = time.monotonic()
        runtime = _runtime_session
        normalized_args: dict[str, object] = {
            "port": port,
            "baudrate": baudrate,
            "text_length": len(text),
            "append_newline": append_newline,
            "timeout_seconds": timeout_seconds,
        }
        if baudrate is not None and baudrate <= 0:
            refusal = PolicyRefusal("uart/invalid-baudrate", "baudrate must be > 0.")
            _record_event(
                "write_serial",
                normalized_args,
                outcome_kind=ToolOutcome.REFUSED,
                error_code=refusal.code,
                duration_ms=_duration_ms(started),
                details={"message": refusal.message},
                session=runtime,
            )
            return _format_refusal(refusal, session_id=_active_session_id())
        if timeout_seconds <= 0:
            refusal = PolicyRefusal("uart/invalid-timeout", "timeout_seconds must be > 0.")
            _record_event(
                "write_serial",
                normalized_args,
                outcome_kind=ToolOutcome.REFUSED,
                error_code=refusal.code,
                duration_ms=_duration_ms(started),
                details={"message": refusal.message},
                session=runtime,
            )
            return _format_refusal(refusal, session_id=_active_session_id())
        if runtime is not None:
            try:
                _watcher.ensure_allowed(runtime, UART_TOOL)
            except WatcherBlocked as blocked:
                _record_blocked_event(
                    "write_serial",
                    normalized_args,
                    blocked,
                    started=started,
                    session=runtime,
                )
                return _format_block(blocked, session_id=runtime.session_id)

        handle = _handle()
        if handle.board is None:
            return NO_BOARD_CONFIG_MESSAGE

        board = handle.board
        resolved_port = _resolve_serial_port_for_session(handle, override=port)
        resolved_baudrate = baudrate or board.default_baudrate
        payload_text = f"{text}\n" if append_newline else text
        payload = payload_text.encode("utf-8")
        normalized_args = {
            "port": resolved_port.device,
            "baudrate": resolved_baudrate,
            "text_length": len(text),
            "bytes_to_write": len(payload),
            "append_newline": append_newline,
            "timeout_seconds": timeout_seconds,
        }
        write_result = write_uart_output(
            resolved_port.device,
            resolved_baudrate,
            payload,
            timeout_seconds=timeout_seconds,
        )
        result = (
            f"UART wrote {write_result.bytes_written} byte(s) on {resolved_port.device} "
            f"at {resolved_baudrate} baud via {handle.route_used}; "
            f"duration={write_result.duration_seconds:.2f}s"
        )
        event = _record_event(
            "write_serial",
            normalized_args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=_duration_ms(started),
            details={
                "bytes_written": write_result.bytes_written,
                "write_duration_seconds": round(write_result.duration_seconds, 3),
            },
            session=runtime,
        )
        if runtime is not None:
            _handle_mutation_event(event)
        return result


@mcp.tool()
def unlock_recover(confirm: bool = False) -> str:
    """Run the shared recover/unlock path for the connected board.

    This is a destructive action. When ``confirm`` is false the tool refuses
    without touching hardware. Boards whose tracked config has no supported
    ``recover_mode`` fail deterministically instead of pretending success.
    """
    with _lock:
        started = time.monotonic()
        runtime = _runtime_session
        if runtime is not None:
            try:
                _watcher.ensure_allowed(runtime, RECOVER_TOOL)
            except WatcherBlocked as blocked:
                blocked_args: dict[str, object] = {"confirm": confirm}
                _record_blocked_event(
                    "unlock_recover",
                    blocked_args,
                    blocked,
                    started=started,
                    session=runtime,
                )
                return _format_block(blocked, session_id=runtime.session_id)

        handle = _session_handle
        normalized_args: dict[str, object] = {"confirm": confirm}
        try:
            authorize_recover(
                handle,
                confirm=confirm,
                recover_already_completed=bool(runtime and runtime.recover_completed),
                action_context=_action_context("unlock_recover"),
            )
            active_handle = _handle()
            backend = target_control.recover_target(active_handle)
        except PolicyRefusal as exc:
            event = _record_event(
                "unlock_recover",
                normalized_args,
                outcome_kind=ToolOutcome.REFUSED,
                error_code=exc.code,
                duration_ms=_duration_ms(started),
                details={"message": exc.message},
                session=runtime,
            )
            if runtime is not None:
                _handle_mutation_event(event)
            return _format_refusal(exc, session_id=_active_session_id())
        except Exception as exc:  # noqa: BLE001 - preserve backend failure text
            event = _record_event(
                "unlock_recover",
                normalized_args,
                outcome_kind=ToolOutcome.FAILED,
                error_code=_error_code(exc),
                duration_ms=_duration_ms(started),
                details={"message": str(exc)[:300]},
                session=runtime,
            )
            if runtime is not None:
                _handle_mutation_event(event)
            raise

        if runtime is not None:
            _session_store.mark_recover_completed(runtime)
        event = _record_event(
            "unlock_recover",
            normalized_args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=_duration_ms(started),
            details={"backend": backend},
            session=runtime,
        )
        if runtime is not None:
            _handle_mutation_event(event)
        board = active_handle.board
        assert board is not None
        return (
            f"Recover completed via {backend} on {board.board_id} via {active_handle.route_used}."
        )


def main() -> None:
    """Console entry point. Runs the server over stdio transport by default."""
    mcp.run()


if __name__ == "__main__":
    main()
