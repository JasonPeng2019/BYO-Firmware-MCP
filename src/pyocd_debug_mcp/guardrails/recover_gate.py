"""Shared recover/unlock guardrail policy for R10."""

from __future__ import annotations

from dataclasses import dataclass

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.board_config import RECOVER_MODE_MANUAL_ONLY
from pyocd_debug_mcp.services.session_runtime import ActionContext, PolicyRefusal


@dataclass(frozen=True)
class AuthorizedRecoverRequest:
    board_id: str
    recover_mode: str


def _refuse(code: str, message: str, context: ActionContext) -> PolicyRefusal:
    return PolicyRefusal(code, message, session_id=context.session_id)


def authorize_recover(
    handle: TargetSessionHandle | None,
    *,
    confirm: bool,
    recover_already_completed: bool,
    action_context: ActionContext,
) -> AuthorizedRecoverRequest:
    if handle is None:
        raise _refuse(
            "recover/no-session", "Recover requires an active connected session.", action_context
        )

    board = handle.board
    if board is None:
        raise _refuse(
            "recover/no-board-config",
            "Recover requires a loaded board config with a supported recover_mode.",
            action_context,
        )
    if not confirm:
        raise _refuse(
            "recover/confirmation-required",
            "Recover requires confirm=True. This operation may erase flash.",
            action_context,
        )
    if recover_already_completed:
        raise _refuse(
            "recover/already-used",
            "Recover already succeeded in this session. Disconnect and reconnect before trying again.",
            action_context,
        )
    if not board.recover_mode:
        raise _refuse(
            "recover/unsupported-mode",
            f"{board.display_name} does not define a recover mode.",
            action_context,
        )
    if board.recover_mode == RECOVER_MODE_MANUAL_ONLY:
        raise _refuse(
            "recover/manual-only",
            f"{board.display_name} requires a manual recover procedure for this family; this repo does not automate recover_mode=manual_only.",
            action_context,
        )

    return AuthorizedRecoverRequest(
        board_id=board.board_id,
        recover_mode=board.recover_mode,
    )
