"""Deterministic human-readable contract generated from live plan definitions."""

from __future__ import annotations

from pyocd_debug_mcp.guardrails.plan_defs import FieldDefinition, PLAN_DEFINITIONS


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _constraints(field: FieldDefinition) -> str:
    values: list[str] = []
    if field.nullable:
        values.append("nullable")
    else:
        values.append("required")
    if field.choices:
        values.append("choices=" + ", ".join(repr(item) for item in field.choices))
    if field.minimum is not None:
        operator = ">" if field.exclusive_minimum else ">="
        values.append(f"{operator} {field.minimum:g}")
    if field.maximum is not None:
        values.append(f"<= {field.maximum:g}")
    if field.min_items is not None:
        values.append(f"min_items={field.min_items}")
    if field.max_items is not None:
        values.append(f"max_items={field.max_items}")
    if field.allow_empty:
        values.append("empty allowed")
    return "; ".join(values)


def render_plan_contract_markdown() -> str:
    """Render all schema-bearing plan metadata without a second handwritten source."""

    lines = [
        "# Current plan-tool contract",
        "",
        "This document is generated from `guardrails/plan_defs.py`, the runtime source of truth.",
        "Do not edit field lists by hand. Regenerate it after changing a plan definition; the",
        "contract test compares this entire file with the live deterministic rendering.",
        "Historical design prose remains under `archive_docs/` and is not runtime authority.",
        "",
        "Every plan tool is first called with its complete NULL envelope. A populated call accepts",
        "only the plan JSON object, binds the exact action parameters below, and rejects extra fields.",
        "",
    ]
    for action_name in sorted(PLAN_DEFINITIONS):
        definition = PLAN_DEFINITIONS[action_name]
        lines.extend(
            [
                f"## `{definition.plan_tool_name}`",
                "",
                f"- Action: `{definition.action_name}`",
                f"- Purpose: {_cell(definition.purpose)}",
                f"- Budget mode: `{definition.budget_mode.value}`",
                f"- Permission mode: `{definition.permission_mode.value}`",
                f"- Safety mode: `{definition.safety_mode.value}`",
                f"- Timeout: `{definition.timeout_seconds:g}` seconds",
                "- Populated plan fields, in order: "
                + ", ".join(f"`{field.name}`" for field in definition.plan_fields),
                "- Exact action-parameter fields, in order: "
                + (
                    ", ".join(f"`{field.name}`" for field in definition.action_fields)
                    if definition.action_fields
                    else "none"
                ),
                "",
                "| Action field | Type | Constraints | Description |",
                "| --- | --- | --- | --- |",
            ]
        )
        for field in definition.action_fields:
            lines.append(
                "| "
                f"`{field.name}` | `{field.field_type.value}` | {_cell(_constraints(field))} | "
                f"{_cell(field.description)} |"
            )
        if not definition.action_fields:
            lines.append("| _none_ | - | - | - |")
        lines.extend(
            [
                "",
                f"Extra instructions: {_cell(definition.extra_instructions)}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"

