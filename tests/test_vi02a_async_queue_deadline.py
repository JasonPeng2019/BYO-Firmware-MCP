"""VI02A: actual async dispatch and public batches share queue/execution budgets.

Real AnyIO, registry, batch, plan, connection and operation managers are used.
Only physical backend effects are simulated. Events establish ownership and
handler entry; separate managed deadlines witness partial-budget consumption.
"""

from __future__ import annotations

import threading
from functools import partial
from typing import Any
from unittest.mock import patch

import anyio
from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp import server
from pyocd_debug_mcp.kernel.operations import (
    OperationTimeoutError,
    current_operation,
    dispatch,
    operation_manager,
)
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP
from pyocd_debug_mcp.tools.batch import build_batch_handlers
from tests.operation_fixture_support import BOARD, HANG_GUARD_SECONDS, _OwnershipFixture, _until

import pytest

BUDGET = 0.30
PARTIAL_BUDGET = 1.2
OBSERVATIONS: list[dict[str, Any]] = []


class AsyncQueueDeadlineTests(_OwnershipFixture):
    def setUp(self) -> None:
        super().setUp()
        self.events: list[str] = []
        self.facts: dict[str, Any] = {}
        self.managed: list[Any] = []
        real_create = operation_manager.create

        def observe(*args: Any, **kwargs: Any) -> Any:
            managed = real_create(*args, **kwargs)
            self.managed.append(managed)
            return managed

        overlay = patch.object(operation_manager, "create", observe)
        overlay.start()
        self.addCleanup(overlay.stop)

    def tearDown(self) -> None:
        alive = self.join_workers()
        OBSERVATIONS.append(
            {
                "test": self.id(),
                "events": self.events,
                "facts": self.facts,
                "backend": self.backend.snapshot(),
                "gate_clears": self.gate.clears,
                "operations": [
                    {
                        "id": op.operation_id,
                        "tool": op.tool_name,
                        "started": op.started_at,
                        "execution_started": op.execution_started_at,
                        "handler_started": op.handler_started_at,
                        "finished": op.finished_at,
                        "done": op.done.is_set(),
                        "owned_resources": op.resources.owned_count,
                        "cancel_requested": op.cancellation_requested.is_set(),
                        "non_interruptible": op.non_interruptible,
                        "completion_committed": op.completion_committed,
                        "cleanup_errors": op.resources.cleanup_errors,
                    }
                    for op in self.managed
                ],
                "cleanup": {"alive_workers": alive, "manager": list(operation_manager.snapshots())},
            }
        )
        self.assertEqual(alive, [])
        self.assertEqual(operation_manager.snapshots(), ())
        self.assertTrue(all(op.done.is_set() for op in self.managed))
        self.assertIsNone(current_operation())

    def launch(self, group: Any, factory: Any) -> dict[str, Any]:
        outcome: dict[str, Any] = {"done": anyio.Event(), "result": None, "error": None}

        async def run() -> None:
            with anyio.CancelScope() as scope:
                outcome["scope"] = scope
                try:
                    outcome["result"] = await factory()
                except BaseException as exc:
                    outcome["error"] = exc
                finally:
                    outcome["done"].set()

        group.start_soon(run)
        return outcome

    async def done(self, outcome: dict[str, Any]) -> None:
        with anyio.fail_after(HANG_GUARD_SECONDS):
            await outcome["done"].wait()

    async def witness(self, seconds: float) -> None:
        with self.assertRaises(OperationTimeoutError):
            await dispatch("wait", None, anyio.sleep_forever, seconds)

    def assert_healthy_owner(self, owner: Any, board: str = BOARD) -> None:
        self.assertIs(self.connections.connection_for(board), owner)
        self.assertIsNotNone(self.gate.snapshot(board))
        self.assertIsNone(owner.runtime_session.closed_at)
        self.assertFalse(any(item[0] == board for item in self.gate.clears))
        self.assertFalse(
            any(
                method in {"close", "release_reset"} and label == owner.handle.probe_uid
                for method, label in self.backend.snapshot()
            )
        )

    def async_call(self, handler: Any, budget: float, *, name: str = "read_memory") -> Any:
        def binder(managed: Any) -> None:
            self.events.append("bind")
            server._bind_managed_board_resources(managed)

        return dispatch(
            name,
            BOARD,
            handler,
            budget,
            resource_binder=binder,
            before_execution=lambda: self.events.append("preflight"),
            finalizer=lambda: self.events.append("finalizer"),
        )

    def make_mcp(self, handlers: dict[str, Any], budget: Any = BUDGET) -> RegistryFastMCP:
        mcp = RegistryFastMCP(
            "vi02a-deadlines",
            timeout_resolver=lambda name, args: (
                (budget(args) if callable(budget) else budget)
                if name == "action_batch"
                else HANG_GUARD_SECONDS
            ),
        )
        for name, handler in handlers.items():
            mcp.add_tool(handler, name=name, structured_output=False)
            mcp.configure_layer2(name)
            mcp.configure_guarded_dispatch(
                name,
                guard=lambda tool, board, args: self.events.append("guard:" + tool),
                lock_for_board=self.connections.lock_for,
            )
        for name, handler in build_batch_handlers(
            mcp.call_tool, tool_exists=mcp.registry.is_registered
        ).items():
            mcp.add_tool(handler, name=name, structured_output=False)
            mcp.configure_layer2(name)
        mcp.configure_operation_resources(server._bind_managed_board_resources)
        return mcp

    @staticmethod
    def batch(mcp: RegistryFastMCP, names: list[str]) -> Any:
        return mcp.call_tool(
            "action_batch",
            {
                "board_id": BOARD,
                "actions": [
                    {"tool_name": name, "arguments": {"board_id": BOARD}} for name in names
                ],
            },
        )

    def partial_budget(self, public: bool) -> None:
        self.connect("owner")
        held, release, running = threading.Event(), threading.Event(), threading.Event()

        async def handler(board_id: str = BOARD) -> str:
            self.events.append("handler")
            self.read_current(board_id)
            running.set()
            await anyio.sleep_forever()
            return "unreachable"

        mcp = self.make_mcp({"read_memory": handler}, PARTIAL_BUDGET)
        factory = (
            partial(self.batch, mcp, ["read_memory"])
            if public
            else partial(self.async_call, handler, PARTIAL_BUDGET)
        )

        async def scenario() -> None:
            async with anyio.create_task_group() as group:
                a = self.launch(
                    group,
                    partial(self.call, "halt", self.holder(held, release), HANG_GUARD_SECONDS),
                )
                try:
                    await _until(held.is_set)
                    b = self.launch(group, factory)
                    await _until(lambda: self.queued("action_batch" if public else "read_memory"))
                    # Spend most of B's budget under event-proven board ownership.
                    await self.witness(PARTIAL_BUDGET * 0.65)
                    self.assertFalse(b["done"].is_set())
                finally:
                    release.set()
                await self.done(a)
                await _until(running.is_set)
                # If execution restarted a full budget, this witness would finish first.
                await self.witness(PARTIAL_BUDGET * 0.65)
                self.facts["finished_before_second_witness"] = b["done"].is_set()
                if not b["done"].is_set():
                    b["scope"].cancel()
                await self.done(b)
                error = b["error"]
                if public:
                    self.assertIs(type(error), ToolError)
                    error = error.__cause__
                self.assertIs(type(error), OperationTimeoutError)
                self.assertEqual(error.timeout_seconds, PARTIAL_BUDGET)

        anyio.run(scenario)
        self.assertTrue(self.facts["finished_before_second_witness"])
        self.assertEqual(self.events.count("handler"), 1)
        self.assertEqual(self.events.count("finalizer"), 0 if public else 1)

    @pytest.mark.mvp
    def test_public_batch_queue_and_execution_use_one_budget(self) -> None:
        self.partial_budget(public=True)

    @pytest.mark.mvp
    def test_public_queued_cancellation_leaves_owner_untouched(self) -> None:
        owner = self.connect("owner")
        held, release = threading.Event(), threading.Event()

        async def read_memory(board_id: str) -> str:
            self.events.append("handler")
            return str(self.read_current(board_id))

        mcp = self.make_mcp({"read_memory": read_memory}, HANG_GUARD_SECONDS)

        async def scenario() -> None:
            async with anyio.create_task_group() as group:
                a = self.launch(
                    group,
                    partial(self.call, "halt", self.holder(held, release), HANG_GUARD_SECONDS),
                )
                try:
                    await _until(held.is_set)
                    b = self.launch(group, partial(self.batch, mcp, ["read_memory"]))
                    await _until(lambda: self.queued("action_batch"))
                    b["scope"].cancel()
                    await self.done(b)
                    self.assertIsInstance(b["error"], anyio.get_cancelled_exc_class())
                    self.assertEqual(self.events, [])
                    self.assert_healthy_owner(owner)
                finally:
                    release.set()
                await self.done(a)

        anyio.run(scenario)
        self.assertEqual(self.events, [])
