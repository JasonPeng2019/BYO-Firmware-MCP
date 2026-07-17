"""Board-scoped UART action handlers with preserved validation and event logging."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pyocd_debug_mcp.kernel.operations import wrap_layer2_response
from pyocd_debug_mcp.kernel.finalizers import OnExitFinalizer
from pyocd_debug_mcp.services.session_runtime import (
    PolicyRefusal,
    SessionRecord,
    ToolOutcome,
    WatcherBlocked,
)


@dataclass(frozen=True, slots=True)
class SerialToolServices:
    runtime_for: Callable[[str], SessionRecord | None]
    active_session_id: Callable[[str], str | None]
    duration_ms: Callable[[float], int]
    record_event: Callable[..., object]
    record_blocked_event: Callable[..., object]
    format_refusal: Callable[..., str]
    format_block: Callable[..., str]
    ensure_uart_allowed: Callable[[SessionRecord], None]
    handle_for: Callable[[str], Any]
    resolve_port: Callable[..., Any]
    capture_uart: Callable[..., Any]
    write_uart: Callable[..., Any]
    reset_target: Callable[[Any], None]
    handle_mutation_event: Callable[[str, object], None]
    no_board_config_message: str


def _record_refusal(
    services: SerialToolServices,
    tool_name: str,
    board_id: str,
    normalized_args: Mapping[str, object],
    refusal: PolicyRefusal,
    started: float,
    runtime: SessionRecord | None,
) -> str:
    services.record_event(
        tool_name,
        normalized_args,
        outcome_kind=ToolOutcome.REFUSED,
        error_code=refusal.code,
        duration_ms=services.duration_ms(started),
        details={"message": refusal.message},
        board_id=board_id,
        session=runtime,
    )
    return services.format_refusal(
        refusal,
        session_id=services.active_session_id(board_id),
    )


def read_serial(
    services: SerialToolServices,
    board_id: str,
    expected_text: str | None = None,
    read_seconds: float = 3.0,
    baudrate: int | None = None,
    port: str | None = None,
    reset_on_open: bool = False,
) -> str:
    """Capture bounded UART output for one planned logical board."""

    started = time.monotonic()
    runtime = services.runtime_for(board_id)
    normalized_args: dict[str, object] = {
        "board_id": board_id,
        "port": port,
        "baudrate": baudrate,
        "expected_text": expected_text,
        "read_seconds": read_seconds,
        "reset_on_open": reset_on_open,
    }
    if read_seconds <= 0:
        return _record_refusal(
            services,
            "read_serial",
            board_id,
            normalized_args,
            PolicyRefusal("uart/invalid-read-seconds", "read_seconds must be > 0."),
            started,
            runtime,
        )
    if baudrate is not None and baudrate <= 0:
        return _record_refusal(
            services,
            "read_serial",
            board_id,
            normalized_args,
            PolicyRefusal("uart/invalid-baudrate", "baudrate must be > 0."),
            started,
            runtime,
        )
    if runtime is not None:
        try:
            services.ensure_uart_allowed(runtime)
        except WatcherBlocked as blocked:
            services.record_blocked_event(
                "read_serial",
                normalized_args,
                blocked,
                started=started,
                board_id=board_id,
                session=runtime,
            )
            return services.format_block(blocked, session_id=runtime.session_id)

    handle = services.handle_for(board_id)
    if handle.board is None:
        return services.no_board_config_message

    board = handle.board
    resolved_port = services.resolve_port(handle, override=port)
    resolved_baudrate = baudrate or board.default_baudrate
    normalized_args = {
        "board_id": board_id,
        "port": resolved_port.device,
        "baudrate": resolved_baudrate,
        "expected_text": expected_text,
        "read_seconds": read_seconds,
        "reset_on_open": reset_on_open,
    }
    on_port_open: Callable[[], None] | None = None
    if reset_on_open:

        def reset_on_open_callback() -> None:
            services.reset_target(handle)

        on_port_open = reset_on_open_callback

    capture = services.capture_uart(
        resolved_port.device,
        resolved_baudrate,
        read_seconds,
        expected_text,
        on_port_open=on_port_open,
    )
    expectation_label = (
        f"expected='{expected_text}'" if expected_text is not None else "expected=(none)"
    )
    verdict = "matched" if capture.matched else "did not match"
    excerpt = capture.excerpt or "(none)"
    result = (
        f"UART {verdict} on {resolved_port.device} at {resolved_baudrate} baud via "
        f"{handle.route_used}; {expectation_label}; reopen_count={capture.reopen_count}; "
        f"duration={capture.duration_seconds:.2f}s; excerpt={excerpt}"
    )
    event = services.record_event(
        "read_serial",
        normalized_args,
        outcome_kind=ToolOutcome.SUCCESS if capture.matched else ToolOutcome.FAILED,
        error_code=None if capture.matched else "uart/no-match",
        duration_ms=services.duration_ms(started),
        details={
            "matched": capture.matched,
            "reopen_count": capture.reopen_count,
            "capture_duration_seconds": round(capture.duration_seconds, 3),
            "excerpt": excerpt,
        },
        board_id=board_id,
        session=runtime,
    )
    if runtime is not None:
        services.handle_mutation_event(board_id, event)
    return result


def write_serial(
    services: SerialToolServices,
    board_id: str,
    text: str,
    baudrate: int | None = None,
    port: str | None = None,
    append_newline: bool = False,
    timeout_seconds: float = 1.0,
) -> str:
    """Write bounded UTF-8 text to one planned logical board's UART."""

    started = time.monotonic()
    runtime = services.runtime_for(board_id)
    normalized_args: dict[str, object] = {
        "board_id": board_id,
        "port": port,
        "baudrate": baudrate,
        "text_length": len(text),
        "append_newline": append_newline,
        "timeout_seconds": timeout_seconds,
    }
    if baudrate is not None and baudrate <= 0:
        return _record_refusal(
            services,
            "write_serial",
            board_id,
            normalized_args,
            PolicyRefusal("uart/invalid-baudrate", "baudrate must be > 0."),
            started,
            runtime,
        )
    if timeout_seconds <= 0:
        return _record_refusal(
            services,
            "write_serial",
            board_id,
            normalized_args,
            PolicyRefusal("uart/invalid-timeout", "timeout_seconds must be > 0."),
            started,
            runtime,
        )
    if runtime is not None:
        try:
            services.ensure_uart_allowed(runtime)
        except WatcherBlocked as blocked:
            services.record_blocked_event(
                "write_serial",
                normalized_args,
                blocked,
                started=started,
                board_id=board_id,
                session=runtime,
            )
            return services.format_block(blocked, session_id=runtime.session_id)

    handle = services.handle_for(board_id)
    if handle.board is None:
        return services.no_board_config_message

    board = handle.board
    resolved_port = services.resolve_port(handle, override=port)
    resolved_baudrate = baudrate or board.default_baudrate
    payload_text = f"{text}\n" if append_newline else text
    payload = payload_text.encode("utf-8")
    normalized_args = {
        "board_id": board_id,
        "port": resolved_port.device,
        "baudrate": resolved_baudrate,
        "text_length": len(text),
        "bytes_to_write": len(payload),
        "append_newline": append_newline,
        "timeout_seconds": timeout_seconds,
    }
    write_result = services.write_uart(
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
    event = services.record_event(
        "write_serial",
        normalized_args,
        outcome_kind=ToolOutcome.SUCCESS,
        error_code=None,
        duration_ms=services.duration_ms(started),
        details={
            "bytes_written": write_result.bytes_written,
            "write_duration_seconds": round(write_result.duration_seconds, 3),
        },
        board_id=board_id,
        session=runtime,
    )
    if runtime is not None:
        services.handle_mutation_event(board_id, event)
    return result


def build_serial_handlers(
    services: SerialToolServices,
) -> dict[str, Callable[..., str]]:
    """Build the final guarded serial surface with common Layer-2 wrapping."""

    def read_serial_handler(
        board_id: str,
        expected_text: str | None = None,
        read_seconds: float = 3.0,
        baudrate: int | None = None,
        port: str | None = None,
        reset_on_open: bool = False,
        on_exit: OnExitFinalizer | None = None,
    ) -> str:
        """Capture bounded UART output under an exact multi-call plan."""

        del on_exit  # RegistryFastMCP validates and owns finalizer execution.
        return wrap_layer2_response(
            read_serial(
                services,
                board_id,
                expected_text,
                read_seconds,
                baudrate,
                port,
                reset_on_open,
            )
        )

    def write_serial_handler(
        board_id: str,
        text: str,
        baudrate: int | None = None,
        port: str | None = None,
        append_newline: bool = False,
        timeout_seconds: float = 1.0,
        on_exit: OnExitFinalizer | None = None,
    ) -> str:
        """Write bounded UTF-8 UART text under an exact multi-call plan."""

        del on_exit  # RegistryFastMCP validates and owns finalizer execution.
        return wrap_layer2_response(
            write_serial(
                services,
                board_id,
                text,
                baudrate,
                port,
                append_newline,
                timeout_seconds,
            )
        )

    return {
        "read_serial": read_serial_handler,
        "write_serial": write_serial_handler,
    }
