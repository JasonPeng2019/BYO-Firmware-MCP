"""Bounded Layer-2 utility actions."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

from pyocd_debug_mcp.kernel.operations import (
    MAX_WAIT_DURATION_MS,
    cancellation_checkpoint,
    current_operation,
    wrap_layer2_response,
)
from pyocd_debug_mcp.services.session_runtime import SessionRecord, ToolOutcome


def _cooperative_sleep(seconds: float) -> None:
    operation = current_operation()
    if operation is None:
        time.sleep(seconds)
        return
    operation.checkpoint()
    # Request cancellation and the dispatch deadline both set this event. Wake
    # the owning worker; only its existing exit/cleanup path releases the board.
    operation.cancellation_requested.wait(seconds)
    operation.checkpoint()


@dataclass(frozen=True, slots=True)
class MiscToolServices:
    runtime_for: Callable[[str], SessionRecord | None]
    duration_ms: Callable[[float], int]
    record_event: Callable[..., object]
    sleep: Callable[[float], None] = _cooperative_sleep


def build_misc_handlers(services: MiscToolServices) -> dict[str, Callable[..., str]]:
    """Build the bounded always-available wait action."""

    def wait(
        board_id: str,
        ms: Annotated[int, Field(ge=1, le=MAX_WAIT_DURATION_MS)],
    ) -> str:
        """Pause one board workflow for 1 through 30,000 milliseconds (30 seconds).

        The maximum matches the ordinary operation budget. Oversized waits refuse
        before board reservation. Cancellation wakes the owning wait and releases
        its reservation after handler exit and cleanup; other boards can proceed.
        """

        started = time.monotonic()
        args = {"board_id": board_id, "ms": ms}
        if isinstance(ms, bool) or not isinstance(ms, int) or not 1 <= ms <= MAX_WAIT_DURATION_MS:
            message = f"ms must be a positive integer at most {MAX_WAIT_DURATION_MS} milliseconds."
            services.record_event(
                "wait",
                args,
                outcome_kind=ToolOutcome.REFUSED,
                error_code="wait/out-of-range",
                duration_ms=services.duration_ms(started),
                details={"message": message},
                board_id=board_id,
                session=services.runtime_for(board_id),
            )
            return wrap_layer2_response(f"Refused [wait/out-of-range]: {message}")
        cancellation_checkpoint()
        runtime = services.runtime_for(board_id)
        services.sleep(ms / 1000.0)
        cancellation_checkpoint()
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
