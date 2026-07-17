from __future__ import annotations

import json
import subprocess
import sys

import mcp.types as types
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session

from pyocd_debug_mcp import server
from pyocd_debug_mcp.guardrails.plan_defs import PLAN_DEFINITIONS
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP
from pyocd_debug_mcp.tools.handshake import (
    build_initialization_guidance,
    register_initialization_handshake,
)


def assert_required_guidance(guidance: str) -> None:
    prose = " ".join(guidance.split())

    assert "intentionally hides" in prose
    assert "currently visible tool list is authoritative" in prose
    assert "never guess, request, or call an unlisted tool" in prose
    assert "Currently visible tools:" in guidance
    assert "initialization_handshake" in guidance
    assert "*-plan" in prose
    assert "every parameter set to NULL" in prose
    assert "one unique, familiar name for each connected board" in prose
    assert 'or "no board"' in prose
    assert "Never ask the user for structured data" in prose
    assert "board IDs" in prose
    assert "connection IDs" in prose
    assert "permission values" in prose
    assert "existing profile name to validation" in prose
    assert "unknown name to setup" in prose
    assert "incomplete or failed profile to repair" in prose
    assert "Never silently choose, rename, reassign, or rewrite a profile" in prose
    assert "Ordinary conversation is never permission" in prose
    assert "pass approval only through the exact structured parameter" in prose
    assert "Never reuse another board's validation, approval, plan" in prose
    assert "After a disconnect or the end of this Server Run, repeat validation" in prose
    assert "If no board is connected, do not begin setup, validation, or hardware actions" in prose
    assert "Never expose structured payloads, continuation tokens, or internal field names" in prose


def test_handshake_is_visible_at_server_run_start() -> None:
    assert "initialization_handshake" in server.tool_registry.advertised()


def test_m4_pilot_actions_start_hidden_with_generated_plan_tools_visible() -> None:
    advertised = set(server.tool_registry.advertised())
    runtime_tools = {
        tool.name: tool for tool in server.mcp._tool_manager.list_tools()
    }

    for action_name in server.PILOT_PLAN_ACTIONS:
        definition = PLAN_DEFINITIONS[action_name]
        assert action_name not in advertised
        assert definition.plan_tool_name in advertised
        assert action_name in runtime_tools
        assert definition.plan_tool_name in runtime_tools
        assert set(runtime_tools[definition.plan_tool_name].parameters["properties"]) == set(
            definition.null_field_names
        )
        assert runtime_tools[definition.plan_tool_name].parameters.get("required", []) == []

        action_properties = set(runtime_tools[action_name].parameters["properties"])
        expected_action_properties = {
            "board_id",
            *(field.name for field in definition.action_fields),
        }
        if action_name in {"read_serial", "write_serial"}:
            expected_action_properties.add("on_exit")
        assert action_properties == expected_action_properties


async def test_in_process_client_lists_and_calls_handshake_before_hardware() -> None:
    async with create_connected_server_and_client_session(server.mcp) as session:
        listed_tools = (await session.list_tools()).tools
        handshake = next(tool for tool in listed_tools if tool.name == "initialization_handshake")

        assert "Call this first" in (handshake.description or "")
        assert handshake.inputSchema.get("properties") == {}
        result = await session.call_tool("initialization_handshake", {})

    assert result.isError is not True
    content = result.content[0]
    assert isinstance(content, types.TextContent)
    assert "Guarded Hardware Server operating guidance" in content.text
    assert_required_guidance(content.text)


def test_handshake_guidance_contains_every_required_operating_rule() -> None:
    guidance = build_initialization_guidance(server.tool_registry)
    assert_required_guidance(guidance)


def test_repeated_handshake_is_side_effect_free() -> None:
    revision_before = server.tool_registry.list_revision
    run_state_before = (
        dict(server.server_run.plans),
        dict(server.server_run.permissions),
        dict(server.server_run.assignments),
        dict(server.server_run.gates),
    )

    first = server.initialization_handshake()
    second = server.initialization_handshake()

    assert first == second
    assert server.tool_registry.list_revision == revision_before
    assert (
        server.server_run.plans,
        server.server_run.permissions,
        server.server_run.assignments,
        server.server_run.gates,
    ) == run_state_before


def test_new_server_runs_start_with_distinct_empty_authority_state() -> None:
    first = server.create_server_run()
    first.plans["example"] = object()
    first.permissions["example"] = object()
    first.assignments["example"] = object()
    first.gates["example"] = object()

    second = server.create_server_run()

    assert first.run_id != second.run_id
    assert second.started_at.tzinfo is not None
    assert second.started_at_text.endswith("Z")
    assert second.plans == {}
    assert second.permissions == {}
    assert second.assignments == {}
    assert second.gates == {}


def test_fresh_interpreter_restart_has_new_empty_server_run() -> None:
    script = """
import json
from pyocd_debug_mcp import server
print(json.dumps({
    "run_id": server.server_run.run_id,
    "plans": server.server_run.plans,
    "permissions": server.server_run.permissions,
    "assignments": server.server_run.assignments,
    "gates": server.server_run.gates,
    "visible": list(server.tool_registry.advertised()),
}, sort_keys=True))
"""

    def start_fresh_interpreter() -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=20.0,
        )
        return json.loads(completed.stdout.strip().splitlines()[-1])

    first = start_fresh_interpreter()
    second = start_fresh_interpreter()

    assert first["run_id"] != second["run_id"]
    for result in (first, second):
        assert result["plans"] == {}
        assert result["permissions"] == {}
        assert result["assignments"] == {}
        assert result["gates"] == {}
        visible = result["visible"]
        assert isinstance(visible, list)
        assert "initialization_handshake" in visible


async def test_handshake_does_not_change_hidden_handler_authorization() -> None:
    mcp = RegistryFastMCP("handshake-authorization-test")

    @mcp.tool()
    def guarded_action(board_id: str) -> str:
        return board_id

    mcp.registry.configure(
        "guarded_action",
        hidden=True,
        locked=True,
        prerequisite="guarded_action-plan",
    )
    register_initialization_handshake(mcp, mcp.registry)
    advertised_before = mcp.registry.advertised()

    with pytest.raises(ToolError, match="guarded_action-plan"):
        await mcp.call_tool("guarded_action", {"board_id": "board_a"})
    await mcp.call_tool("initialization_handshake", {})

    assert mcp.registry.advertised() == advertised_before
    with pytest.raises(ToolError, match="guarded_action-plan"):
        await mcp.call_tool("guarded_action", {"board_id": "board_a"})
