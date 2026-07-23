"""Observed target execution controls."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from firmware_mcp.kernel.operations import wrap_layer2_response


@dataclass(frozen=True, slots=True)
class ExecutionToolServices:
    halt: Callable[[str], str]
    resume: Callable[[str], str]
    step: Callable[[str], str]
    reset: Callable[[str, bool], str]


def build_execution_handlers(services: ExecutionToolServices) -> dict[str, Callable[..., str]]:
    """Build execution actions with observed postconditions."""

    def halt_target(board_id: str) -> str:
        """**What** Halt the target and observe the halted state.

        **When** Use before inspection or register/memory debug work.

        **Parameters** `board_id` names the connected board, for example `"board-a"`.

        **Returns** Observed halt evidence.

        **Failures and recovery** Transport or state-observation failure is explicit; reconnect with
        `connect_board` and retry.
        """
        return wrap_layer2_response(services.halt(board_id))

    def resume_target(board_id: str) -> str:
        """**What** Ask the target to resume and report its immediate observed state.

        **When** Use after a halt or breakpoint inspection.

        **Parameters** `board_id` is the connected board, for example `"board-a"`.

        **Returns** Command acceptance and observed state; a breakpoint may legitimately re-halt.

        **Failures and recovery** Connection failure is explicit; use `connect_board` then retry.
        """
        return wrap_layer2_response(services.resume(board_id))

    def step_target(board_id: str) -> str:
        """**What** Execute one instruction while halted.

        **When** Use for single-step debugging.

        **Parameters** `board_id` identifies the halted board, for example `"board-a"`.

        **Returns** Observed halted state and real program counter.

        **Failures and recovery** Halt-state or transport errors are reported; use `halt_target` or
        `connect_board` before retrying.
        """
        return wrap_layer2_response(services.step(board_id))

    def reset_target(board_id: str, halt_after_reset: bool = False) -> str:
        """**What** Reset the target and observe its immediate final state.

        **When** Use to restart firmware or reset into debug.

        **Parameters** `board_id` names the board; `halt_after_reset` is a boolean (for example
        `true`) requesting a halted reset.

        **Returns** Requested reset mode and observed state without promising persistent running.

        **Failures and recovery** An unobserved postcondition is explicit; reconnect with
        `connect_board` and retry.
        """
        return wrap_layer2_response(services.reset(board_id, halt_after_reset))

    return {
        "halt_target": halt_target,
        "resume_target": resume_target,
        "step_target": step_target,
        "reset_target": reset_target,
    }
