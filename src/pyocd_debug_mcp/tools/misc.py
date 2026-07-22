"""Bounded Layer-2 utility actions."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from pyocd_debug_mcp.kernel.operations import wrap_layer2_response
from pyocd_debug_mcp.services.session_runtime import SessionRecord, ToolOutcome


@dataclass(frozen=True, slots=True)
class MiscToolServices:
    runtime_for: Callable[[str], SessionRecord | None]
    duration_ms: Callable[[float], int]
    record_event: Callable[..., object]
    sleep: Callable[[float], None] = time.sleep


def build_misc_handlers(services: MiscToolServices) -> dict[str, Callable[..., str]]:
    """Build the bounded always-available wait action."""

    def wait(board_id: str, ms: int) -> str:
        """Pause for a positive number of milliseconds for one logical board workflow."""

        started = time.monotonic()
        args = {"board_id": board_id, "ms": ms}
        runtime = services.runtime_for(board_id)
        if isinstance(ms, bool) or not isinstance(ms, int) or ms < 1:
            services.record_event(
                "wait",
                args,
                outcome_kind=ToolOutcome.REFUSED,
                error_code="wait/out-of-range",
                duration_ms=services.duration_ms(started),
                details={"message": "ms must be a positive integer."},
                board_id=board_id,
                session=runtime,
            )
            return wrap_layer2_response(
                "Refused [wait/out-of-range]: ms must be a positive integer."
            )
        services.sleep(ms / 1000.0)
        services.record_event(
            "wait",
            args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=services.duration_ms(started),
            board_id=board_id,
            session=runtime,
        )
        return wrap_layer2_response(f"Waited {ms} ms for board '{board_id}'.")

    return {"wait": wait}
