"""Run-scoped structured permission grants for guarded plan tools."""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from pyocd_debug_mcp.guardrails.plan_defs import PermissionMode, PlanDefinition
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.services.session_runtime import PolicyRefusal, utc_now_text


class GrantMode(str, Enum):
    ONE_TIME = "one-time"
    FULL_SESSION = "full-session"


@dataclass(frozen=True, slots=True)
class PermissionGrant:
    grant_id: str
    run_id: str
    tool_name: str
    board_id: str
    mode: GrantMode
    granted_at: str


@dataclass(frozen=True, slots=True)
class PermissionAuthorization:
    """Opaque plan binding to one current store grant."""

    grant_id: str
    run_id: str
    tool_name: str
    board_id: str
    mode: GrantMode


@dataclass(frozen=True, slots=True)
class PermissionRevocation:
    tool_name: str
    board_id: str
    reason: str
    revoked_at: str
    grant_id: str | None
    revoked: bool


RevocationHandler = Callable[[str, str, str], None]


class PermissionStore:
    """Own permission authority for exactly one :class:`ServerRun`.

    Grants are keyed by underlying action name and logical board. They live in
    ``ServerRun.permissions`` so restart-default state remains explicit and
    inspectable without persisting any authority to disk.
    """

    def __init__(
        self,
        server_run: ServerRun,
        *,
        on_revoke: RevocationHandler | None = None,
    ) -> None:
        self.server_run = server_run
        self._on_revoke = on_revoke
        self._guard = threading.RLock()

    def set_revocation_handler(self, handler: RevocationHandler) -> None:
        """Attach the plan invalidation callback during composition."""

        with self._guard:
            self._on_revoke = handler

    @staticmethod
    def _key(tool_name: str, board_id: str) -> tuple[str, str]:
        if not tool_name.strip():
            raise ValueError("tool_name must be non-empty")
        if not board_id.strip():
            raise ValueError("board_id must be non-empty")
        return tool_name, board_id

    @staticmethod
    def _permission_request(definition: PlanDefinition) -> PolicyRefusal:
        requirement = (
            "fresh one-time"
            if definition.permission_mode is PermissionMode.FRESH_ONE_TIME
            else "one-time or full-session"
        )
        return PolicyRefusal(
            "permission/required",
            f"{definition.plan_tool_name} requires {requirement} user approval. Ask the user "
            "clearly in ordinary language, then pass the approval only as the structured "
            "user_permission value in the plan tool. General conversational assent is not "
            "authorization.",
        )

    def _current_state_locked(self, key: tuple[str, str]) -> PermissionGrant | None:
        state = self.server_run.permissions.get(key)
        if not isinstance(state, PermissionGrant):
            return None
        if state.run_id != self.server_run.run_id:
            self.server_run.permissions.pop(key, None)
            return None
        return state

    def active_grant(self, tool_name: str, board_id: str) -> PermissionGrant | None:
        key = self._key(tool_name, board_id)
        with self._guard:
            return self._current_state_locked(key)

    def null_disclosure(self, definition: PlanDefinition) -> str | None:
        if definition.permission_mode is PermissionMode.NONE:
            return None
        if definition.permission_mode is PermissionMode.FRESH_ONE_TIME:
            return (
                "No permission is reusable for this tool. Every execution requires a fresh "
                "one-time approval; full-session permission never applies."
            )
        with self._guard:
            boards = sorted(
                key[1]
                for key, value in self.server_run.permissions.items()
                if isinstance(key, tuple)
                and len(key) == 2
                and key[0] == definition.action_name
                and isinstance(value, PermissionGrant)
                and value.run_id == self.server_run.run_id
                and value.mode is GrantMode.FULL_SESSION
            )
        if not boards:
            return "No full-session permission is active for this tool."
        board_list = ", ".join(boards)
        return (
            f"Full-session permission is active for board(s): {board_list}. "
            "user_permission may be NULL only for a listed board."
        )

    def authorize_plan(
        self,
        definition: PlanDefinition,
        board_id: str,
        user_permission: object,
        max_calls: int,
        max_calls_buffer: int,
    ) -> PermissionAuthorization:
        if definition.permission_mode is PermissionMode.NONE:
            raise PolicyRefusal(
                "permission/not-applicable",
                f"{definition.plan_tool_name} does not accept user permission.",
            )
        key = self._key(definition.action_name, board_id)
        with self._guard:
            active = self._current_state_locked(key)
            if definition.permission_mode is PermissionMode.FRESH_ONE_TIME:
                active = None

            if user_permission is None:
                if active is None or active.mode is not GrantMode.FULL_SESSION:
                    raise self._permission_request(definition)
                grant = active
            elif user_permission == GrantMode.ONE_TIME.value:
                if (max_calls, max_calls_buffer) != (1, 0):
                    raise PolicyRefusal(
                        "permission/one-time-budget",
                        "one-time permission requires max_calls=1 and max_calls_buffer=0.",
                    )
                # An existing full-session grant is not silently downgraded.
                grant = (
                    active
                    if active is not None and active.mode is GrantMode.FULL_SESSION
                    else self._grant_locked(key, GrantMode.ONE_TIME)
                )
            elif user_permission == GrantMode.FULL_SESSION.value:
                if definition.permission_mode is PermissionMode.FRESH_ONE_TIME:
                    raise PolicyRefusal(
                        "permission/fresh-one-time-required",
                        f"{definition.plan_tool_name} requires fresh one-time permission; "
                        "full-session permission cannot authorize this action.",
                    )
                grant = self._grant_locked(key, GrantMode.FULL_SESSION)
            else:
                raise self._permission_request(definition)

            return PermissionAuthorization(
                grant_id=grant.grant_id,
                run_id=grant.run_id,
                tool_name=grant.tool_name,
                board_id=grant.board_id,
                mode=grant.mode,
            )

    def _grant_locked(
        self,
        key: tuple[str, str],
        mode: GrantMode,
    ) -> PermissionGrant:
        grant = PermissionGrant(
            grant_id=f"permission-{secrets.token_hex(8)}",
            run_id=self.server_run.run_id,
            tool_name=key[0],
            board_id=key[1],
            mode=mode,
            granted_at=utc_now_text(),
        )
        self.server_run.permissions[key] = grant
        return grant

    def validate_execution(
        self,
        definition: PlanDefinition,
        board_id: str,
        authorization: object,
    ) -> None:
        if not isinstance(authorization, PermissionAuthorization):
            raise PolicyRefusal(
                "permission/invalid-authorization",
                "The plan has no valid structured permission authorization.",
            )
        key = self._key(definition.action_name, board_id)
        with self._guard:
            active = self._current_state_locked(key)
            if (
                active is None
                or authorization.run_id != self.server_run.run_id
                or authorization.tool_name != definition.action_name
                or authorization.board_id != board_id
                or authorization.grant_id != active.grant_id
            ):
                raise PolicyRefusal(
                    "permission/inactive",
                    f"Permission is no longer active for {definition.action_name} on "
                    f"board '{board_id}'; submit a new permission-carrying plan.",
                )

    def consume_execution(
        self,
        definition: PlanDefinition,
        board_id: str,
        authorization: object,
    ) -> None:
        self.validate_execution(definition, board_id, authorization)
        assert isinstance(authorization, PermissionAuthorization)
        if authorization.mode is not GrantMode.ONE_TIME:
            return
        key = self._key(definition.action_name, board_id)
        with self._guard:
            active = self._current_state_locked(key)
            if active is not None and active.grant_id == authorization.grant_id:
                self.server_run.permissions.pop(key, None)

    def revoke(
        self,
        tool_name: str,
        board_id: str,
        *,
        reason: str,
    ) -> PermissionRevocation:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("revocation reason must be non-empty")
        key = self._key(tool_name, board_id)
        with self._guard:
            grant = self._current_state_locked(key)
            self.server_run.permissions.pop(key, None)
            callback = self._on_revoke
        if callback is not None:
            callback(tool_name, board_id, normalized_reason)
        return PermissionRevocation(
            tool_name=tool_name,
            board_id=board_id,
            reason=normalized_reason,
            revoked_at=utc_now_text(),
            grant_id=grant.grant_id if grant is not None else None,
            revoked=grant is not None,
        )

    def reset(self) -> None:
        """Revoke every grant and invalidate matching plans at Server Run end."""

        with self._guard:
            keys = [
                key
                for key, value in self.server_run.permissions.items()
                if isinstance(key, tuple) and len(key) == 2 and isinstance(value, PermissionGrant)
            ]
            self.server_run.permissions.clear()
            callback = self._on_revoke
        if callback is not None:
            for tool_name, board_id in keys:
                callback(tool_name, board_id, "Server Run ended")
