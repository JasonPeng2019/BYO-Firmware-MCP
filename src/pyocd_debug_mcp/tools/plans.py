"""Generated MCP plan-tool registration from declarative plan definitions."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import ConfigDict, model_validator

from pyocd_debug_mcp.guardrails.plan_defs import (
    FieldDefinition,
    FieldType,
    PermissionMode,
    PlanDefinition,
)
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine


SessionIdResolver = Callable[[str], str | None]


def forbid_unknown_tool_arguments(
    mcp: FastMCP,
    tool_name: str,
    *,
    reject_populated_permission: bool = False,
) -> None:
    """Make a pinned FastMCP argument model preserve the exact plan envelope."""

    tool = mcp._tool_manager.get_tool(tool_name)  # type: ignore[reportPrivateUsage]
    if tool is None:  # pragma: no cover - SDK registration invariant
        raise RuntimeError(f"Tool registration failed: {tool_name}")
    argument_model = tool.fn_metadata.arg_model
    if reject_populated_permission:

        @model_validator(mode="before")
        @classmethod
        def reject_permission_on_populated_plan(
            cls: type[object], value: object
        ) -> object:
            del cls
            if (
                isinstance(value, Mapping)
                and "user_permission" in value
                and any(
                    item is not None
                    for name, item in value.items()
                    if name != "user_permission"
                )
            ):
                raise ValueError(
                    "user_permission must be omitted from a populated non-permission plan"
                )
            return value

        argument_model = type(
            argument_model.__name__,
            (argument_model,),
            {
                "model_config": ConfigDict(
                    arbitrary_types_allowed=True,
                    extra="forbid",
                ),
                "reject_permission_on_populated_plan": reject_permission_on_populated_plan,
            },
        )
        tool.fn_metadata.arg_model = argument_model
    else:
        argument_model.model_config["extra"] = "forbid"
    argument_model.model_rebuild(force=True)
    tool.parameters = argument_model.model_json_schema(by_alias=True)


def _field_annotation(field: FieldDefinition) -> object:
    base: object = {
        FieldType.TEXT: str,
        FieldType.INTEGER: int,
        FieldType.NUMBER: float | int,
        FieldType.BOOLEAN: bool,
        FieldType.ARRAY: list[object],
        FieldType.OBJECT: dict[str, object],
        FieldType.JSON: object,
        FieldType.TEXT_OR_INTEGER: str | int,
    }[field.field_type]
    try:
        return base | None  # type: ignore[operator]
    except TypeError:  # pragma: no cover - defensive for unusual typing objects
        return Any


def build_plan_handler(
    definition: PlanDefinition,
    engine: PlanEngine,
    session_id_for_board: SessionIdResolver,
) -> Callable[..., str]:
    """Build one all-optional callable while preserving a precise MCP schema."""

    def plan_handler(**fields: object) -> str:
        # FastMCP materializes an omitted optional argument as None. Non-permission
        # populated plans omit the field by contract, so remove only that synthetic
        # default while preserving the universal all-NULL initialization envelope.
        if (
            definition.permission_mode is PermissionMode.NONE
            and fields.get("user_permission") is None
            and not all(value is None for value in fields.values())
        ):
            fields.pop("user_permission", None)
        board_value = fields.get("board_id")
        session_id = (
            session_id_for_board(board_value) if isinstance(board_value, str) else None
        )
        return engine.submit(
            definition.plan_tool_name,
            fields,
            session_id=session_id,
        ).message

    plan_handler.__name__ = definition.plan_tool_name.replace("-", "_")
    plan_handler.__doc__ = (
        f"Initialize or submit the immutable plan for {definition.action_name}. "
        "Call first with every parameter NULL."
    )
    plan_handler.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters=[
            inspect.Parameter(
                field.name,
                inspect.Parameter.KEYWORD_ONLY,
                default=None,
                annotation=_field_annotation(field),
            )
            for field in definition.call_fields
        ],
        return_annotation=str,
    )
    return plan_handler


def register_plan_tools(
    mcp: FastMCP,
    engine: PlanEngine,
    definitions: Iterable[PlanDefinition],
    session_id_for_board: SessionIdResolver,
) -> dict[str, Callable[..., str]]:
    """Register visible plan tools generated wholly from their definitions."""

    registered: dict[str, Callable[..., str]] = {}
    for definition in definitions:
        handler = build_plan_handler(definition, engine, session_id_for_board)
        mcp.add_tool(
            handler,
            name=definition.plan_tool_name,
            description=handler.__doc__,
            structured_output=False,
        )
        # The pinned FastMCP SDK otherwise ignores unknown arguments before our
        # strict plan engine can inspect them. Rebuild this generated argument
        # model with Pydantic's fail-closed extra policy and publish the matching
        # additionalProperties=false schema.
        forbid_unknown_tool_arguments(
            mcp,
            definition.plan_tool_name,
            reject_populated_permission=definition.permission_mode is PermissionMode.NONE,
        )
        registered[definition.plan_tool_name] = handler
    return registered
