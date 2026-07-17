from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import anyio
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from pyocd_debug_mcp import server
from pyocd_debug_mcp.kernel.operations import SAFE_EXIT_REMINDER


ALWAYS_VISIBLE = {
    "action_batch",
    "connect",
    "disconnect",
    "find_symbol",
    "get_board_info",
    "get_state",
    "halt",
    "initialization_handshake",
    "load_setup_tool",
    "board_setup-plan",
    "board_safety_refresh",
    "board_safety_setup",
    "board_validate",
    "read_cpu_register",
    "read_execution_state",
    "read_memory_symbol",
    "remove_breakpoint",
    "reset_and_run",
    "resume",
    "step",
    "wait",
}
GUARDED = set(server.M5_GUARDED_ACTIONS)
M8_GUARDED = set(server.M8_GUARDED_ACTIONS)
PLAN_VISIBLE = {f"{name}-plan" for name in GUARDED | M8_GUARDED}
M6_GUARDED = set(server.M6_GUARDED_ACTIONS)
RETIRED_LEGACY = {
    "flash_firmware",
    "read_core_register",
    "read_memory",
    "read_memory_block",
    "read_symbol_u32",
    "reset",
    "write_core_register",
    "unlock_recover",
}


@asynccontextmanager
async def _connected_session() -> AsyncIterator[ClientSession]:
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                server.mcp._mcp_server.run,  # type: ignore[reportPrivateUsage]
                server_read,
                server_write,
                server.mcp.create_initialization_options(),
            )
            try:
                async with ClientSession(
                    read_stream=client_read,
                    write_stream=client_write,
                    read_timeout_seconds=timedelta(seconds=2),
                ) as session:
                    await session.initialize()
                    yield session
            finally:
                task_group.cancel_scope.cancel()


def _first_text(result: types.CallToolResult) -> str:
    content = result.content[0]
    assert isinstance(content, types.TextContent)
    return content.text


def _locked_arguments(name: str) -> dict[str, object]:
    arguments: dict[str, dict[str, object]] = {
        "connect_override": {"board_id": "m5-lock-board"},
        "reset_and_halt": {"board_id": "m5-lock-board"},
        "connect_under_reset": {"board_id": "m5-lock-board"},
        "write_cpu_register": {
            "board_id": "m5-lock-board",
            "name": "r0",
            "value": 1,
        },
        "set_execution_state": {
            "board_id": "m5-lock-board",
            "name": "pc",
            "value": "0x08000000",
        },
        "register_write": {
            "board_id": "m5-lock-board",
            "address": "0x40000000",
            "mask": "0xff",
            "value": 1,
        },
        "read_memory_address": {
            "board_id": "m5-lock-board",
            "address": "0x20000000",
        },
        "write_memory": {
            "board_id": "m5-lock-board",
            "symbol_or_address": "counter",
            "value": 1,
        },
        "set_breakpoint": {
            "board_id": "m5-lock-board",
            "symbol_or_address": "main",
        },
        "flash_application": {
            "board_id": "m5-lock-board",
            "artifact": "firmware.hex",
        },
        "flash_bootloader": {
            "board_id": "m5-lock-board",
            "artifact": "bootloader.hex",
        },
        "read_serial": {"board_id": "m5-lock-board"},
        "write_serial": {"board_id": "m5-lock-board", "text": "status"},
        "target_unlock": {
            "board_id": "m5-lock-board",
            "recovery_mechanism": "nrf_pyocd_unlock",
        },
    }
    return arguments[name]


async def test_m5_in_process_surface_is_exact_and_every_hidden_handler_is_locked() -> None:
    server.tool_registry.reset()
    try:
        async with _connected_session() as session:
            advertised = {tool.name for tool in (await session.list_tools()).tools}
            registered = {
                tool.name for tool in server.mcp._tool_manager.list_tools()
            }

            assert advertised == ALWAYS_VISIBLE | PLAN_VISIBLE
            assert registered == advertised | GUARDED | M6_GUARDED | M8_GUARDED
            assert GUARDED.isdisjoint(advertised)
            assert M6_GUARDED.isdisjoint(advertised)
            assert M8_GUARDED.isdisjoint(advertised)
            assert RETIRED_LEGACY.isdisjoint(registered)

            for name in sorted(GUARDED | M8_GUARDED):
                result = await session.call_tool(name, _locked_arguments(name))
                assert result.isError is True, name
                response = _first_text(result)
                assert f"{name}-plan" in response, name
                assert SAFE_EXIT_REMINDER in response, name

            setup_arguments = {
                "board_id": "m6-lock-board",
                "mode": "setup",
                "connection_id": "probe:001",
                "display_name": "Bench Board",
                "mcu_part_number": "Part-Exact",
                "serial_baudrate": 115200,
            }
            for name in sorted(M6_GUARDED):
                result = await session.call_tool(name, setup_arguments)
                assert result.isError is True, name
                assert "board_setup-plan" in _first_text(result), name
    finally:
        server.tool_registry.reset()


def test_m5_every_revised_runtime_and_plan_schema_is_exact() -> None:
    expected_action_fields = {
        "connect": {"board_id", "unique_id", "target", "board_config"},
        "disconnect": {"board_id"},
        "get_board_info": {"board_id"},
        "get_state": {"board_id"},
        "connect_override": {
            "board_id",
            "probe_uid",
            "target_override",
            "external_board_config",
        },
        "halt": {"board_id"},
        "resume": {"board_id"},
        "step": {"board_id"},
        "reset_and_run": {"board_id"},
        "reset_and_halt": {"board_id"},
        "connect_under_reset": {"board_id", "probe_uid", "target_override"},
        "read_cpu_register": {"board_id", "name"},
        "read_execution_state": {"board_id", "name"},
        "write_cpu_register": {"board_id", "name", "value"},
        "set_execution_state": {"board_id", "name", "value"},
        "register_write": {"board_id", "address", "mask", "value"},
        "find_symbol": {"board_id", "query"},
        "read_memory_symbol": {"board_id", "symbol", "width"},
        "read_memory_address": {"board_id", "address", "width", "length"},
        "write_memory": {
            "board_id",
            "symbol_or_address",
            "value",
            "width",
            "allow_address_fallback",
            "reason",
        },
        "flash_application": {"board_id", "artifact", "target_address"},
        "flash_bootloader": {"board_id", "artifact", "target_address"},
        "read_serial": {
            "board_id",
            "expected_text",
            "read_seconds",
            "baudrate",
            "port",
            "reset_on_open",
            "on_exit",
        },
        "write_serial": {
            "board_id",
            "text",
            "baudrate",
            "port",
            "append_newline",
            "timeout_seconds",
            "on_exit",
        },
        "set_breakpoint": {"board_id", "symbol_or_address"},
        "remove_breakpoint": {"board_id", "address"},
        "wait": {"board_id", "ms"},
        "target_unlock": {"board_id", "recovery_mechanism"},
        "load_setup_tool": {"board_id", "tool_name"},
        "board_setup": {
            "board_id",
            "mode",
            "connection_id",
            "display_name",
            "mcu_part_number",
            "serial_baudrate",
        },
        "board_fix_setup": {
            "board_id",
            "mode",
            "connection_id",
            "display_name",
            "mcu_part_number",
            "serial_baudrate",
        },
        "board_validate": {"board_id", "probe_id", "serial_id"},
        "board_safety_setup": {"board_id"},
        "board_safety_refresh": {"board_id"},
        "action_batch": {"board_id", "actions"},
    }
    tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}

    for name, expected in expected_action_fields.items():
        assert set(tools[name].parameters["properties"]) == expected, name
        assert "board_id" in tools[name].parameters["required"], name

    common_plan_fields = {
        "board_id",
        "hypothesis",
        "hypothesis_made",
        "strategy",
        "strategy_evaluated",
        "expected_fail_return",
        "expected_success_return",
        "max_calls",
        "max_calls_buffer",
    }
    for action_name in GUARDED | M8_GUARDED:
        expected = common_plan_fields | {"action_parameters", "user_permission"}
        plan_schema = tools[f"{action_name}-plan"].parameters
        assert set(plan_schema["properties"]) == expected, action_name
        assert plan_schema["additionalProperties"] is False, action_name
        assert plan_schema.get("required", []) == [], action_name

    setup_plan = tools["board_setup-plan"].parameters
    expected_setup = common_plan_fields | {"action_parameters", "user_permission"}
    assert set(setup_plan["properties"]) == expected_setup
    assert setup_plan["additionalProperties"] is False
    assert setup_plan.get("required", []) == []
