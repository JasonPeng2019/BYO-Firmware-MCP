"""Revised Layer-2 target execution controls."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pyocd_debug_mcp.kernel.operations import wrap_layer2_response


@dataclass(frozen=True, slots=True)
class ExecutionToolServices:
    halt: Callable[[str], str]
    resume: Callable[[str], str]
    step: Callable[[str], str]
    reset: Callable[[str, bool], str]
    connect_under_reset: Callable[[str, str | None, str | None], str]


def build_execution_handlers(
    services: ExecutionToolServices,
) -> dict[str, Callable[..., str]]:
    """Build always-available and plan-guarded execution handlers."""

    def halt(board_id: str) -> str:
        """Halt the selected board's core."""

        return wrap_layer2_response(services.halt(board_id))

    def resume(board_id: str) -> str:
        """Resume the selected board's core."""

        return wrap_layer2_response(services.resume(board_id))

    def step(board_id: str) -> str:
        """Single-step and return the resulting program counter."""

        return wrap_layer2_response(services.step(board_id))

    def reset_and_run(board_id: str) -> str:
        """Reset and execute from the reset vector without changing target security."""

        return wrap_layer2_response(services.reset(board_id, False))

    def reset_and_halt(board_id: str) -> str:
        """Reset and halt at startup without changing target security."""

        return wrap_layer2_response(services.reset(board_id, True))

    def connect_under_reset(
        board_id: str,
        probe_uid: str | None = None,
        target_override: str | None = None,
    ) -> str:
        """Assert wired reset, attach and halt, then release reset."""

        return wrap_layer2_response(
            services.connect_under_reset(board_id, probe_uid, target_override)
        )

    return {
        "halt": halt,
        "resume": resume,
        "step": step,
        "reset_and_run": reset_and_run,
        "reset_and_halt": reset_and_halt,
        "connect_under_reset": connect_under_reset,
    }
