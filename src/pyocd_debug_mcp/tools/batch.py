"""Sequential same-board batches that reuse the standard MCP dispatch path."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from pyocd_debug_mcp.capabilities.matrix import route_names
from pyocd_debug_mcp.kernel.operations import SAFE_EXIT_REMINDER
from pyocd_debug_mcp.monitor.tools import MONITOR_TOOL_NAMES
from pyocd_debug_mcp.setup_flow.setup import TERMINAL_SETUP_STATUSES
from pyocd_debug_mcp.setup_flow.validate import VALIDATION_STATUSES


_PENDING_STATUSES = frozenset(
    {
        "setup_needs_user_input",
        "setup_research_required",
        # continue_setup only stages evidence; board_fix_setup must still run.
        "setup_continuation_accepted",
        "validation_needs_user_input",
        "validation_research_required",
    }
)
_FAILED_STATUSES = frozenset(
    {
        "error",
        "refused",
        "failed",
        "setup_tool_not_loaded",
        "setup_policy_recovery_required",
        "setup_plan_ineligible",
        "setup_assignment_required",
        "setup_continuation_unavailable",
        "setup_overview_unavailable",
        "setup_status_unavailable",
        "safety_refresh_blocked",
        "policy_restore_blocked",
    }
) | (set(TERMINAL_SETUP_STATUSES) - {"setup_completed"} - _PENDING_STATUSES) | (
    set(VALIDATION_STATUSES) - {"validation_passed"} - _PENDING_STATUSES
)
_RESULT_STATUSES = {
    **{name: {"ok", "error", "refused"} for name in route_names("raw")},
    "load_setup_tool": {
        "setup_tool_loaded",
        "setup_policy_recovery_required",
        "setup_plan_ineligible",
        "setup_assignment_required",
    },
    "board_setup": set(TERMINAL_SETUP_STATUSES),
    "board_fix_setup": set(TERMINAL_SETUP_STATUSES),
    "continue_setup": {
        "setup_continuation_accepted",
        "setup_research_required",
        "setup_continuation_unavailable",
    },
    "board_validate": {*VALIDATION_STATUSES, "setup_tool_not_loaded"},
    "board_safety_refresh": {
        "safety_refresh_completed",
        "safety_refresh_replayed",
        "safety_refresh_blocked",
        "setup_tool_not_loaded",
    },
    "board_policy_restore": {
        "policy_restore_completed",
        "policy_restore_blocked",
        "setup_tool_not_loaded",
    },
}


class BatchChild(BaseModel):
    """One JSON-only MCP child call with no extra or authority-bearing fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]


ChildDispatcher = Callable[[str, dict[str, Any]], Awaitable[Any]]
ToolLookup = Callable[[str], bool]
ChildResultNormalizer = Callable[[str, str, JsonValue], JsonValue]
ChildErrorNormalizer = Callable[[str, str, BaseException], JsonValue | None]


class BatchValidationError(ValueError):
    """The complete child list failed structural validation before execution."""


def _validate_children(
    board_id: str,
    children: list[BatchChild],
    *,
    tool_exists: ToolLookup,
) -> tuple[BatchChild, ...]:
    board = board_id.strip()
    if not board:
        raise BatchValidationError("board_id must be non-empty")
    if board != board_id:
        raise BatchValidationError("board_id must not contain surrounding whitespace")
    if not children:
        raise BatchValidationError("actions must contain at least one child call")
    validated: list[BatchChild] = []
    for index, child in enumerate(children):
        name = child.tool_name.strip()
        if name.casefold() == "action_batch":
            raise BatchValidationError(
                f"actions[{index}] is a nested action_batch; nested batches are forbidden"
            )
        if name in MONITOR_TOOL_NAMES:
            # Monitoring actions are not board work and must stay off the path that
            # exists to sequence board work.
            raise BatchValidationError(
                f"actions[{index}] names monitoring tool '{name}'; monitoring tools "
                "are not batchable and must be called directly"
            )
        if name != child.tool_name:
            raise BatchValidationError(
                f"actions[{index}].tool_name must not contain surrounding whitespace"
            )
        if not tool_exists(name):
            raise BatchValidationError(f"actions[{index}] names unknown tool '{name}'")
        child_board = child.arguments.get("board_id")
        if not isinstance(child_board, str) or not child_board:
            raise BatchValidationError(f"actions[{index}] must contain a non-empty string board_id")
        if child_board != board:
            raise BatchValidationError(
                f"actions[{index}] targets board '{child_board}', not shared board '{board}'"
            )
        validated.append(child)
    return tuple(validated)


def _json_result(value: Any) -> JsonValue:
    if isinstance(value, list):
        # ``FastMCP.call_tool`` returns one TextContent wrapper for ordinary
        # text tools. A stable-client fallback promises the child tool's
        # public text payload, not an implementation-specific content list.
        if len(value) == 1:
            item = value[0]
            text = getattr(item, "text", None)
            if isinstance(text, str):
                return text
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                return item["text"]
        return [
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in value
        ]
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if value is None or isinstance(value, (str, int, float, bool, dict)):
        return value
    return str(value)


def _child_stop(tool_name: str, result: JsonValue) -> tuple[str, str, str] | None:
    """Inspect the public outcome without rewriting the child's result payload.

    Legacy tools return human-readable text and metadata tools need not have a
    status. Require a status only for actions whose existing contract defines it.
    """

    expected = _RESULT_STATUSES.get(tool_name)
    payload = result
    malformed = (
        "batch_failed",
        "MalformedChildResult",
        "Child did not return a valid result for its public contract.",
    )
    if isinstance(payload, str):
        body = payload.partition(f"\n{SAFE_EXIT_REMINDER}")[0].strip()
        if body.startswith("Refused ["):
            return "batch_failed", "ChildResultError", body.split("\n", 1)[0]
        if not body:
            return malformed
        if expected is not None or body.startswith(("{", "[")):
            try:
                payload = json.loads(body)
            except ValueError:
                return malformed
        else:
            return None
    if not isinstance(payload, dict):
        return malformed
    if "status" not in payload and expected is None:
        return None
    status = payload.get("status")
    if not isinstance(status, str) or not status or (expected is not None and status not in expected):
        return malformed
    if status in _PENDING_STATUSES:
        return "batch_pending", "ChildPending", f"Child requires continuation ({status})."
    if status in _FAILED_STATUSES:
        return "batch_failed", "ChildResultError", f"Child returned {status}."
    return None


def build_batch_handlers(
    dispatch_child: ChildDispatcher,
    *,
    tool_exists: ToolLookup,
    normalize_child_result: ChildResultNormalizer | None = None,
    normalize_child_error: ChildErrorNormalizer | None = None,
) -> dict[str, Callable[..., Awaitable[str]]]:
    """Build the batch tool without introducing a second authorization path."""

    async def action_batch(board_id: str, actions: list[BatchChild]) -> str:
        """Execute bounded same-board child calls through their normal dispatch path.

        Use an accepted *-plan's server-generated one-child fallback unchanged when a client's
        callable bindings remain static. Never invent hidden children or use a batch to bypass
        plans, permission, validation, gates, freshness, timeouts, budgets, locks, or cleanup;
        every child independently traverses those normal checks.
        Stop at the first error, refusal, malformed result, or pending setup/validation
        continuation. completed contains only the successful prefix; failure identifies the
        stopped child and retains its result. batch_pending requires following that child's
        continuation before submitting remaining work. Earlier effects are not rolled back.
        """

        validated = _validate_children(board_id, actions, tool_exists=tool_exists)
        completed: list[dict[str, JsonValue]] = []
        failure: dict[str, JsonValue] | None = None
        status = "batch_completed"

        for index, child in enumerate(validated):
            arguments = dict(child.arguments)
            try:
                result = _json_result(await dispatch_child(child.tool_name, arguments))
                if normalize_child_result is not None:
                    result = normalize_child_result(board_id, child.tool_name, result)
            except Exception as exc:  # child dispatch owns the typed authorization failure
                normalized_error = (
                    normalize_child_error(board_id, child.tool_name, exc)
                    if normalize_child_error is not None
                    else None
                )
                failure = {
                    "index": index,
                    "tool_name": child.tool_name,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "result": normalized_error if normalized_error is not None else str(exc),
                }
                status = "batch_failed"
                break
            stopped = _child_stop(child.tool_name, result)
            if stopped is not None:
                status, error_type, message = stopped
                failure = {
                    "index": index,
                    "tool_name": child.tool_name,
                    "result": result,
                    "error_type": error_type,
                    "message": message,
                }
                break
            completed.append(
                {
                    "index": index,
                    "tool_name": child.tool_name,
                    "result": result,
                }
            )

        payload: dict[str, JsonValue] = {
            "status": status,
            "board_id": board_id,
            "completed": list(completed),
            "failure": dict(failure) if failure is not None else None,
        }
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"{body}\n{SAFE_EXIT_REMINDER}"

    return {"action_batch": action_batch}


__all__ = [
    "BatchChild",
    "BatchValidationError",
    "ChildErrorNormalizer",
    "ChildResultNormalizer",
    "build_batch_handlers",
]
