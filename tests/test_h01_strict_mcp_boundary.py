"""Adversarial registered-boundary checks for H01 batch strictness."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from typing import ClassVar

import anyio
from mcp.client.session import ClientSession
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, field_validator

from pyocd_debug_mcp.kernel.operations import SAFE_EXIT_REMINDER
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP, ToolRegistry
from pyocd_debug_mcp.guardrails.plan_defs import PLAN_TOOL_DEFINITIONS
from pyocd_debug_mcp.tools.batch import build_batch_handlers
from pyocd_debug_mcp.tools.plans import _PlanToolMetadata, register_plan_tools


class _CountingPayload(BaseModel):
    """Records Pydantic validation without making handler execution observable by accident."""

    validations: ClassVar[int] = 0
    value: int

    @field_validator("value")
    @classmethod
    def _count_validation(cls, value: int) -> int:
        cls.validations += 1
        return value


class _NotificationSession:
    def __init__(self) -> None:
        self.calls = 0

    async def send_tool_list_changed(self) -> None:
        self.calls += 1


class _CapturingPlanEngine:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def submit(self, _tool_name: str, fields: dict[str, object], *, session_id: str | None) -> SimpleNamespace:
        del session_id
        self.calls.append(dict(fields))
        return SimpleNamespace(message="captured plan")


class _CountingPlanMetadata(_PlanToolMetadata):
    pre_parse_calls: ClassVar[int] = 0

    def pre_parse_json(self, data: dict[str, object]) -> dict[str, object]:
        type(self).pre_parse_calls += 1
        return super().pre_parse_json(data)


class _ObservingClientSession(ClientSession):
    """Record actual server notifications while retaining ClientSession protocol handling."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.notifications: list[object] = []

    async def _received_notification(self, notification: object) -> None:
        self.notifications.append(notification)
        await super()._received_notification(notification)  # type: ignore[arg-type]


class H01StrictMcpBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """Prove H01 semantics at the registered FastMCP boundary, without hardware."""

    def _mcp(self) -> tuple[RegistryFastMCP, ToolRegistry, _NotificationSession]:
        registry = ToolRegistry()
        mcp = RegistryFastMCP("h01-strict-boundary", registry=registry, timeout_resolver=lambda *_: 1)
        session = _NotificationSession()
        # The production call path obtains its context from a live MCP request.  This is the
        # smallest faithful host-only stand-in needed to observe its notification side effect.
        mcp.get_context = lambda: SimpleNamespace(request_id="h01", session=session)  # type: ignore[method-assign]
        return mcp, registry, session

    @staticmethod
    def _body(error: ToolError) -> dict[str, object]:
        text = str(error)
        body, reminder = text.rsplit("\n", 1)
        if reminder != SAFE_EXIT_REMINDER:
            raise AssertionError(f"missing or duplicated safe-exit reminder: {text!r}")
        start = body.find("{")
        if start < 0:
            raise AssertionError(f"missing structured batch payload: {text!r}")
        payload, end = json.JSONDecoder().raw_decode(body[start:])
        if body[start + end :]:
            raise AssertionError(f"multiple or trailing batch payloads: {text!r}")
        return payload

    @staticmethod
    def _text(result: object) -> str:
        """Read FastMCP's converted text through sync or async dispatch wrapping."""

        if isinstance(result, tuple):
            result = result[0]
        while isinstance(result, list):
            if len(result) != 1:
                raise AssertionError(f"expected exactly one converted result, got {result!r}")
            result = result[0]
        return result.text  # type: ignore[union-attr]

    async def test_cl001_all_registered_root_schemas_are_strict_and_valid_calls_keep_defaults(self) -> None:
        mcp, _, _ = self._mcp()
        calls: list[object] = []

        def zero() -> str:
            calls.append("zero")
            return "zero"

        def typed(value: int = 7) -> str:
            calls.append(value)
            return str(value)

        mcp.add_tool(zero)
        mcp.add_tool(typed)
        for name, arguments in (("zero", {"unexpected": True}), ("typed", {"unexpected": True})):
            with self.subTest(tool=name):
                tool = mcp._tool_manager.get_tool(name)  # type: ignore[reportPrivateUsage]
                self.assertIsNotNone(tool)
                self.assertIs(tool.parameters["additionalProperties"], False)  # type: ignore[union-attr]
                with self.assertRaisesRegex(ToolError, "unexpected"):
                    await tool.run(arguments, convert_result=True)  # type: ignore[union-attr]
        self.assertEqual(calls, [])

        result = await mcp.call_tool("typed", {})
        self.assertEqual(self._text(result), "7")
        self.assertEqual(calls, [7])

    async def test_cl002_lock_precedes_schema_then_validation_precedes_guard_and_handler_once(self) -> None:
        mcp, registry, _ = self._mcp()
        guard_calls: list[dict[str, object]] = []
        handler_calls: list[int] = []

        def guarded(board_id: str, payload: _CountingPayload) -> str:
            del board_id
            handler_calls.append(payload.value)
            return "ok"

        mcp.add_tool(guarded)
        registry.configure("guarded", hidden=True, locked=True, prerequisite="guarded-plan")
        mcp.configure_guarded_dispatch(
            "guarded",
            guard=lambda _name, _board, fields: guard_calls.append(dict(fields)),
            lock_for_board=lambda _board: _NullLock(),
        )

        _CountingPayload.validations = 0
        with self.assertRaisesRegex(ToolError, "guarded-plan"):
            await mcp.call_tool(
                "guarded", {"board_id": "board", "payload": {"value": 1}, "unexpected": True}
            )
        self.assertEqual((_CountingPayload.validations, guard_calls, handler_calls), (0, [], []))

        registry.unlock("guarded", "board")
        with self.assertRaisesRegex(ToolError, "unexpected"):
            await mcp.call_tool(
                "guarded", {"board_id": "board", "payload": {"value": 2}, "unexpected": True}
            )
        self.assertEqual(guard_calls, [])
        self.assertEqual(handler_calls, [])
        self.assertEqual(_CountingPayload.validations, 1)

        await mcp.call_tool("guarded", {"board_id": "board", "payload": {"value": 3}})
        self.assertEqual(_CountingPayload.validations, 2)
        self.assertEqual(guard_calls, [{"board_id": "board", "payload": {"value": 3}}])
        self.assertEqual(handler_calls, [3])

    async def test_cl001_cl002_generated_plan_metadata_preserves_text_permission_and_once_only_validation(self) -> None:
        """Exercise actual generated-plan registration, not an ordinary-tool validator surrogate."""

        mcp, _, _ = self._mcp()
        engine = _CapturingPlanEngine()
        non_permission = PLAN_TOOL_DEFINITIONS["connect_override-plan"]
        permission_required = PLAN_TOOL_DEFINITIONS["board_setup-plan"]
        register_plan_tools(
            mcp,
            engine,  # type: ignore[arg-type]
            (non_permission, permission_required),
            lambda board_id: f"session:{board_id}",
        )

        counters: dict[str, type[_CountingPlanMetadata]] = {}
        for definition in (non_permission, permission_required):
            tool = mcp._tool_manager.get_tool(definition.plan_tool_name)  # type: ignore[reportPrivateUsage]
            self.assertIsNotNone(tool)
            metadata = tool.fn_metadata  # type: ignore[union-attr]
            counter = type(
                f"Counting{definition.action_name.title().replace('_', '')}Metadata",
                (_CountingPlanMetadata,),
                {"pre_parse_calls": 0},
            )
            tool.fn_metadata = counter(
                arg_model=metadata.arg_model,
                output_schema=metadata.output_schema,
                output_model=metadata.output_model,
                wrap_output=metadata.wrap_output,
                literal_string_fields=metadata.literal_string_fields,
            )
            counters[definition.plan_tool_name] = counter

        envelope = {
            "board_id": "board",
            "hypothesis": "null",
            "strategy": "{}",
            "hypothesis_made": True,
            "strategy_evaluated": True,
            "expected_fail_return": "[]",
            "expected_success_return": "true",
            "max_calls": 1,
            "max_calls_buffer": 0,
            "action_parameters": '{"probe_uid":"null","target_override":null,"external_board_config":null}',
        }
        with self.assertRaisesRegex(ToolError, "user_permission must be omitted"):
            await mcp.call_tool(
                non_permission.plan_tool_name,
                {**envelope, "user_permission": "one-time"},
            )
        self.assertEqual(counters[non_permission.plan_tool_name].pre_parse_calls, 1)
        self.assertEqual(engine.calls, [])

        await mcp.call_tool(non_permission.plan_tool_name, envelope)
        self.assertEqual(counters[non_permission.plan_tool_name].pre_parse_calls, 2)
        self.assertEqual(len(engine.calls), 1)
        captured = engine.calls[-1]
        self.assertEqual(captured["hypothesis"], "null")
        self.assertEqual(captured["strategy"], "{}")
        self.assertEqual(captured["expected_fail_return"], "[]")
        self.assertEqual(captured["expected_success_return"], "true")
        self.assertNotIn("user_permission", captured)
        self.assertEqual(
            captured["action_parameters"],
            {"probe_uid": "null", "target_override": None, "external_board_config": None},
        )

        required_fields = {
            **envelope,
            "action_parameters": '{"mode":"setup"}',
            "user_permission": "one-time",
        }
        await mcp.call_tool(permission_required.plan_tool_name, required_fields)
        self.assertEqual(counters[permission_required.plan_tool_name].pre_parse_calls, 1)
        self.assertEqual(len(engine.calls), 2)
        self.assertEqual(engine.calls[-1]["user_permission"], "one-time")

    async def test_cl001_cl003_batch_schema_and_child_refusal_are_strict_structured_errors(self) -> None:
        mcp, _, _ = self._mcp()
        child_calls: list[str] = []

        async def child(board_id: str, payload: _CountingPayload) -> str:
            child_calls.append(f"{board_id}:{payload.value}")
            return "ok"

        mcp.add_tool(child)
        batch = build_batch_handlers(mcp.call_tool, tool_exists=mcp.registry.is_registered)["action_batch"]
        mcp.add_tool(batch, name="action_batch")
        batch_tool = mcp._tool_manager.get_tool("action_batch")  # type: ignore[reportPrivateUsage]
        self.assertIsNotNone(batch_tool)
        self.assertIs(batch_tool.parameters["additionalProperties"], False)  # type: ignore[union-attr]
        self.assertIs(batch_tool.parameters["$defs"]["BatchChild"]["additionalProperties"], False)  # type: ignore[index,union-attr]

        for arguments in (
            {"board_id": "board", "actions": [], "outer_extra": True},
            {
                "board_id": "board",
                "actions": [{"tool_name": "child", "arguments": {"board_id": "board"}, "envelope_extra": True}],
            },
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ToolError, "extra"):
                    await mcp.call_tool("action_batch", arguments)
        self.assertEqual(child_calls, [])

        _CountingPayload.validations = 0
        with self.assertRaises(ToolError) as raised:
            await mcp.call_tool(
                "action_batch",
                {
                    "board_id": "board",
                    "actions": [
                        {
                            "tool_name": "child",
                            "arguments": {
                                "board_id": "board",
                                "payload": {"value": 9},
                                "child_extra": True,
                            },
                        }
                    ],
                },
            )
        payload = self._body(raised.exception)
        self.assertEqual(payload["status"], "batch_failed")
        self.assertEqual(payload["completed"], [])
        failure = payload["failure"]
        self.assertIsInstance(failure, dict)
        self.assertEqual(failure["index"], 0)
        self.assertEqual(failure["tool_name"], "child")
        self.assertIn("child_extra", failure["message"])
        self.assertEqual(child_calls, [])
        self.assertEqual(_CountingPayload.validations, 1)

    async def test_cl002_cl003_valid_batch_validates_each_child_once_and_stops_on_first_failure(self) -> None:
        mcp, _, _ = self._mcp()
        seen: list[str] = []

        async def child(board_id: str, payload: _CountingPayload) -> str:
            seen.append(f"{board_id}:{payload.value}")
            if payload.value == 2:
                raise ToolError("child refusal")
            return str(payload.value)

        mcp.add_tool(child)
        mcp.add_tool(
            build_batch_handlers(mcp.call_tool, tool_exists=mcp.registry.is_registered)["action_batch"],
            name="action_batch",
        )
        _CountingPayload.validations = 0
        result = await mcp.call_tool(
            "action_batch",
            {"board_id": "board", "actions": [{"tool_name": "child", "arguments": {"board_id": "board", "payload": {"value": 1}}}]},
        )
        self.assertIn('"status":"batch_completed"', self._text(result))
        self.assertEqual((_CountingPayload.validations, seen), (1, ["board:1"]))

        with self.assertRaises(ToolError) as raised:
            await mcp.call_tool(
                "action_batch",
                {
                    "board_id": "board",
                    "actions": [
                        {"tool_name": "child", "arguments": {"board_id": "board", "payload": {"value": 1}}},
                        {"tool_name": "child", "arguments": {"board_id": "board", "payload": {"value": 2}}},
                        {"tool_name": "child", "arguments": {"board_id": "board", "payload": {"value": 3}}},
                    ],
                },
            )
        payload = self._body(raised.exception)
        self.assertEqual(payload["status"], "batch_failed")
        self.assertEqual([item["index"] for item in payload["completed"]], [0])
        self.assertEqual(payload["failure"]["index"], 1)  # type: ignore[index]
        self.assertEqual(seen, ["board:1", "board:1", "board:2"])

    async def test_cl004_nested_revision_notification_is_coalesced_and_state_is_restored(self) -> None:
        mcp, registry, session = self._mcp()

        async def child(board_id: str) -> str:
            registry.relock("child", board_id)
            return "done"

        async def failing(board_id: str) -> str:
            del board_id
            raise ToolError("inner refusal")

        mcp.add_tool(child)
        mcp.add_tool(failing)
        registry.configure("child", hidden=True, locked=True, prerequisite="child-plan")
        mcp.add_tool(
            build_batch_handlers(mcp.call_tool, tool_exists=registry.is_registered)["action_batch"],
            name="action_batch",
        )

        registry.unlock("child", "board")
        await mcp.call_tool("child", {"board_id": "board"})
        self.assertEqual(session.calls, 1)

        registry.unlock("child", "board")
        await mcp.call_tool(
            "action_batch",
            {"board_id": "board", "actions": [{"tool_name": "child", "arguments": {"board_id": "board"}}]},
        )
        self.assertEqual(session.calls, 2, "one outer batch request must yield one notification")

        with self.assertRaises(ToolError):
            await mcp.call_tool(
                "action_batch",
                {"board_id": "board", "actions": [{"tool_name": "failing", "arguments": {"board_id": "board"}}]},
            )
        registry.unlock("child", "board")
        await mcp.call_tool("child", {"board_id": "board"})
        self.assertEqual(session.calls, 3, "an inner failure must not leak nesting state")

    async def test_cl004_cancellation_restores_dispatch_ownership_for_the_continuing_task(self) -> None:
        """Cancellation must reset the context-local outer-dispatch marker without a sleep race."""

        mcp, registry, session = self._mcp()
        started = anyio.Event()

        async def blocked(board_id: str) -> str:
            del board_id
            started.set()
            await anyio.sleep_forever()

        async def visibility_change(board_id: str) -> str:
            registry.relock("visibility_change", board_id)
            return "done"

        mcp.add_tool(blocked)
        mcp.add_tool(visibility_change)
        registry.configure("visibility_change", hidden=True, locked=True)
        registry.unlock("visibility_change", "board")

        async def cancel_after_entry(scope: anyio.CancelScope) -> None:
            await started.wait()
            scope.cancel()

        with anyio.CancelScope() as scope:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(cancel_after_entry, scope)
                with self.assertRaises(asyncio.CancelledError):
                    await mcp.call_tool("blocked", {"board_id": "board"})

        await mcp.call_tool("visibility_change", {"board_id": "board"})
        self.assertEqual(session.calls, 1)

    async def test_cl003_wire_results_mark_refusals_as_errors_without_losing_batch_payload(self) -> None:
        """Use MCP's request handler, rather than treating a ToolError as a wire result."""

        mcp = RegistryFastMCP("h01-wire", timeout_resolver=lambda *_: 1)
        child_calls: list[str] = []

        async def child(board_id: str, value: int) -> str:
            child_calls.append(f"{board_id}:{value}")
            if value == 2:
                raise ToolError("child refusal")
            return str(value)

        mcp.add_tool(child)
        mcp.add_tool(
            build_batch_handlers(mcp.call_tool, tool_exists=mcp.registry.is_registered)["action_batch"],
            name="action_batch",
        )
        client_send, server_receive = anyio.create_memory_object_stream()
        server_send, client_receive = anyio.create_memory_object_stream()
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                mcp._mcp_server.run,  # type: ignore[reportPrivateUsage]
                server_receive,
                server_send,
                mcp.create_initialization_options(),
            )
            async with ClientSession(client_receive, client_send) as client:
                await client.initialize()
                for arguments in (
                    {"board_id": "board", "actions": [], "outer_extra": True},
                    {
                        "board_id": "board",
                        "actions": [
                            {
                                "tool_name": "child",
                                "arguments": {"board_id": "board", "value": 1},
                                "envelope_extra": True,
                            }
                        ],
                    },
                    {
                        "board_id": "board",
                        "actions": [
                            {
                                "tool_name": "child",
                                "arguments": {
                                    "board_id": "board",
                                    "value": 1,
                                    "child_extra": True,
                                },
                            }
                        ],
                    },
                ):
                    with self.subTest(arguments=arguments):
                        refusal = await client.call_tool("action_batch", arguments)
                        self.assertTrue(refusal.isError)
                failed = await client.call_tool(
                    "action_batch",
                    {
                        "board_id": "board",
                        "actions": [
                            {"tool_name": "child", "arguments": {"board_id": "board", "value": 1}},
                            {"tool_name": "child", "arguments": {"board_id": "board", "value": 2}},
                        ],
                    },
                )
                self.assertTrue(failed.isError)
                failed_text = failed.content[0].text
                self.assertIn('"status":"batch_failed"', failed_text)
                self.assertIn('"index":1', failed_text)
                self.assertEqual(failed_text.count(SAFE_EXIT_REMINDER), 1)

                completed = await client.call_tool(
                    "action_batch",
                    {
                        "board_id": "board",
                        "actions": [
                            {"tool_name": "child", "arguments": {"board_id": "board", "value": 3}}
                        ],
                    },
                )
                self.assertFalse(completed.isError)
                self.assertIn('"status":"batch_completed"', completed.content[0].text)
            tasks.cancel_scope.cancel()
        self.assertEqual(child_calls, ["board:1", "board:2", "board:3"])

    async def test_cl004_wire_nested_relock_notifies_once_and_failure_does_not_leak_state(self) -> None:
        """Observe the real transport notification before each corresponding tools/call response."""

        registry = ToolRegistry()
        mcp = RegistryFastMCP("h01-wire-notifications", registry=registry, timeout_resolver=lambda *_: 1)

        async def child(board_id: str) -> str:
            registry.relock("child", board_id)
            return "done"

        async def failing(board_id: str) -> str:
            del board_id
            raise ToolError("nested refusal")

        mcp.add_tool(child)
        mcp.add_tool(failing)
        registry.configure("child", hidden=True, locked=True)
        mcp.add_tool(
            build_batch_handlers(mcp.call_tool, tool_exists=registry.is_registered)["action_batch"],
            name="action_batch",
        )
        client_send, server_receive = anyio.create_memory_object_stream()
        server_send, client_receive = anyio.create_memory_object_stream()
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                mcp._mcp_server.run,  # type: ignore[reportPrivateUsage]
                server_receive,
                server_send,
                mcp.create_initialization_options(),
            )
            async with _ObservingClientSession(client_receive, client_send) as client:
                await client.initialize()
                registry.unlock("child", "board")
                completed = await client.call_tool(
                    "action_batch",
                    {"board_id": "board", "actions": [{"tool_name": "child", "arguments": {"board_id": "board"}}]},
                )
                self.assertFalse(completed.isError)
                self.assertEqual(
                    [item.root.method for item in client.notifications],
                    ["notifications/tools/list_changed"],
                )

                failed = await client.call_tool(
                    "action_batch",
                    {"board_id": "board", "actions": [{"tool_name": "failing", "arguments": {"board_id": "board"}}]},
                )
                self.assertTrue(failed.isError)
                self.assertEqual(len(client.notifications), 1)

                registry.unlock("child", "board")
                direct = await client.call_tool("child", {"board_id": "board"})
                self.assertFalse(direct.isError)
                self.assertEqual(
                    [item.root.method for item in client.notifications],
                    ["notifications/tools/list_changed", "notifications/tools/list_changed"],
                )
            tasks.cancel_scope.cancel()


class _NullLock:
    def __enter__(self) -> _NullLock:
        return self

    def __exit__(self, *_: object) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
