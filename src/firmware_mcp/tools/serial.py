"""Board-scoped UART action handlers with preserved validation and event logging."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from firmware_mcp.adapters.debug_interface import session_metadata
from firmware_mcp.kernel.operations import wrap_layer2_response
from firmware_mcp.services.session_runtime import (
    InvalidRequestError,
    SessionRecord,
    ToolOutcome,
)
from firmware_mcp.services.uart_exchange_schema import (
    validate_exchange_serial_parameters,
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


@dataclass(frozen=True, slots=True)
class SerialToolServices:
    runtime_for: Callable[[str], SessionRecord | None]
    active_session_id: Callable[[str], str | None]
    duration_ms: Callable[[float], int]
    record_event: Callable[..., object]
    format_invalid: Callable[..., str]
    handle_for: Callable[[str], Any]
    resolve_port: Callable[..., Any]
    capture_uart: Callable[..., Any]
    write_uart: Callable[..., Any]
    exchange_uart: Callable[..., Any]
    reset_target: Callable[[Any], None]
    no_board_config_message: str


def _record_invalid(
    services: SerialToolServices,
    tool_name: str,
    board_id: str,
    normalized_args: Mapping[str, object],
    issue: InvalidRequestError,
    started: float,
    runtime: SessionRecord | None,
) -> str:
    services.record_event(
        tool_name,
        normalized_args,
        outcome_kind=ToolOutcome.INVALID,
        error_code=issue.code,
        duration_ms=services.duration_ms(started),
        details={"message": issue.message},
        board_id=board_id,
        session=runtime,
    )
    return services.format_invalid(
        issue,
        session_id=services.active_session_id(board_id),
    )


def read_serial(
    services: SerialToolServices,
    board_id: str,
    timeout_seconds: float,
    expected_text: str | None = None,
    baud: int | None = None,
    port: str | None = None,
    reset_on_open: bool = False,
) -> str:
    """Capture bounded UART output for one logical board."""

    started = time.monotonic()
    runtime = services.runtime_for(board_id)
    normalized_args: dict[str, object] = {
        "board_id": board_id,
        "port": port,
        "baud": baud,
        "expected_text": expected_text,
        "timeout_seconds": timeout_seconds,
        "reset_on_open": reset_on_open,
    }
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        return _record_invalid(
            services,
            "read_serial",
            board_id,
            normalized_args,
            InvalidRequestError(
                "uart/invalid-timeout", "timeout_seconds must be a positive finite number."
            ),
            started,
            runtime,
        )
    if baud is not None and baud <= 0:
        return _record_invalid(
            services,
            "read_serial",
            board_id,
            normalized_args,
            InvalidRequestError("uart/invalid-baud", "baud must be > 0."),
            started,
            runtime,
        )
    handle = services.handle_for(board_id)
    if handle.board is None:
        return services.no_board_config_message

    board = handle.board
    resolved_port = services.resolve_port(handle, override=port)
    resolved_baudrate = baud or board.default_baudrate
    normalized_args = {
        "board_id": board_id,
        "port": resolved_port.device,
        "baud": resolved_baudrate,
        "expected_text": expected_text,
        "timeout_seconds": timeout_seconds,
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
        timeout_seconds,
        expected_text,
        on_port_open=on_port_open,
    )
    expectation_label = (
        f"expected='{expected_text}'" if expected_text is not None else "expected=(none)"
    )
    verdict = (
        "completed"
        if expected_text is None
        else ("matched" if capture.matched else "did not match")
    )
    captured_text = capture.text
    result = (
        f"UART {verdict} on {resolved_port.device} at {resolved_baudrate} baud via "
        f"{session_metadata(handle).route_used}; {expectation_label}; "
        f"duration={capture.duration_seconds:.2f}s; "
        f"captured_bytes={len(capture.raw_bytes)}; "
        f"captured_hex={json.dumps(capture.raw_bytes.hex())}; "
        f"captured_text={json.dumps(captured_text, ensure_ascii=True)}"
    )
    services.record_event(
        "read_serial",
        normalized_args,
        outcome_kind=ToolOutcome.SUCCESS if capture.matched else ToolOutcome.FAILED,
        error_code=None if capture.matched else "uart/no-match",
        duration_ms=services.duration_ms(started),
        details={
            "matched": capture.matched,
            "capture_duration_seconds": round(capture.duration_seconds, 3),
            "captured_bytes": len(capture.raw_bytes),
            "captured_hex": capture.raw_bytes.hex(),
            "captured_text": captured_text,
        },
        board_id=board_id,
        session=runtime,
    )
    return result


def write_serial(
    services: SerialToolServices,
    board_id: str,
    text: str,
    timeout_seconds: float,
    baud: int | None = None,
    port: str | None = None,
    line_ending: str = "none",
) -> str:
    """Write bounded UTF-8 text to one logical board's UART."""

    started = time.monotonic()
    runtime = services.runtime_for(board_id)
    normalized_args: dict[str, object] = {
        "board_id": board_id,
        "port": port,
        "baud": baud,
        "text_length": len(text),
        "line_ending": line_ending,
        "timeout_seconds": timeout_seconds,
    }
    if baud is not None and baud <= 0:
        return _record_invalid(
            services,
            "write_serial",
            board_id,
            normalized_args,
            InvalidRequestError("uart/invalid-baud", "baud must be > 0."),
            started,
            runtime,
        )
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        return _record_invalid(
            services,
            "write_serial",
            board_id,
            normalized_args,
            InvalidRequestError(
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
    resolved_baudrate = baud or board.default_baudrate
    try:
        payload = _encode_uart_text(text, line_ending)
    except ValueError as exc:
        return _record_invalid(
            services,
            "write_serial",
            board_id,
            normalized_args,
            InvalidRequestError("uart/invalid-line-ending", str(exc)),
            started,
            runtime,
        )
    normalized_args = {
        "board_id": board_id,
        "port": resolved_port.device,
        "baud": resolved_baudrate,
        "text_length": len(text),
        "bytes_to_write": len(payload),
        "line_ending": line_ending,
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


def exchange_serial(
    services: SerialToolServices,
    board_id: str,
    steps: list[dict[str, object]],
    timeout_seconds: float,
    baud: int | None = None,
    port: str | None = None,
    ready_text: str | None = None,
    ready_timeout_seconds: float = 0.0,
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
        "timeout_seconds": timeout_seconds,
        "baud": baud,
        "port": port,
        "ready_text": ready_text,
        "ready_timeout_seconds": ready_timeout_seconds,
        "ready_probe_text": ready_probe_text,
        "ready_probe_line_ending": ready_probe_line_ending,
        "ready_probe_delay_seconds": ready_probe_delay_seconds,
        "clear_input": clear_input,
    }
    schema_error = validate_exchange_serial_parameters(
        {key: value for key, value in normalized_args.items() if key != "board_id"}
    )
    if schema_error is not None:
        return _record_invalid(
            services,
            "exchange_serial",
            board_id,
            normalized_args,
            InvalidRequestError(
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
    resolved_baudrate = baud or handle.board.default_baudrate
    first_payload, first_expected = validated_steps[0]
    exchange = services.exchange_uart(
        resolved_port.device,
        resolved_baudrate,
        first_payload,
        first_expected,
        timeout_seconds,
        ready_text=ready_text,
        ready_seconds=ready_timeout_seconds,
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
    result = (
        f"UART exchange {'matched' if exchange.matched else 'did not match'} on "
        f"{resolved_port.device} at {resolved_baudrate} baud; wrote "
        f"{exchange.bytes_written} byte(s); duration={exchange.duration_seconds:.2f}s; "
        f"ready={'matched' if exchange.ready_matched else 'did not match'}; "
        f"ready_probe_bytes={exchange.ready_probe_bytes_written}; "
        f"steps={len(exchange.steps)} [{step_summary or 'none'}]; "
        f"captured_bytes={len(exchange.raw_bytes)}; "
        f"captured_hex={json.dumps(exchange.raw_bytes.hex())}; "
        f"captured_text={json.dumps(exchange.text, ensure_ascii=True)}; "
        "step_captured_texts="
        f"{json.dumps([step.text for step in exchange.steps], ensure_ascii=True)}; "
        f"step_captured_hex={json.dumps([step.raw_bytes.hex() for step in exchange.steps])}"
    )
    services.record_event(
        "exchange_serial",
        normalized_args,
        outcome_kind=ToolOutcome.SUCCESS if exchange.matched else ToolOutcome.FAILED,
        error_code=None if exchange.matched else "uart/no-match",
        duration_ms=services.duration_ms(started),
        details={
            "matched": exchange.matched,
            "bytes_written": exchange.bytes_written,
            "captured_bytes": len(exchange.raw_bytes),
            "captured_hex": exchange.raw_bytes.hex(),
            "captured_text": exchange.text,
            "ready_matched": exchange.ready_matched,
            "ready_probe_bytes_written": exchange.ready_probe_bytes_written,
            "steps": [
                {
                    "expected_text": step.expected_text,
                    "matched": step.matched,
                    "bytes_written": step.bytes_written,
                    "captured_bytes": len(step.raw_bytes),
                    "captured_hex": step.raw_bytes.hex(),
                    "captured_text": step.text,
                }
                for step in exchange.steps
            ],
        },
        board_id=board_id,
        session=runtime,
    )
    return result


def build_serial_handlers(
    services: SerialToolServices,
) -> dict[str, Callable[..., str]]:
    """Build the final serial surface with common Layer-2 wrapping."""

    def read_serial_handler(
        board_id: str,
        timeout_seconds: float,
        expected_text: str | None = None,
        baud: int | None = None,
        port: str | None = None,
        reset_on_open: bool = False,
    ) -> str:
        """**What** Capture UART bytes and optional expected text.

        **When** Use for exploratory serial observation or a firmware output check.

        **Parameters** `board_id` is the board; `expected_text` is optional; required
        `timeout_seconds` is a caller-supplied positive finite capture duration in seconds (for
        example, caller-chosen `3.0`, never a server default); optional `baud` is baud (for
        example `115200`); `port` overrides the port; `reset_on_open` is a boolean.

        **Returns** Captured byte count, hex/text evidence, and expectation result; empty capture
        without an expectation is successful transport evidence.

        **Failures and recovery** Port, timeout, and expected-text misses are explicit; use
        `get_board_info`, adjust `port`/`baud`, or retry `read_serial`.
        """
        return wrap_layer2_response(
            read_serial(
                services,
                board_id,
                timeout_seconds,
                expected_text,
                baud,
                port,
                reset_on_open,
            )
        )

    def write_serial_handler(
        board_id: str,
        text: str,
        timeout_seconds: float,
        baud: int | None = None,
        port: str | None = None,
        line_ending: str = "none",
    ) -> str:
        """**What** Write UTF-8 UART text and account for every byte.

        **When** Use to send one command to connected firmware.

        **Parameters** `board_id` is the board; `text` is payload text; optional `baud` is baud;
        optional `port` selects a port; `line_ending` is `none`, `lf`, `cr`, or `crlf`; and
        required `timeout_seconds` is a caller-supplied positive finite duration in seconds (for
        example, caller-chosen `1.0`, never a server default).

        **Returns** Exact written count and duration.

        **Failures and recovery** Partial writes or unavailable ports fail; adjust `port`/`baud`
        and retry `write_serial`.
        """
        return wrap_layer2_response(
            write_serial(
                services,
                board_id,
                text,
                timeout_seconds,
                baud,
                port,
                line_ending,
            )
        )

    def exchange_serial_handler(
        board_id: str,
        steps: list[dict[str, object]],
        timeout_seconds: float,
        baud: int | None = None,
        port: str | None = None,
        ready_text: str | None = None,
        ready_timeout_seconds: float = 0.0,
        ready_probe_text: str | None = None,
        ready_probe_line_ending: str = "none",
        ready_probe_delay_seconds: float = 0.0,
        clear_input: bool = False,
    ) -> str:
        """**What** Run ordered UART command/response steps through one port open.

        **When** Use for a stateful firmware dialogue.

        **Parameters** `board_id` is the board; `steps` contain `text`, `expected_text`, and
        `line_ending`; required `timeout_seconds` is a caller-supplied positive finite per-step
        duration in seconds with no server default; optional `baud` is baud; `port` selects a
        port; `ready_text` and `ready_timeout_seconds` describe optional readiness;
        `ready_probe_text`, `ready_probe_line_ending`, `ready_probe_delay_seconds`, and
        `clear_input` control the named exchange behavior. For example,
        `exchange_serial(board_id="board-a", steps=[{"text": "status", "expected_text":
        "ok", "line_ending": "lf"}], timeout_seconds=2.0, baud=115200,
        port="<detected-port>")`, where `2.0` is caller-chosen.

        **Returns** Per-step write/capture hex evidence and match results.

        **Failures and recovery** Partial writes, readiness, and expected-text misses are explicit;
        use `read_serial` to diagnose then retry `exchange_serial`.
        """

        return wrap_layer2_response(
            exchange_serial(
                services,
                board_id,
                steps,
                timeout_seconds,
                baud,
                port,
                ready_text,
                ready_timeout_seconds,
                ready_probe_text,
                ready_probe_line_ending,
                ready_probe_delay_seconds,
                clear_input,
            )
        )

    return {
        "read_serial": read_serial_handler,
        "write_serial": write_serial_handler,
        "exchange_serial": exchange_serial_handler,
    }
