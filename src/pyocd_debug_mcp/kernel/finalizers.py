"""Strict structured finalizers for eligible stateful operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


class UARTWriteFinalizer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["uart_write"]
    text: str = Field(min_length=1)
    timeout_seconds: float = Field(default=1.0, gt=0)

    @field_validator("timeout_seconds")
    @classmethod
    def _finite_timeout(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("timeout_seconds must be finite")
        return value


class ResetAndRunFinalizer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["reset_and_run"]


OnExitFinalizer = Annotated[
    UARTWriteFinalizer | ResetAndRunFinalizer,
    Field(discriminator="action"),
]
_FINALIZER_ADAPTER = TypeAdapter(OnExitFinalizer)
ELIGIBLE_FINALIZER_TOOLS = frozenset({"read_serial", "write_serial"})


class FinalizerValidationError(ValueError):
    """A finalizer was malformed, arbitrary, or attached to an ineligible tool."""


def parse_finalizer(tool_name: str, value: object) -> OnExitFinalizer | None:
    if value is None:
        return None
    if tool_name not in ELIGIBLE_FINALIZER_TOOLS:
        raise FinalizerValidationError(f"Tool '{tool_name}' does not accept an on_exit finalizer.")
    if not isinstance(value, Mapping):
        raise FinalizerValidationError(
            "on_exit must be a structured uart_write or reset_and_run object; shell strings "
            "and arbitrary commands are forbidden."
        )
    try:
        return _FINALIZER_ADAPTER.validate_python(dict(value), strict=True)
    except ValueError as exc:
        raise FinalizerValidationError(
            "Invalid on_exit finalizer; only structured uart_write and reset_and_run are allowed."
        ) from exc


def build_finalizer(
    tool_name: str,
    board_id: str,
    value: object,
    *,
    uart_write: Callable[[str, str, float], None],
    reset_and_run: Callable[[str], None],
) -> Callable[[], None] | None:
    finalizer = parse_finalizer(tool_name, value)
    if finalizer is None:
        return None
    if isinstance(finalizer, UARTWriteFinalizer):
        return lambda: uart_write(board_id, finalizer.text, finalizer.timeout_seconds)
    return lambda: reset_and_run(board_id)
