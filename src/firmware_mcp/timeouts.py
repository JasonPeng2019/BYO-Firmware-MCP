"""Small helpers for caller-defined semantic timeouts."""

from __future__ import annotations


def subprocess_timeout_stream_text(value: object) -> str:
    """Normalize subprocess timeout output to text for diagnostics."""

    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
