"""Board-scoped connection handlers for the final public surface."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from firmware_mcp.kernel.operations import wrap_layer2_response


@dataclass(frozen=True, slots=True)
class SessionToolServices:
    connect: Callable[..., str]
    disconnect: Callable[[str], str]
    get_board_info: Callable[[str], str]
    get_state: Callable[[str], str]


def build_session_handlers(services: SessionToolServices) -> dict[str, Callable[..., str]]:
    """Build the one-home public connection surface."""

    def connect_board(
        board_id: str,
        probe_id: str | None = None,
        target: str | None = None,
        board_config_path: str | None = None,
        under_reset: bool = False,
    ) -> str:
        """**What** Connect one logical board to one physical probe.

        **When** Use after setup has persisted this board's verified profile and exact connection
        assignment. Request permission and create its exact plan before connecting; no live target
        session is needed to plan this stored route.

        **Parameters** `board_id` is the exact logical key returned by `get_setup_overview` (for
        example `"board_a"` only when that exact key was returned); copy it rather than replacing
        it with an illustrative spelling. Optional `probe_id` must be the stored assignment or a
        stable-equivalent pyOCD UID; optional `target` must match verified stored support;
        `board_config_path` must be null because this action replays the stored profile; and
        `under_reset` requests wired-reset attach over that same route (for example `true`).

        **Returns** Connection identity, route, and session evidence; profile text is never rewritten.

        **Failures and recovery** A busy or missing probe is reported honestly; use
        `get_setup_overview` or `setup_board`, then retry `connect_board`.
        """

        return wrap_layer2_response(
            services.connect(board_id, probe_id, target, board_config_path, under_reset)
        )

    def disconnect_board(board_id: str) -> str:
        """**What** Close one board-local target connection.

        **When** Use before reassigning a board or when hardware is removed.

        **Parameters** `board_id` names the board, for example `"board-a"`.

        **Returns** Board-local cleanup evidence without touching other boards.

        **Failures and recovery** Transport cleanup uncertainty is reported; reconnect with
        `connect_board` after checking the probe.
        """

        return wrap_layer2_response(services.disconnect(board_id))

    def get_board_info(board_id: str) -> str:
        """**What** Return profile, assignment, and active connection facts.

        **When** Use to inspect a board before an operation.

        **Parameters** `board_id` is the board, for example `"board-a"`.

        **Returns** Stored and live routing evidence.

        **Failures and recovery** Missing setup or connection is reported; use `setup_board` or
        `connect_board` as appropriate.
        """

        return wrap_layer2_response(services.get_board_info(board_id))

    def get_target_state(board_id: str) -> str:
        """**What** Observe the connected target execution state.

        **When** Use before or after debug controls.

        **Parameters** `board_id` identifies the connected board, for example `"board-a"`.

        **Returns** The current observed state, not a prediction.

        **Failures and recovery** A dropped connection is reported; use `connect_board` and retry.
        """

        return wrap_layer2_response(services.get_state(board_id))

    return {
        "connect_board": connect_board,
        "disconnect_board": disconnect_board,
        "get_board_info": get_board_info,
        "get_target_state": get_target_state,
    }
