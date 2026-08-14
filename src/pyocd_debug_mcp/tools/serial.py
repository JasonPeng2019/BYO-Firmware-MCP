"""Board-scoped UART action handlers with preserved validation and event logging."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pyocd_debug_mcp.adapters.swd_interface import session_metadata
from pyocd_debug_mcp.kernel.operations import wrap_layer2_response
from pyocd_debug_mcp.kernel.finalizers import OnExitFinalizer
from pyocd_debug_mcp.services.session_runtime import (
    PolicyRefusal,
    SessionRecord,
    ToolOutcome,
)
from pyocd_debug_mcp.services.uart_exchange_schema import (
    validate_serial_exchange_parameters,
)

_LINE_ENDINGS = {
    "none": "",
    "lf": "\n",
    "cr": "\r",
    "crlf": "\r\n",
}


def _encode_uart_text(text: str, line_ending: str) -> bytes:
    try:
        suffix = _LINE_ENDINGS[line_ending]
    except KeyError as exc:
        raise ValueError("line_ending must be one of: none, lf, cr, crlf") from exc
    return f"{text}{suffix}".encode("utf-8")


def _raw_uart_evidence(raw_bytes: bytes) -> dict[str, str | int]:
    """Return reversible, independently checkable metadata for observed UART bytes."""

    return {
        "captured_bytes_base64": base64.b64encode(raw_bytes).decode("ascii"),
        "captured_byte_count": len(raw_bytes),
        "captured_bytes_sha256": hashlib.sha256(raw_bytes).hexdigest(),
    }


@dataclass(frozen=True, slots=True)
class SerialToolServices:
    runtime_for: Callable[[str], SessionRecord | None]
    active_session_id: Callable[[str], str | None]
    duration_ms: Callable[[float], int]
    record_event: Callable[..., object]
    format_refusal: Callable[..., str]
    handle_for: Callable[[str], Any]
    resolve_port: Callable[..., Any]
    capture_uart: Callable[..., Any]
    write_uart: Callable[..., Any]
    exchange_uart: Callable[..., Any]
    reset_target: Callable[[Any], None]
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
    if (
        isinstance(read_seconds, bool)
        or not isinstance(read_seconds, (int, float))
        or not math.isfinite(read_seconds)
        or read_seconds <= 0
    ):
        return _record_refusal(
            services,
            "read_serial",
            board_id,
            normalized_args,
            PolicyRefusal(
                "uart/invalid-read-seconds", "read_seconds must be a positive finite number."
            ),
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
    captured_text = capture.text
    raw_evidence = _raw_uart_evidence(capture.raw_bytes)
    result = (
        f"UART {verdict} on {resolved_port.device} at {resolved_baudrate} baud via "
        f"{session_metadata(handle).route_used}; {expectation_label}; "
        f"reopen_count={capture.reopen_count}; "
        f"duration={capture.duration_seconds:.2f}s; "
        f"captured_text={json.dumps(captured_text, ensure_ascii=True)}; "
        f"captured_bytes_base64={raw_evidence['captured_bytes_base64']}; "
        f"captured_byte_count={raw_evidence['captured_byte_count']}; "
        f"captured_bytes_sha256={raw_evidence['captured_bytes_sha256']}"
    )
    services.record_event(
        "read_serial",
        normalized_args,
        outcome_kind=ToolOutcome.SUCCESS if capture.matched else ToolOutcome.FAILED,
        error_code=None if capture.matched else "uart/no-match",
        duration_ms=services.duration_ms(started),
        details={
            "matched": capture.matched,
            "reopen_count": capture.reopen_count,
            "capture_duration_seconds": round(capture.duration_seconds, 3),
            "captured_text": captured_text,
            **raw_evidence,
        },
        board_id=board_id,
        session=runtime,
    )
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
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        return _record_refusal(
            services,
            "write_serial",
            board_id,
            normalized_args,
            PolicyRefusal(
                "uart/invalid-timeout", "timeout_seconds must be a positive finite number."
            ),
            started,
            runtime,
        )
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
        f"at {resolved_baudrate} baud via {session_metadata(handle).route_used}; "
        f"duration={write_result.duration_seconds:.2f}s"
    )
    services.record_event(
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
    return result


def serial_exchange(
    services: SerialToolServices,
    board_id: str,
    steps: list[dict[str, object]],
    read_seconds: float,
    baudrate: int | None = None,
    port: str | None = None,
    ready_text: str | None = None,
    ready_seconds: float = 0.0,
    ready_probe_text: str | None = None,
    ready_probe_line_ending: str = "none",
    ready_probe_delay_seconds: float = 0.0,
    clear_input: bool = False,
) -> str:
    """Run a bounded, prevalidated UART conversation through one port open."""

    started = time.monotonic()
    runtime = services.runtime_for(board_id)
    normalized_args: dict[str, object] = {
        "board_id": board_id,
        "steps": steps,
        "read_seconds": read_seconds,
        "baudrate": baudrate,
        "port": port,
        "ready_text": ready_text,
        "ready_seconds": ready_seconds,
        "ready_probe_text": ready_probe_text,
        "ready_probe_line_ending": ready_probe_line_ending,
        "ready_probe_delay_seconds": ready_probe_delay_seconds,
        "clear_input": clear_input,
    }
    schema_error = validate_serial_exchange_parameters(
        {key: value for key, value in normalized_args.items() if key != "board_id"}
    )
    if schema_error is not None:
        return _record_refusal(
            services,
            "serial_exchange",
            board_id,
            normalized_args,
            PolicyRefusal(
                "uart/invalid-exchange",
                schema_error,
            ),
            started,
            runtime,
        )
    validated_steps: list[tuple[bytes, str]] = []
    for row in steps:
        assert isinstance(row, dict)
        text = row["text"]
        expected = row["expected_text"]
        ending = row["line_ending"]
        assert isinstance(text, str) and isinstance(expected, str) and isinstance(ending, str)
        validated_steps.append((_encode_uart_text(text, ending), expected))
    handle = services.handle_for(board_id)
    if handle.board is None:
        return services.no_board_config_message
    resolved_port = services.resolve_port(handle, override=port)
    resolved_baudrate = baudrate or handle.board.default_baudrate
    first_payload, first_expected = validated_steps[0]
    exchange = services.exchange_uart(
        resolved_port.device,
        resolved_baudrate,
        first_payload,
        first_expected,
        read_seconds,
        ready_text=ready_text,
        ready_seconds=ready_seconds,
        ready_probe=(
            _encode_uart_text(ready_probe_text, ready_probe_line_ending)
            if ready_probe_text is not None
            else None
        ),
        ready_probe_delay_seconds=ready_probe_delay_seconds,
        followup_steps=tuple(validated_steps[1:]),
        clear_input=clear_input,
    )
    step_summary = "; ".join(
        f"{index}:{step.expected_text}={'matched' if step.matched else 'did not match'}"
        for index, step in enumerate(exchange.steps, start=1)
    )
    raw_evidence = _raw_uart_evidence(exchange.raw_bytes)
    step_raw_evidence = [_raw_uart_evidence(step.raw_bytes) for step in exchange.steps]
    result = (
        f"UART exchange {'matched' if exchange.matched else 'did not match'} on "
        f"{resolved_port.device} at {resolved_baudrate} baud; wrote "
        f"{exchange.bytes_written} byte(s); duration={exchange.duration_seconds:.2f}s; "
        f"ready={'matched' if exchange.ready_matched else 'did not match'}; "
        f"ready_probe_bytes={exchange.ready_probe_bytes_written}; "
        f"steps={len(exchange.steps)} [{step_summary or 'none'}]; "
        f"captured_text={json.dumps(exchange.text, ensure_ascii=True)}; "
        "step_captured_texts="
        f"{json.dumps([step.text for step in exchange.steps], ensure_ascii=True)}; "
        f"captured_bytes_base64={raw_evidence['captured_bytes_base64']}; "
        f"captured_byte_count={raw_evidence['captured_byte_count']}; "
        f"captured_bytes_sha256={raw_evidence['captured_bytes_sha256']}; "
        f"step_captured_bytes={json.dumps(step_raw_evidence, ensure_ascii=True, separators=(',', ':'))}"
    )
    services.record_event(
        "serial_exchange",
        normalized_args,
        outcome_kind=ToolOutcome.SUCCESS if exchange.matched else ToolOutcome.FAILED,
        error_code=None if exchange.matched else "uart/no-match",
        duration_ms=services.duration_ms(started),
        details={
            "matched": exchange.matched,
            "bytes_written": exchange.bytes_written,
            "captured_text": exchange.text,
            **raw_evidence,
            "ready_matched": exchange.ready_matched,
            "ready_probe_bytes_written": exchange.ready_probe_bytes_written,
            "steps": [
                {
                    "expected_text": step.expected_text,
                    "matched": step.matched,
                    "bytes_written": step.bytes_written,
                    "captured_text": step.text,
                    **step_evidence,
                }
                for step, step_evidence in zip(exchange.steps, step_raw_evidence, strict=True)
            ],
        },
        board_id=board_id,
        session=runtime,
    )
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
        """Capture bounded UART output under an exact multi-call plan.

        When ``port`` names a connected endpoint, it is refused before UART I/O only
        if the current validated board session proves a different UART. Use that
        resolved UART or omit ``port`` to recover. A connected explicit port remains
        allowed when the server cannot prove the board/UART correlation.
        """

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
        """Write bounded UTF-8 UART text under an exact multi-call plan.

        When ``port`` names a connected endpoint, it is refused before UART I/O only
        if the current validated board session proves a different UART. Use that
        resolved UART or omit ``port`` to recover. A connected explicit port remains
        allowed when the server cannot prove the board/UART correlation.
        """

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

    def serial_exchange_handler(
        board_id: str,
        steps: list[dict[str, object]],
        read_seconds: float = 3.0,
        baudrate: int | None = None,
        port: str | None = None,
        ready_text: str | None = None,
        ready_seconds: float = 0.0,
        ready_probe_text: str | None = None,
        ready_probe_line_ending: str = "none",
        ready_probe_delay_seconds: float = 0.0,
        clear_input: bool = False,
    ) -> str:
        """Run planned command/response steps through one state-preserving UART open.

        When ``port`` names a connected endpoint, it is refused before UART I/O only
        if the current validated board session proves a different UART. Use that
        resolved UART or omit ``port`` to recover. A connected explicit port remains
        allowed when the server cannot prove the board/UART correlation.
        """

        return wrap_layer2_response(
            serial_exchange(
                services,
                board_id,
                steps,
                read_seconds,
                baudrate,
                port,
                ready_text,
                ready_seconds,
                ready_probe_text,
                ready_probe_line_ending,
                ready_probe_delay_seconds,
                clear_input,
            )
        )

    return {
        "read_serial": read_serial_handler,
        "write_serial": write_serial_handler,
        "serial_exchange": serial_exchange_handler,
    }
