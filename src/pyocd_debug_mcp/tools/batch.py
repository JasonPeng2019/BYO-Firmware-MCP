"""Sequential same-board batches that reuse the standard MCP dispatch path."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from pyocd_debug_mcp.kernel.operations import SAFE_EXIT_REMINDER
from pyocd_debug_mcp.monitor.tools import MONITOR_TOOL_NAMES

class BatchChild(BaseModel):
    """One JSON-only MCP child call with no extra or authority-bearing fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]


ChildDispatcher = Callable[[str, dict[str, Any]], Awaitable[Any]]
ToolLookup = Callable[[str], bool]


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
            raise BatchValidationError(
                f"actions[{index}] names unknown tool '{name}'"
            )
        child_board = child.arguments.get("board_id")
        if not isinstance(child_board, str) or not child_board:
            raise BatchValidationError(
                f"actions[{index}] must contain a non-empty string board_id"
            )
        if child_board != board:
            raise BatchValidationError(
                f"actions[{index}] targets board '{child_board}', not shared board '{board}'"
            )
        validated.append(child)
    return tuple(validated)


def _json_result(value: Any) -> JsonValue:
    if isinstance(value, list):
        return [
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item
            for item in value
        ]
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if value is None or isinstance(value, (str, int, float, bool, dict)):
        return value
    return str(value)


def build_batch_handlers(
    dispatch_child: ChildDispatcher,
    *,
    tool_exists: ToolLookup,
) -> dict[str, Callable[..., Awaitable[str]]]:
    """Build the batch tool without introducing a second authorization path."""

    async def action_batch(board_id: str, actions: list[BatchChild]) -> str:
        """Execute bounded same-board child calls through their normal dispatch path.

        Use an accepted *-plan's server-generated one-child fallback unchanged when a client's
        callable bindings remain static. Never invent hidden children or use a batch to bypass
        plans, permission, validation, gates, freshness, timeouts, budgets, locks, or cleanup;
        every child independently traverses those normal checks.
        """

        validated = _validate_children(board_id, actions, tool_exists=tool_exists)
        completed: list[dict[str, JsonValue]] = []
        failure: dict[str, JsonValue] | None = None

        for index, child in enumerate(validated):
            arguments = dict(child.arguments)
            try:
                result = await dispatch_child(child.tool_name, arguments)
            except Exception as exc:  # child dispatch owns the typed authorization failure
                failure = {
                    "index": index,
                    "tool_name": child.tool_name,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
                break
            completed.append(
                {
                    "index": index,
                    "tool_name": child.tool_name,
                    "result": _json_result(result),
                }
            )

        payload: dict[str, JsonValue] = {
            "status": "batch_completed" if failure is None else "batch_failed",
            "board_id": board_id,
            "completed": list(completed),
            "failure": dict(failure) if failure is not None else None,
        }
        body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return f"{body}\n{SAFE_EXIT_REMINDER}"

    return {"action_batch": action_batch}


__all__ = [
    "BatchChild",
    "BatchValidationError",
    "build_batch_handlers",
]
