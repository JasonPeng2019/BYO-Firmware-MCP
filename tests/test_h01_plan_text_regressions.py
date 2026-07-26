"""Regression coverage for H01 generated-plan metadata isolation."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Mapping

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.guardrails.plan_defs import PLAN_TOOL_DEFINITIONS, PlanDefinition
from pyocd_debug_mcp.tools.plans import forbid_unknown_tool_arguments, register_plan_tools


class _CapturingEngine:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def submit(
        self,
        tool_name: str,
        fields: Mapping[str, object],
        *,
        session_id: str | None = None,
    ) -> SimpleNamespace:
        del tool_name, session_id
        self.calls.append(dict(fields))
        return SimpleNamespace(message="captured")


class H01PlanTextRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Probe behavior adjacent to the generated plan text-preservation policy."""

    @staticmethod
    def _all_null(definition: PlanDefinition) -> dict[str, object]:
        return {field.name: None for field in definition.null_fields}

    @staticmethod
    def _populated_envelope() -> dict[str, object]:
        return {
            "board_id": "board_1",
            "hypothesis": "a specific observable cause is under test",
            "strategy": "perform one bounded comparison against the prediction",
            "hypothesis_made": True,
            "strategy_evaluated": True,
            "expected_fail_return": "the observation differs from the prediction",
            "expected_success_return": "the observation matches the prediction",
            "max_calls": 1,
            "max_calls_buffer": 0,
        }

    def _registered_plan(self) -> tuple[object, _CapturingEngine, PlanDefinition]:
        definition = PLAN_TOOL_DEFINITIONS["connect_override-plan"]
        engine = _CapturingEngine()
        mcp = FastMCP("h01-plan-regressions")
        register_plan_tools(mcp, engine, (definition,), lambda board_id: f"session:{board_id}")
        tool = mcp._tool_manager.get_tool(definition.plan_tool_name)  # type: ignore[reportPrivateUsage]
        self.assertIsNotNone(tool)
        return tool, engine, definition

    async def test_non_text_object_compatibility_and_nested_text_survive_plan_boundary(self) -> None:
        tool, engine, definition = self._registered_plan()
        await tool.run(self._all_null(definition), convert_result=True)  # type: ignore[union-attr]
        fields = self._populated_envelope()
        fields["action_parameters"] = '{"probe_uid":"null","target_override":null,"external_board_config":null}'

        await tool.run(fields, convert_result=True)  # type: ignore[union-attr]

        parameters = engine.calls[-1]["action_parameters"]
        self.assertEqual(
            parameters,
            {"probe_uid": "null", "target_override": None, "external_board_config": None},
        )

    async def test_plan_schema_remains_strict_and_unknown_arguments_fail_before_handler(self) -> None:
        tool, engine, definition = self._registered_plan()
        schema = tool.parameters  # type: ignore[union-attr]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["hypothesis"]["anyOf"][0]["type"], "string")
        self.assertEqual(schema["properties"]["action_parameters"]["anyOf"][0]["type"], "object")

        fields = self._all_null(definition)
        fields["unexpected"] = "value"
        with self.assertRaises(ToolError):
            await tool.run(fields, convert_result=True)  # type: ignore[union-attr]
        self.assertEqual(engine.calls, [])

    async def test_non_plan_strict_helper_retains_sdk_json_string_preparse(self) -> None:
        mcp = FastMCP("h01-non-plan-control")
        received: list[object] = []

        def union_text(value: str | None = None) -> str:
            received.append(value)
            return "ok"

        mcp.add_tool(union_text, name="union_text", structured_output=False)
        forbid_unknown_tool_arguments(mcp, "union_text")
        tool = mcp._tool_manager.get_tool("union_text")  # type: ignore[reportPrivateUsage]
        self.assertIsNotNone(tool)

        await tool.run({"value": "null"}, convert_result=True)  # type: ignore[union-attr]

        self.assertEqual(received, [None])


if __name__ == "__main__":
    unittest.main()
