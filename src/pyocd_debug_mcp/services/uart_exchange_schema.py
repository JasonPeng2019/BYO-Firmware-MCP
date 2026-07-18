"""Shared exact schema validation for state-preserving UART exchanges."""

from __future__ import annotations

import math
from collections.abc import Mapping

LINE_ENDINGS = frozenset({"none", "lf", "cr", "crlf"})
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


def _bounded_number(value: object, *, minimum: float, maximum: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and minimum <= float(value) <= maximum
    )


def validate_serial_exchange_parameters(parameters: Mapping[str, object]) -> str | None:
    """Return a precise refusal reason, or ``None`` for one executable exchange."""

    supplied = set(parameters)
    if supplied != SERIAL_EXCHANGE_FIELDS:
        return (
            "serial_exchange parameters must match exactly; "
            f"missing={sorted(SERIAL_EXCHANGE_FIELDS - supplied)}; "
            f"unknown={sorted(supplied - SERIAL_EXCHANGE_FIELDS)}"
        )
    steps = parameters["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
        return "steps must contain 1-8 exact command/response objects"
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
        if not isinstance(text, str) or not text or len(text.encode("utf-8")) > 4096:
            return f"steps[{index}].text must be 1-4096 UTF-8 bytes"
        if not isinstance(expected, str) or not expected:
            return f"steps[{index}].expected_text must be non-empty text"
        if not isinstance(ending, str) or ending not in LINE_ENDINGS:
            return f"steps[{index}].line_ending must be none, lf, cr, or crlf"

    if not _bounded_number(parameters["read_seconds"], minimum=0.000001, maximum=30):
        return "read_seconds must be a finite number in (0, 30]"
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
    if not _bounded_number(ready_seconds, minimum=0, maximum=30):
        return "ready_seconds must be a finite number in [0, 30]"
    if not _bounded_number(probe_delay, minimum=0, maximum=30):
        return "ready_probe_delay_seconds must be a finite number in [0, 30]"
    assert isinstance(ready_seconds, (int, float)) and not isinstance(ready_seconds, bool)
    assert isinstance(probe_delay, (int, float)) and not isinstance(probe_delay, bool)

    if ready_text is None:
        if ready_seconds != 0 or probe_text is not None or probe_delay != 0:
            return (
                "without ready_text, ready_seconds and ready_probe_delay_seconds must be 0 "
                "and ready_probe_text must be NULL"
            )
        if probe_ending != "none":
            return "ready_probe_line_ending must be none when ready_probe_text is NULL"
        return None
    if not isinstance(ready_text, str) or not ready_text:
        return "ready_text must be non-empty text or NULL"
    if not 0 < float(ready_seconds) <= 30:
        return "ready_text requires ready_seconds in (0, 30]"
    if probe_text is None:
        if probe_delay != 0:
            return "ready_probe_delay_seconds requires ready_probe_text"
        if probe_ending != "none":
            return "ready_probe_line_ending must be none when ready_probe_text is NULL"
        return None
    if not isinstance(probe_text, str) or len(probe_text.encode("utf-8")) > 256:
        return "ready_probe_text must be at most 256 UTF-8 bytes or NULL"
    if not probe_text and probe_ending == "none":
        return "an empty ready_probe_text requires a line ending"
    if float(probe_delay) > float(ready_seconds):
        return "ready_probe_delay_seconds must not exceed ready_seconds"
    return None
