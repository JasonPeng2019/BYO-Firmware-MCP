"""Focused regression checks for strict registered-boundary dispatch."""

from __future__ import annotations

import asyncio
import json
import unittest
from contextvars import ContextVar
from types import SimpleNamespace

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.kernel.operations import SAFE_EXIT_REMINDER
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP, ToolRegistry
from pyocd_debug_mcp.tools.batch import build_batch_handlers


class _NotificationSession:
    def __init__(self) -> None:
        self.calls = 0

    async def send_tool_list_changed(self) -> None:
        self.calls += 1


class _NullLock:
    def __enter__(self) -> _NullLock:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _text(result: object) -> str:
    if isinstance(result, tuple):
        result = result[0]
    while isinstance(result, list):
        if len(result) != 1:
            raise AssertionError(f"expected one converted result, got {result!r}")
        result = result[0]
    return result.text  # type: ignore[union-attr]


class H01StrictMcpRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Exercise callers and adjacent dispatch contracts affected by H01."""

    def _mcp(self) -> tuple[RegistryFastMCP, ToolRegistry, _NotificationSession]:
        registry = ToolRegistry()
        mcp = RegistryFastMCP("h01-regressions", registry=registry, timeout_resolver=lambda *_: 1)
        session = _NotificationSession()
        mcp.get_context = lambda: SimpleNamespace(request_id="h01", session=session)  # type: ignore[method-assign]
        return mcp, registry, session

    async def test_validated_values_reach_handler_once_while_guard_and_finalizer_keep_raw_request(self) -> None:
        mcp, registry, _ = self._mcp()
        guard_inputs: list[dict[str, object]] = []
        finalizer_inputs: list[dict[str, object]] = []
        calls: list[int] = []

        def action(board_id: str, value: int, on_exit: dict[str, object] | None = None) -> str:
            del board_id, on_exit
            calls.append(value)
            return str(value)

        mcp.add_tool(action)
        registry.configure("action", hidden=True, locked=True, prerequisite="action-plan")
        mcp.configure_guarded_dispatch(
            "action",
            guard=lambda _name, _board, values: guard_inputs.append(dict(values)),
            lock_for_board=lambda _board: _NullLock(),
        )
        mcp.configure_finalizers(
            lambda _name, _board, values: finalizer_inputs.append(dict(values)) or None
        )
        registry.unlock("action", "board")

        request = {
            "board_id": "board",
            "value": "5",
            "on_exit": {"action": "uart_write", "text": "done"},
        }
        self.assertEqual(_text(await mcp.call_tool("action", request)), "5")
        self.assertEqual(calls, [5])
        self.assertEqual(guard_inputs, [request])
        self.assertEqual(finalizer_inputs, [request])

        with self.assertRaisesRegex(ToolError, "unexpected"):
            await mcp.call_tool("action", {**request, "unexpected": True})
        self.assertEqual(calls, [5])
        self.assertEqual(guard_inputs, [request])
        self.assertEqual(finalizer_inputs, [request])

    async def test_context_injection_and_conversion_survive_once_validated_sync_and_async_calls(self) -> None:
        mcp, _, _ = self._mcp()
        contexts: list[object] = []

        def synchronous(value: int, ctx: Context) -> dict[str, int]:
            contexts.append(ctx)
            return {"value": value}

        async def asynchronous(value: int, ctx: Context) -> dict[str, int]:
            contexts.append(ctx)
            return {"value": value}

        mcp.add_tool(synchronous)
        mcp.add_tool(asynchronous)
        sync_result = await mcp.call_tool("synchronous", {"value": 4})
        async_result = await mcp.call_tool("asynchronous", {"value": 6})
        self.assertEqual(json.loads(_text(sync_result)), {"value": 4})
        self.assertEqual(json.loads(_text(async_result)), {"value": 6})
        self.assertEqual(len(contexts), 2)
        self.assertTrue(
            all(
                getattr(context, "request_id", None) == "h01"
                and getattr(context, "session", None) is mcp.get_context().session
                for context in contexts
            )
        )

    async def test_successful_and_failed_batches_preserve_order_prefix_and_one_reminder(self) -> None:
        mcp, _, _ = self._mcp()
        calls: list[int] = []

        async def child(board_id: str, value: int) -> str:
            del board_id
            calls.append(value)
            if value == 2:
                raise ToolError("refused")
            return str(value)

        mcp.add_tool(child)
        mcp.add_tool(
            build_batch_handlers(mcp.call_tool, tool_exists=mcp.registry.is_registered)["action_batch"],
            name="action_batch",
        )
        completed = await mcp.call_tool(
            "action_batch",
            {"board_id": "board", "actions": [
                {"tool_name": "child", "arguments": {"board_id": "board", "value": 3}},
                {"tool_name": "child", "arguments": {"board_id": "board", "value": 4}},
            ]},
        )
        self.assertEqual(calls, [3, 4])
        self.assertIn('"status":"batch_completed"', _text(completed))

        with self.assertRaises(ToolError) as raised:
            await mcp.call_tool(
                "action_batch",
                {"board_id": "board", "actions": [
                    {"tool_name": "child", "arguments": {"board_id": "board", "value": 1}},
                    {"tool_name": "child", "arguments": {"board_id": "board", "value": 2}},
                    {"tool_name": "child", "arguments": {"board_id": "board", "value": 5}},
                ]},
            )
        body, reminder = str(raised.exception).rsplit("\n", 1)
        self.assertEqual(reminder, SAFE_EXIT_REMINDER)
        payload, end = json.JSONDecoder().raw_decode(body[body.find("{") :])
        self.assertEqual(end, len(body) - body.find("{"))
        self.assertEqual(payload["status"], "batch_failed")
        self.assertEqual([item["index"] for item in payload["completed"]], [0])
        self.assertEqual(payload["failure"]["index"], 1)
        self.assertEqual(calls, [3, 4, 1, 2])

    async def test_concurrent_dispatches_keep_notification_ownership_context_local(self) -> None:
        registry = ToolRegistry()
        mcp = RegistryFastMCP("h01-concurrency", registry=registry, timeout_resolver=lambda *_: 1)
        sessions: ContextVar[_NotificationSession | None] = ContextVar("h01_session", default=None)
        mcp.get_context = lambda: SimpleNamespace(request_id="h01", session=sessions.get())  # type: ignore[method-assign]
        first_relock_finished = asyncio.Event()

        async def relock(board_id: str) -> str:
            if board_id == "one":
                await first_relock_finished.wait()
            registry.relock("relock", board_id)
            if board_id == "two":
                first_relock_finished.set()
            return "done"

        mcp.add_tool(relock)
        registry.configure("relock", hidden=True, locked=True, prerequisite="relock-plan")
        registry.unlock("relock", "one")
        registry.unlock("relock", "two")
        first, second = _NotificationSession(), _NotificationSession()

        async def invoke(board_id: str, session: _NotificationSession) -> None:
            token = sessions.set(session)
            try:
                await mcp.call_tool("relock", {"board_id": board_id})
            finally:
                sessions.reset(token)

        second_task = asyncio.create_task(invoke("two", second))
        await first_relock_finished.wait()
        await invoke("one", first)
        await second_task
        self.assertEqual((first.calls, second.calls), (1, 0))


if __name__ == "__main__":
    unittest.main()
