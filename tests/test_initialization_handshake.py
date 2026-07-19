from __future__ import annotations

import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import mcp.types as types
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session

from pyocd_debug_mcp import server, zephyr_build
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
    assert "one unique, familiar name for each board" in prose
    assert "Other visible debug probes may remain unassigned" in prose
    assert 'or "no board"' in prose
    assert '"no board" is a literal sentinel' in prose
    assert "pass it by itself" in prose
    assert "never a candidate board name" in prose
    assert "Never ask the user for structured data" in prose
    assert "board IDs" in prose
    assert "connection IDs" in prose
    assert "permission values" in prose
    assert "repair route for an incomplete same-identity profile" in prose
    assert "safety-refresh route for a stable-map problem" in prose
    assert "unknown name to setup" in prose
    assert "Follow the server-returned repair route" in prose
    assert "Never silently choose, rename, reassign, or rewrite a profile" in prose
    assert "Ordinary conversation is never permission" in prose
    assert "pass approval only through the exact structured parameter" in prose
    assert "Never reuse another board's validation, approval, plan" in prose
    assert "After a disconnect or the end of this Server Run, repeat validation" in prose
    assert "If no board is connected, do not begin setup, validation, or hardware actions" in prose
    assert "Never expose structured payloads, continuation tokens, or internal field names" in prose
    assert "bounded local-first discovery" in prose
    assert "STM32CubeIDE-provided STM32Cube, ThreadX" in prose
    assert "never recursively crawl the whole disk" in prose
    assert "network download only when no compatible local copy exists" in prose
    assert "always-visible collect_build_artifacts MCP tool" in prose
    assert 'expected_roles to ["elf", "map"]' in prose
    assert "Continue with the matching flash plan" in prose
    assert "use board_safety_refresh only for an actual stable-map problem" in prose
    assert "never treat collection as validation" in prose.casefold()
    assert "exactly three trigger categories" in prose
    for trigger in ("initial setup or server restart", "disconnect, reconnect, probe change", "identity repair, mismatch, or destructive recovery"):
        assert trigger in prose
    for nontrigger in ("ordinary build or relink", "flash", "reset or halt", "UART work", "safety refresh or full map reconstruction", "artifact collection", "bookkeeping"):
        assert nontrigger in prose


def test_handshake_is_visible_at_server_run_start() -> None:
    assert "initialization_handshake" in server.tool_registry.advertised()


def test_m4_pilot_actions_start_hidden_with_generated_plan_tools_visible() -> None:
    advertised = set(server.tool_registry.advertised())
    runtime_tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}

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


async def test_in_process_client_lists_and_uses_visible_artifact_collector(
    tmp_path: Path,
) -> None:
    elf = tmp_path / "native-app.out"
    linker_map = tmp_path / "native-app.linkermap"
    elf.write_bytes(b"elf")
    linker_map.write_bytes(b"map")
    output = tmp_path / "canonical"

    async with create_connected_server_and_client_session(server.mcp) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        collector = tools["collect_build_artifacts"]
        assert "after a native IDE or CLI build" in (collector.description or "")
        assert set(collector.inputSchema.get("properties", {})) == {
            "output_dir",
            "elf_path",
            "hex_path",
            "bin_path",
            "map_path",
            "expected_roles",
        }
        assert set(
            collector.inputSchema["properties"]["expected_roles"]["anyOf"][0]["items"]["enum"]
        ) == {
            "elf",
            "hex",
            "bin",
            "map",
        }
        result = await session.call_tool(
            "collect_build_artifacts",
            {
                "output_dir": str(output),
                "elf_path": str(elf),
                "map_path": str(linker_map),
                "expected_roles": ["elf", "map"],
            },
        )
        refused = await session.call_tool(
            "collect_build_artifacts",
            {
                "output_dir": str(output),
                "elf_path": str(elf),
            },
        )

    assert result.isError is not True
    content = result.content[0]
    assert isinstance(content, types.TextContent)
    payload = json.loads(content.text)
    assert payload["status"] == "artifacts_collected"
    assert payload["authority"] == "provenance_only"
    assert payload["safety_handoff"]["status"] == "flash_plan_artifact_available"
    assert "flash plan" in payload["safety_handoff"]["next_step"]
    assert "Refresh only if the stable map is invalid" in payload["safety_handoff"]["next_step"]
    assert payload["canonical_paths"]["elf"] == str(output.resolve() / "firmware.elf")
    assert (output / "firmware.map").read_bytes() == b"map"
    assert refused.isError is not True
    refused_content = refused.content[0]
    assert isinstance(refused_content, types.TextContent)
    refused_payload = json.loads(refused_content.text)
    assert refused_payload["status"] == "artifact_collection_refused"
    assert "new or empty output_dir" in refused_payload["remedy"]


async def test_generic_mcp_collection_and_labeled_zephyr_fallback_share_one_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native_elf = tmp_path / "native" / "application.out"
    native_map = tmp_path / "native" / "application.linkermap"
    native_elf.parent.mkdir()
    native_elf.write_bytes(b"native-elf")
    native_map.write_bytes(b"native-map")

    app_dir = tmp_path / "zephyr-app"
    build_dir = tmp_path / "zephyr-build"
    app_dir.mkdir()

    def fake_west(command: list[str], **_kwargs: object) -> None:
        output = Path(command[command.index("-d") + 1]) / "zephyr"
        output.mkdir(parents=True, exist_ok=True)
        (output / "zephyr.elf").write_bytes(b"zephyr-elf")
        (output / "zephyr.map").write_bytes(b"zephyr-map")

    monkeypatch.setattr(zephyr_build, "_run", fake_west)
    runtime = zephyr_build.ZephyrRuntime(
        workspace_dir=tmp_path / "workspace",
        workspace_source="bounded-test",
        sdk_dir=tmp_path / "sdk",
        sdk_source="bounded-test",
        west_python=Path(sys.executable),
        managed_workspace_dir=tmp_path / "managed",
    )
    zephyr_build.run_build(
        Namespace(
            app_dir=str(app_dir),
            build_dir=str(build_dir),
            board="configured/board-target",
            pristine="auto",
        ),
        runtime,
    )

    async with create_connected_server_and_client_session(server.mcp) as session:
        native = await session.call_tool(
            "collect_build_artifacts",
            {
                "output_dir": str(tmp_path / "native-collected"),
                "elf_path": str(native_elf),
                "map_path": str(native_map),
                "expected_roles": ["elf", "map"],
            },
        )
        fallback = await session.call_tool(
            "collect_build_artifacts",
            {
                "output_dir": str(tmp_path / "zephyr-collected"),
                "elf_path": str(build_dir / "firmware.elf"),
                "map_path": str(build_dir / "firmware.map"),
                "expected_roles": ["elf", "map"],
            },
        )

    contents = [result.content[0] for result in (native, fallback)]
    assert all(isinstance(content, types.TextContent) for content in contents)
    payloads = [json.loads(content.text) for content in contents if isinstance(content, types.TextContent)]
    assert [sorted(payload["artifacts"]) for payload in payloads] == [["elf", "map"]] * 2
    assert all(payload["authority"] == "provenance_only" for payload in payloads)


def test_handshake_guidance_contains_every_required_operating_rule() -> None:
    guidance = build_initialization_guidance(server.tool_registry)
    assert_required_guidance(guidance)


def test_server_handshake_exposes_non_authorizing_run_evidence() -> None:
    guidance = server.initialization_handshake()

    assert f"run_id: {server.server_run.run_id}" in guidance
    assert f"started_at: {server.server_run.started_at_text}" in guidance
    assert "grant no authority" in guidance


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


def test_static_binding_fallback_is_narrowly_documented() -> None:
    prose = " ".join(build_initialization_guidance(server.tool_registry).split())
    assert "static callable bindings" in prose
    assert "exact server-returned single-child action_batch fallback" in prose
    assert "never invent a hidden child name" in prose
    assert "identical plan, permission, validation, gate, freshness" in prose
