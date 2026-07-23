"""Board-local timing utility."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from firmware_mcp.kernel.operations import wrap_layer2_response
from firmware_mcp.services.session_runtime import SessionRecord, ToolOutcome


@dataclass(frozen=True, slots=True)
class MiscToolServices:
    runtime_for: Callable[[str], SessionRecord | None]
    duration_ms: Callable[[float], int]
    record_event: Callable[..., object]
    sleep: Callable[[float], None] = time.sleep


def build_misc_handlers(services: MiscToolServices) -> dict[str, Callable[..., str]]:
    def wait_duration(board_id: str, duration_seconds: float) -> str:
        """**What** Wait for a positive finite board-local duration.

        **When** Use when firmware needs a deliberate observation interval.

        **Parameters** `board_id` is the board; `duration_seconds` is seconds, for example `0.5`.

        **Returns** The elapsed requested duration as operation evidence.

        **Failures and recovery** Invalid duration is refused; provide positive finite seconds.
        """
        started = time.monotonic()
        args = {"board_id": board_id, "duration_seconds": duration_seconds}
        runtime = services.runtime_for(board_id)
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(duration_seconds)
            or duration_seconds <= 0
        ):
            services.record_event(
                "wait_duration",
                args,
                outcome_kind=ToolOutcome.INVALID,
                error_code="wait/invalid-duration",
                duration_ms=services.duration_ms(started),
                details={"message": "duration_seconds must be positive and finite."},
                board_id=board_id,
                session=runtime,
            )
            return wrap_layer2_response(
                "Invalid [wait/invalid-duration]: duration_seconds must be positive and finite."
            )
        services.sleep(float(duration_seconds))
        services.record_event(
            "wait_duration",
            args,
            outcome_kind=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=services.duration_ms(started),
            board_id=board_id,
            session=runtime,
        )
        return wrap_layer2_response(
            f"Waited {duration_seconds:g} second(s) for board '{board_id}'."
        )

    return {"wait_duration": wait_duration}
