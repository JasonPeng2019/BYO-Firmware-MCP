"""Shared exact schema validation for state-preserving UART exchanges."""

from __future__ import annotations

import math
from collections.abc import Mapping

LINE_ENDINGS = frozenset({"none", "lf", "cr", "crlf"})
_LINE_ENDING_BYTES = {"none": b"", "lf": b"\n", "cr": b"\r", "crlf": b"\r\n"}
MAX_UART_SECONDS = 30.0
MAX_UART_EXCHANGE_STEPS = 16
MAX_UART_TEXT_BYTES = 65_536
SERIAL_EXCHANGE_FIELDS = frozenset(
    {
        "steps",
        "read_seconds",
        "baudrate",
        "port",
        "ready_text",
        "ready_seconds",
        "ready_probe_text",
        "ready_probe_line_ending",
        "ready_probe_delay_seconds",
        "clear_input",
    }
)


def _finite_number_at_least(value: object, *, minimum: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and minimum <= float(value)
    )


def validate_serial_exchange_parameters(
    parameters: Mapping[str, object],
    *,
    max_seconds: float | None = None,
    max_steps: int | None = None,
    max_text_bytes: int | None = None,
) -> str | None:
    """Return a precise refusal reason, or ``None`` for one executable exchange.

    The established planned UART surface intentionally keeps its historical
    positive-duration/unbounded-capture behavior. Explicit raw calls opt into
    finite transport caps through these parameters.
    """

    supplied = set(parameters)
    if supplied != SERIAL_EXCHANGE_FIELDS:
        return (
            "serial_exchange parameters must match exactly; "
            f"missing={sorted(SERIAL_EXCHANGE_FIELDS - supplied)}; "
            f"unknown={sorted(supplied - SERIAL_EXCHANGE_FIELDS)}"
        )
    steps = parameters["steps"]
    if not isinstance(steps, list) or not steps:
        return "steps must contain one or more exact command/response objects"
    if max_steps is not None and len(steps) > max_steps:
        return f"steps must contain at most {max_steps} command/response objects"
    text_bytes = 0
    for index, row in enumerate(steps):
        if not isinstance(row, Mapping) or set(row) != {
            "text",
            "expected_text",
            "line_ending",
        }:
            return f"steps[{index}] must contain exactly text, expected_text, and line_ending"
        text = row["text"]
        expected = row["expected_text"]
        ending = row["line_ending"]
        if not isinstance(text, str) or not text:
            return f"steps[{index}].text must be non-empty text"
        if not isinstance(expected, str) or not expected:
            return f"steps[{index}].expected_text must be non-empty text"
        if not isinstance(ending, str) or ending not in LINE_ENDINGS:
            return f"steps[{index}].line_ending must be none, lf, cr, or crlf"
        text_bytes += len(text.encode("utf-8")) + len(expected.encode("utf-8"))
        text_bytes += len(_LINE_ENDING_BYTES[ending])

    if not _finite_number_at_least(parameters["read_seconds"], minimum=0.000001):
        return "read_seconds must be a positive finite number"
    read_seconds = parameters["read_seconds"]
    assert isinstance(read_seconds, (int, float)) and not isinstance(read_seconds, bool)
    if max_seconds is not None and float(read_seconds) > max_seconds:
        return f"read_seconds must not exceed {max_seconds:g}"
    baudrate = parameters["baudrate"]
    if baudrate is not None and (
        isinstance(baudrate, bool) or not isinstance(baudrate, int) or baudrate <= 0
    ):
        return "baudrate must be a positive integer or NULL"
    port = parameters["port"]
    if port is not None and (not isinstance(port, str) or not port.strip()):
        return "port must be non-empty text or NULL"
    if not isinstance(parameters["clear_input"], bool):
        return "clear_input must be a boolean"

    ready_text = parameters["ready_text"]
    ready_seconds = parameters["ready_seconds"]
    probe_text = parameters["ready_probe_text"]
    probe_ending = parameters["ready_probe_line_ending"]
    probe_delay = parameters["ready_probe_delay_seconds"]
    if not isinstance(probe_ending, str) or probe_ending not in LINE_ENDINGS:
        return "ready_probe_line_ending must be none, lf, cr, or crlf"
    if not _finite_number_at_least(ready_seconds, minimum=0):
        return "ready_seconds must be a nonnegative finite number"
    assert isinstance(ready_seconds, (int, float)) and not isinstance(ready_seconds, bool)
    if max_seconds is not None and float(ready_seconds) > max_seconds:
        return f"ready_seconds must not exceed {max_seconds:g}"
    if not _finite_number_at_least(probe_delay, minimum=0):
        return "ready_probe_delay_seconds must be a nonnegative finite number"
    assert isinstance(probe_delay, (int, float)) and not isinstance(probe_delay, bool)

    if ready_text is None:
        if ready_seconds != 0 or probe_text is not None or probe_delay != 0:
            return (
                "without ready_text, ready_seconds and ready_probe_delay_seconds must be 0 "
                "and ready_probe_text must be NULL"
            )
        if probe_ending != "none":
            return "ready_probe_line_ending must be none when ready_probe_text is NULL"
    else:
        if not isinstance(ready_text, str) or not ready_text:
            return "ready_text must be non-empty text or NULL"
        text_bytes += len(ready_text.encode("utf-8"))
        if not 0 < float(ready_seconds):
            return "ready_text requires positive ready_seconds"
        if probe_text is None:
            if probe_delay != 0:
                return "ready_probe_delay_seconds requires ready_probe_text"
            if probe_ending != "none":
                return "ready_probe_line_ending must be none when ready_probe_text is NULL"
        else:
            if not isinstance(probe_text, str):
                return "ready_probe_text must be text or NULL"
            text_bytes += len(probe_text.encode("utf-8")) + len(_LINE_ENDING_BYTES[probe_ending])
            if not probe_text and probe_ending == "none":
                return "an empty ready_probe_text requires a line ending"
            if float(probe_delay) > float(ready_seconds):
                return "ready_probe_delay_seconds must not exceed ready_seconds"
    if max_text_bytes is not None and text_bytes > max_text_bytes:
        return f"serial exchange text must not exceed {max_text_bytes} UTF-8 bytes"
    return None
