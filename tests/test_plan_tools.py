from __future__ import annotations

import threading
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import AsyncIterator

import anyio
import mcp.types as types
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from pyocd_debug_mcp.guardrails.plan_defs import PLAN_DEFINITIONS
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.services.session_runtime import PolicyRefusal
from pyocd_debug_mcp.tools.plans import register_plan_tools


SESSION = "session-a"


def plan_fields(board_id: str = "board_a") -> dict[str, object]:
    return {
        "board_id": board_id,
        "hypothesis": "The UART capture will contain the expected boot text.",
        "hypothesis_made": True,
        "strategy": "Capture one bounded UART interval and compare its output.",
        "strategy_evaluated": True,
        "expected_fail_return": "The expected text is absent or capture is refused.",
        "expected_success_return": "The expected boot text is captured.",
        "max_calls": 1,
        "max_calls_buffer": 0,
        "action_parameters": {
            "expected_text": "boot ok",
            "read_seconds": 3.0,
            "baudrate": 115200,
            "port": "COM7",
            "reset_on_open": False,
        },
    }


@asynccontextmanager
async def connected_session(
    server: RegistryFastMCP,
    message_handler,
) -> AsyncIterator[ClientSession]:
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                server._mcp_server.run,  # type: ignore[reportPrivateUsage]
                server_read,
                server_write,
                server.create_initialization_options(),
            )
            try:
                async with ClientSession(
                    read_stream=client_read,
                    write_stream=client_write,
                    read_timeout_seconds=timedelta(seconds=2),
                    message_handler=message_handler,
                ) as session:
                    await session.initialize()
                    yield session
            finally:
                task_group.cancel_scope.cancel()


async def ignore_message(message) -> None:
    del message


def make_server() -> RegistryFastMCP:
    server = RegistryFastMCP("generated-plan-tools")
    server_run = ServerRun(run_id="plan-tool-run")
    engine = PlanEngine(server_run, server.registry)

    @server.tool()
    def read_serial(
        board_id: str,
        expected_text: str | None,
        read_seconds: float,
        baudrate: int | None,
        port: str | None,
        reset_on_open: bool,
    ) -> str:
        return f"{board_id}:{expected_text}:{read_seconds}:{baudrate}:{port}:{reset_on_open}"

    server.registry.configure(
        "read_serial",
        hidden=True,
        locked=True,
        prerequisite="read_serial-plan",
    )

    def guard(name: str, board_id: str, arguments: Mapping[str, object]) -> None:
        parameters = {key: value for key, value in arguments.items() if key != "board_id"}
        engine.enforce(name, board_id, parameters, session_id=SESSION)

    locks: dict[str, threading.Lock] = {}
    server.configure_guarded_dispatch(
        "read_serial",
        guard=guard,
        lock_for_board=lambda board_id: locks.setdefault(board_id, threading.Lock()),
    )
    register_plan_tools(
        server,
        engine,
        [PLAN_DEFINITIONS["read_serial"]],
        lambda board_id: SESSION if board_id == "board_a" else "session-b",
    )
    return server


async def test_ac_3_2_and_3_4_generated_plan_drives_mcp_visibility_transitions() -> None:
    server = make_server()
    unlocked = anyio.Event()
    relocked = anyio.Event()
    notifications = 0

    async def handle_message(message) -> None:
        nonlocal notifications
        if isinstance(message, types.ServerNotification) and isinstance(
            message.root,
            types.ToolListChangedNotification,
        ):
            notifications += 1
            (unlocked if notifications == 1 else relocked).set()

    async with connected_session(server, handle_message) as session:
        initial = {tool.name for tool in (await session.list_tools()).tools}
        assert initial == {"read_serial-plan"}

        definition = PLAN_DEFINITIONS["read_serial"]
        all_null = {name: None for name in definition.null_field_names}
        initialized = await session.call_tool("read_serial-plan", all_null)
        assert initialized.isError is not True

        accepted = await session.call_tool("read_serial-plan", plan_fields())
        assert accepted.isError is not True
        with anyio.fail_after(1.0):
            await unlocked.wait()
        assert {tool.name for tool in (await session.list_tools()).tools} == {
            "read_serial-plan",
            "read_serial",
        }

        wrong_board = await session.call_tool(
            "read_serial",
            {
                "board_id": "board_b",
                "expected_text": "boot ok",
                "read_seconds": 3.0,
                "baudrate": 115200,
                "port": "COM7",
                "reset_on_open": False,
            },
        )
        assert wrong_board.isError is True

        result = await session.call_tool(
            "read_serial",
            {
                "board_id": "board_a",
                "expected_text": "boot ok",
                "read_seconds": 3.0,
                "baudrate": 115200,
                "port": "COM7",
                "reset_on_open": False,
            },
        )
        assert result.isError is not True
        with anyio.fail_after(1.0):
            await relocked.wait()
        assert {tool.name for tool in (await session.list_tools()).tools} == {
            "read_serial-plan"
        }


async def test_generated_plan_schema_contains_every_definition_field() -> None:
    server = make_server()
    tools = await server.list_tools()
    plan_tool = next(tool for tool in tools if tool.name == "read_serial-plan")

    definition = PLAN_DEFINITIONS["read_serial"]
    assert set(plan_tool.inputSchema["properties"]) == {
        field.name for field in definition.call_fields
    }
    assert plan_tool.inputSchema["additionalProperties"] is False
    assert plan_tool.inputSchema.get("required", []) == []


async def test_mcp_rejects_unknown_or_flattened_plan_json_before_activation() -> None:
    server = make_server()
    definition = PLAN_DEFINITIONS["read_serial"]

    async with connected_session(server, ignore_message) as session:
        initialized = await session.call_tool(
            definition.plan_tool_name,
            {name: None for name in definition.null_field_names},
        )
        assert initialized.isError is not True

        unknown = await session.call_tool(
            definition.plan_tool_name,
            plan_fields() | {"invented": True},
        )
        assert unknown.isError is True
        unknown_content = unknown.content[0]
        assert isinstance(unknown_content, types.TextContent)
        assert "Extra inputs are not permitted" in unknown_content.text

        flattened = dict(plan_fields())
        parameters = flattened.pop("action_parameters")
        assert isinstance(parameters, dict)
        flattened.update(parameters)
        flat_result = await session.call_tool(definition.plan_tool_name, flattened)
        assert flat_result.isError is True

        permission_field = await session.call_tool(
            definition.plan_tool_name,
            plan_fields() | {"user_permission": None},
        )
        assert permission_field.isError is True

    assert server.registry.is_unlocked("read_serial", "board_a") is False


PILOT_PARAMETERS: dict[str, dict[str, object]] = {
    "read_serial": {
        "expected_text": "boot ok",
        "read_seconds": 3.0,
        "baudrate": 115200,
        "port": "COM7",
        "reset_on_open": False,
    },
    "write_serial": {
        "text": "ping",
        "baudrate": 115200,
        "port": "COM7",
        "append_newline": True,
        "timeout_seconds": 1.0,
    },
    "write_memory": {
        "symbol_or_address": "0x20000000",
        "value": "1",
        "width": 32,
        "allow_address_fallback": True,
        "reason": "The address is pointer-derived and has no stable symbol.",
    },
}
PILOT_BUDGETS = {"read_serial": 2, "write_serial": 2, "write_memory": 1}


def complete_plan_fields(action_name: str) -> dict[str, object]:
    return {
        "board_id": "board_a",
        "hypothesis": f"The bounded {action_name} operation will produce its expected result.",
        "hypothesis_made": True,
        "strategy": f"Execute the exact planned {action_name} parameters and inspect the result.",
        "strategy_evaluated": True,
        "expected_fail_return": "A deterministic plan, session, safety, or backend failure.",
        "expected_success_return": f"The {action_name} operation completes exactly once.",
        "max_calls": PILOT_BUDGETS[action_name],
        "max_calls_buffer": 0,
        "action_parameters": PILOT_PARAMETERS[action_name],
    }


@dataclass(slots=True)
class PilotHarness:
    server: RegistryFastMCP
    engine: PlanEngine
    calls: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    prestart_refusals: set[str] = field(default_factory=set)
    started_failures: set[str] = field(default_factory=set)


def make_all_pilot_server() -> PilotHarness:
    server = RegistryFastMCP("all-m4-pilots")
    engine = PlanEngine(ServerRun(run_id="all-pilot-run"), server.registry)
    harness = PilotHarness(server=server, engine=engine)

    def record(action_name: str, values: dict[str, object]) -> str:
        harness.calls.setdefault(action_name, []).append(values)
        if action_name in harness.started_failures:
            raise RuntimeError(f"{action_name} failed after start")
        return f"executed:{action_name}"

    @server.tool()
    def read_serial(
        board_id: str,
        expected_text: str | None,
        read_seconds: float,
        baudrate: int | None,
        port: str | None,
        reset_on_open: bool,
    ) -> str:
        return record(
            "read_serial",
            {
                "board_id": board_id,
                "expected_text": expected_text,
                "read_seconds": read_seconds,
                "baudrate": baudrate,
                "port": port,
                "reset_on_open": reset_on_open,
            },
        )

    @server.tool()
    def write_serial(
        board_id: str,
        text: str,
        baudrate: int | None,
        port: str | None,
        append_newline: bool,
        timeout_seconds: float,
    ) -> str:
        return record(
            "write_serial",
            {
                "board_id": board_id,
                "text": text,
                "baudrate": baudrate,
                "port": port,
                "append_newline": append_newline,
                "timeout_seconds": timeout_seconds,
            },
        )

    @server.tool()
    def write_memory(
        board_id: str,
        symbol_or_address: str | int,
        value: object,
        width: int,
        allow_address_fallback: bool,
        reason: str | None,
    ) -> str:
        return record(
            "write_memory",
            {
                "board_id": board_id,
                "symbol_or_address": symbol_or_address,
                "value": value,
                "width": width,
                "allow_address_fallback": allow_address_fallback,
                "reason": reason,
            },
        )

    locks: dict[str, threading.Lock] = {}

    def guard(name: str, board_id: str, arguments: Mapping[str, object]) -> None:
        parameters = {key: value for key, value in arguments.items() if key != "board_id"}

        def safety_check() -> None:
            if name in harness.prestart_refusals:
                raise PolicyRefusal("safety/pre-start", "Injected Layer-2 refusal")

        engine.enforce(
            name,
            board_id,
            parameters,
            session_id=SESSION,
            preconditions=safety_check,
        )

    for action_name in PILOT_PARAMETERS:
        server.registry.configure(
            action_name,
            hidden=True,
            locked=True,
            prerequisite=f"{action_name}-plan",
        )
        server.configure_guarded_dispatch(
            action_name,
            guard=guard,
            lock_for_board=lambda board_id: locks.setdefault(board_id, threading.Lock()),
        )

    register_plan_tools(
        server,
        engine,
        (PLAN_DEFINITIONS[name] for name in PILOT_PARAMETERS),
        lambda board_id: SESSION if board_id == "board_a" else "session-b",
    )
    return harness


@pytest.mark.parametrize("action_name", tuple(PILOT_PARAMETERS))
async def test_m4_each_pilot_runs_all_null_to_exhaustion_and_relock(
    action_name: str,
) -> None:
    harness = make_all_pilot_server()
    plan_tool_name = f"{action_name}-plan"
    definition = PLAN_DEFINITIONS[action_name]

    async with connected_session(harness.server, ignore_message) as session:
        initial = {tool.name for tool in (await session.list_tools()).tools}
        assert initial == {f"{name}-plan" for name in PILOT_PARAMETERS}

        all_null = {name: None for name in definition.null_field_names}
        initialized = await session.call_tool(plan_tool_name, all_null)
        assert initialized.isError is not True
        accepted = await session.call_tool(plan_tool_name, complete_plan_fields(action_name))
        assert accepted.isError is not True

        unlocked = {tool.name for tool in (await session.list_tools()).tools}
        assert action_name in unlocked
        assert not (set(PILOT_PARAMETERS) - {action_name}) & unlocked

        action_arguments = {"board_id": "board_a", **PILOT_PARAMETERS[action_name]}
        for call_index in range(PILOT_BUDGETS[action_name]):
            result = await session.call_tool(action_name, action_arguments)
            assert result.isError is not True
            visible = {tool.name for tool in (await session.list_tools()).tools}
            if call_index + 1 < PILOT_BUDGETS[action_name]:
                assert action_name in visible
            else:
                assert action_name not in visible

        assert harness.calls[action_name] == [
            action_arguments for _ in range(PILOT_BUDGETS[action_name])
        ]
        stale_call = await session.call_tool(action_name, action_arguments)
        assert stale_call.isError is True
        assert "-plan" in _first_text(stale_call)


def _first_text(result: types.CallToolResult) -> str:
    content = result.content[0]
    assert isinstance(content, types.TextContent)
    return content.text


async def test_m4_cross_tool_and_cross_board_isolation_preserve_budget() -> None:
    harness = make_all_pilot_server()
    definition = PLAN_DEFINITIONS["read_serial"]
    arguments = {"board_id": "board_a", **PILOT_PARAMETERS["read_serial"]}

    async with connected_session(harness.server, ignore_message) as session:
        await session.call_tool(
            "read_serial-plan",
            {name: None for name in definition.null_field_names},
        )
        await session.call_tool("read_serial-plan", complete_plan_fields("read_serial"))

        wrong_tool = await session.call_tool(
            "write_serial",
            {"board_id": "board_a", **PILOT_PARAMETERS["write_serial"]},
        )
        wrong_board = await session.call_tool(
            "read_serial",
            {"board_id": "board_b", **PILOT_PARAMETERS["read_serial"]},
        )

        assert wrong_tool.isError is True
        assert wrong_board.isError is True
        active = harness.engine.active_plan("read_serial", "board_a")
        assert active is not None and active.remaining_calls == 2

        exact = await session.call_tool("read_serial", arguments)
        assert exact.isError is not True


async def test_m4_prestart_refusal_does_not_burn_dispatch_budget() -> None:
    harness = make_all_pilot_server()
    harness.prestart_refusals.add("read_serial")
    definition = PLAN_DEFINITIONS["read_serial"]
    arguments = {"board_id": "board_a", **PILOT_PARAMETERS["read_serial"]}

    async with connected_session(harness.server, ignore_message) as session:
        await session.call_tool(
            "read_serial-plan",
            {name: None for name in definition.null_field_names},
        )
        await session.call_tool("read_serial-plan", complete_plan_fields("read_serial"))

        refused = await session.call_tool("read_serial", arguments)
        assert refused.isError is True
        assert harness.calls.get("read_serial", []) == []
        active = harness.engine.active_plan("read_serial", "board_a")
        assert active is not None and active.remaining_calls == 2

        harness.prestart_refusals.clear()
        accepted = await session.call_tool("read_serial", arguments)
        assert accepted.isError is not True
        active = harness.engine.active_plan("read_serial", "board_a")
        assert active is not None and active.remaining_calls == 1


async def test_m4_failed_after_start_burns_final_budget_and_relocks() -> None:
    harness = make_all_pilot_server()
    harness.started_failures.add("write_memory")
    definition = PLAN_DEFINITIONS["write_memory"]
    arguments = {"board_id": "board_a", **PILOT_PARAMETERS["write_memory"]}

    async with connected_session(harness.server, ignore_message) as session:
        await session.call_tool(
            "write_memory-plan",
            {name: None for name in definition.null_field_names},
        )
        await session.call_tool("write_memory-plan", complete_plan_fields("write_memory"))

        failed = await session.call_tool("write_memory", arguments)
        assert failed.isError is True
        assert len(harness.calls["write_memory"]) == 1
        assert harness.engine.active_plan("write_memory", "board_a") is None
        assert "write_memory" not in {
            tool.name for tool in (await session.list_tools()).tools
        }
