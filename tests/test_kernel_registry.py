from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import AsyncIterator

import anyio
import mcp.types as types
import pytest
from mcp.client.session import ClientSession
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_client_server_memory_streams

from pyocd_debug_mcp.kernel.registry import RegistryFastMCP, ToolRegistry
from pyocd_debug_mcp.kernel.operations import cancellation_checkpoint, operation_resources


def first_text(result: types.CallToolResult) -> str:
    content = result.content[0]
    assert isinstance(content, types.TextContent)
    return content.text


def test_registry_visibility_never_authorizes_a_handler() -> None:
    registry = ToolRegistry()
    registry.register(
        "guarded_action",
        hidden=True,
        locked=True,
        prerequisite="guarded_action-plan",
    )

    assert "guarded_action" not in registry.advertised()
    with pytest.raises(ToolError, match="guarded_action-plan"):
        registry.require_unlocked("guarded_action", "board_a")

    registry.unlock("guarded_action", "board_a")
    assert "guarded_action" in registry.advertised()
    registry.require_unlocked("guarded_action", "board_a")
    with pytest.raises(ToolError, match="board_b.*guarded_action-plan"):
        registry.require_unlocked("guarded_action", "board_b")

    registry.relock("guarded_action", "board_a")
    assert "guarded_action" not in registry.advertised()
    with pytest.raises(ToolError, match="guarded_action-plan"):
        registry.require_unlocked("guarded_action", "board_a")


def test_registry_list_revision_changes_only_when_advertised_list_changes() -> None:
    registry = ToolRegistry()
    registry.register("guarded", hidden=True, locked=True, prerequisite="guarded-plan")
    initial = registry.list_revision

    registry.unlock("guarded", "board_a")
    first_unlock = registry.list_revision
    registry.unlock("guarded", "board_b")
    second_unlock = registry.list_revision
    registry.relock("guarded", "board_a")
    first_relock = registry.list_revision
    registry.relock("guarded", "board_b")

    assert first_unlock == initial + 1
    assert second_unlock == first_unlock
    assert first_relock == second_unlock
    assert registry.list_revision == first_relock + 1


@asynccontextmanager
async def connected_session(
    server: RegistryFastMCP,
    message_handler,
) -> AsyncIterator[tuple[ClientSession, types.InitializeResult]]:
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
                    result = await session.initialize()
                    yield session, result
            finally:
                task_group.cancel_scope.cancel()


async def test_installed_sdk_cancelled_notification_reaches_managed_operation() -> None:
    server = RegistryFastMCP("sdk-cancellation-test")
    started = threading.Event()
    cleaned = threading.Event()

    @server.tool()
    def cancellable_read(board_id: str, slow: bool) -> str:
        del board_id
        if not slow:
            return "next-call-succeeded"
        operation_resources().stop_io.append(cleaned.set)
        started.set()
        while True:
            cancellation_checkpoint()
            time.sleep(0.01)

    async def ignore_message(message) -> None:  # type: ignore[no-untyped-def]
        del message

    async with connected_session(server, ignore_message) as (session, _initialization):
        request_id = session._request_id  # type: ignore[reportPrivateUsage]
        call = asyncio.create_task(
            session.call_tool(
                "cancellable_read", {"board_id": "board_a", "slow": True}
            )
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        await session.send_notification(
            types.ClientNotification(
                types.CancelledNotification(
                    params=types.CancelledNotificationParams(
                        requestId=request_id,
                        reason="Task 17 SDK cancellation test",
                    )
                )
            )
        )
        with pytest.raises(McpError, match="Request cancelled"):
            await call
        assert await asyncio.to_thread(cleaned.wait, 1.0)

        next_result = await session.call_tool(
            "cancellable_read", {"board_id": "board_a", "slow": False}
        )
        assert first_text(next_result) == "next-call-succeeded"


async def test_in_process_dynamic_list_and_notification_preserve_physical_lock() -> None:
    server = RegistryFastMCP("registry-test")
    first_list_changed = anyio.Event()
    second_list_changed = anyio.Event()
    notification_count = 0

    @server.tool()
    def guarded_echo(board_id: str, text: str) -> str:
        return f"{board_id}:{text}"

    server.registry.configure(
        "guarded_echo",
        hidden=True,
        locked=True,
        prerequisite="guarded_echo-plan",
    )

    @server.tool()
    def unlock_echo(board_id: str) -> str:
        server.registry.unlock("guarded_echo", board_id)
        return "unlocked"

    @server.tool()
    def relock_echo(board_id: str) -> str:
        server.registry.relock("guarded_echo", board_id)
        return "relocked"

    async def handle_message(message) -> None:
        nonlocal notification_count
        if isinstance(message, types.ServerNotification) and isinstance(
            message.root,
            types.ToolListChangedNotification,
        ):
            notification_count += 1
            if notification_count == 1:
                first_list_changed.set()
            if notification_count == 2:
                second_list_changed.set()

    async with connected_session(server, handle_message) as (session, initialize_result):
        assert initialize_result.capabilities.tools is not None
        assert initialize_result.capabilities.tools.listChanged is True

        initial_names = {tool.name for tool in (await session.list_tools()).tools}
        assert initial_names == {"relock_echo", "unlock_echo"}

        with pytest.raises(ToolError, match="guarded_echo-plan"):
            await server.call_tool(
                "guarded_echo",
                {"board_id": "board_a", "text": "direct"},
            )
        direct_locked_call = await session.call_tool(
            "guarded_echo",
            {"board_id": "board_a", "text": "hidden"},
        )
        assert direct_locked_call.isError is True
        assert "guarded_echo-plan" in first_text(direct_locked_call)

        unlock_result = await session.call_tool("unlock_echo", {"board_id": "board_a"})
        assert unlock_result.isError is not True
        with anyio.fail_after(1.0):
            await first_list_changed.wait()

        unlocked_names = {tool.name for tool in (await session.list_tools()).tools}
        assert unlocked_names == {"guarded_echo", "relock_echo", "unlock_echo"}
        wrong_board = await session.call_tool(
            "guarded_echo",
            {"board_id": "board_b", "text": "wrong"},
        )
        assert wrong_board.isError is True
        assert "board_b" in first_text(wrong_board)

        right_board = await session.call_tool(
            "guarded_echo",
            {"board_id": "board_a", "text": "ok"},
        )
        assert right_board.isError is not True
        assert first_text(right_board) == "board_a:ok"

        relock_result = await session.call_tool("relock_echo", {"board_id": "board_a"})
        assert relock_result.isError is not True
        with anyio.fail_after(1.0):
            await second_list_changed.wait()

        relocked_names = {tool.name for tool in (await session.list_tools()).tools}
        assert relocked_names == {"relock_echo", "unlock_echo"}
        stale_client_call = await session.call_tool(
            "guarded_echo",
            {"board_id": "board_a", "text": "stale"},
        )
        assert stale_client_call.isError is True
        assert "guarded_echo-plan" in first_text(stale_client_call)
