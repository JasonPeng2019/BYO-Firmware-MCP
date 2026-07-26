"""Adversarial registered-boundary tests for H01 CL-001."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Mapping

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.guardrails.plan_defs import PLAN_TOOL_DEFINITIONS, PlanDefinition
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.tools.plans import register_plan_tools


class _Registry:
    def is_registered(self, name: str) -> bool:
        del name
        return True

    def unlock(self, name: str, board_id: str) -> None:
        del name, board_id

    def relock(self, name: str, board_id: str) -> None:
        del name, board_id

    def is_unlocked(self, name: str, board_id: str | None) -> bool:
        del name, board_id
        return True


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


class H01PlanTextPreservationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise FastMCP's real invocation path, never a direct handler/model surrogate."""

    def _registered_tool(
        self,
        definition: PlanDefinition,
        engine: object,
    ) -> object:
        mcp = FastMCP("h01-plan-text-preservation")
        register_plan_tools(mcp, engine, (definition,), lambda board_id: f"session:{board_id}")
        tool = mcp._tool_manager.get_tool(definition.plan_tool_name)  # type: ignore[reportPrivateUsage]
        self.assertIsNotNone(tool)
        return tool

    @staticmethod
    def _all_null(definition: PlanDefinition) -> dict[str, object]:
        return {field.name: None for field in definition.null_fields}

    @staticmethod
    def _populated_envelope() -> dict[str, object]:
        return {
            "board_id": "board_1",
            "hypothesis": "the observed state has one concrete cause",
            "strategy": "compare the next observation with the predicted result",
            "hypothesis_made": True,
            "strategy_evaluated": True,
            "expected_fail_return": "the observation contradicts the stated cause",
            "expected_success_return": "the observation matches the stated cause",
            "max_calls": 1,
            "max_calls_buffer": 0,
            "action_parameters": {},
        }

    async def test_cl001_json_looking_text_reaches_a_generated_plan_handler_verbatim(self) -> None:
        """No JSON-looking text spelling may be converted before the generated handler."""
        definition = PLAN_TOOL_DEFINITIONS["board_setup-plan"]
        engine = _CapturingEngine()
        tool = self._registered_tool(definition, engine)

        await tool.run(self._all_null(definition), convert_result=True)  # type: ignore[union-attr]
        self.assertEqual(engine.calls[-1], self._all_null(definition))

        for value in ("null", "true", "[]", "{}"):
            with self.subTest(value=value):
                fields = self._populated_envelope()
                fields["hypothesis"] = value
                fields["user_permission"] = value
                await tool.run(fields, convert_result=True)  # type: ignore[union-attr]
                captured = engine.calls[-1]
                self.assertIsInstance(captured["hypothesis"], str)
                self.assertEqual(captured["hypothesis"], value)
                self.assertIsInstance(captured["user_permission"], str)
                self.assertEqual(captured["user_permission"], value)

    async def test_cl001_null_text_reaches_reasoning_validation_as_a_placeholder(self) -> None:
        """Every signed placeholder must reach reasoning validation as literal text."""
        definition = PLAN_TOOL_DEFINITIONS["connect_override-plan"]
        placeholders = ("n/a", "na", "none", "null", "placeholder", "tbd", "todo", "unknown")
        for field_name in ("hypothesis", "strategy", "expected_fail_return", "expected_success_return"):
            for placeholder in placeholders:
                with self.subTest(field=field_name, placeholder=placeholder):
                    engine = PlanEngine(ServerRun(), _Registry())
                    tool = self._registered_tool(definition, engine)
                    await tool.run(self._all_null(definition), convert_result=True)  # type: ignore[union-attr]
                    fields = self._populated_envelope()
                    fields[field_name] = placeholder
                    fields["action_parameters"] = {
                        "probe_uid": None,
                        "target_override": None,
                        "external_board_config": None,
                    }
                    with self.assertRaisesRegex(
                        ToolError,
                        rf"{field_name} must be concrete, not placeholder text",
                    ) as raised:
                        await tool.run(fields, convert_result=True)  # type: ignore[union-attr]
                    self.assertNotIn("must not be NULL", str(raised.exception))

    async def test_cl001_actual_null_keeps_the_existing_initialization_and_field_error_contract(self) -> None:
        definition = PLAN_TOOL_DEFINITIONS["connect_override-plan"]
        engine = PlanEngine(ServerRun(), _Registry())
        tool = self._registered_tool(definition, engine)

        initialized = await tool.run(self._all_null(definition), convert_result=True)  # type: ignore[union-attr]
        self.assertIn("Plan initialization for connect_override-plan.", initialized[0].text)

        fields = self._populated_envelope()
        fields["hypothesis"] = None
        fields["action_parameters"] = {
            "probe_uid": None,
            "target_override": None,
            "external_board_config": None,
        }
        with self.assertRaisesRegex(ToolError, "hypothesis.*must not be NULL"):
            await tool.run(fields, convert_result=True)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
