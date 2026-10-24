"""VI02A: actual async dispatch and public batches share queue/execution budgets.

Real AnyIO, registry, batch, plan, connection and operation managers are used.
Only physical backend effects are simulated. Events establish ownership and
handler entry; separate managed deadlines witness partial-budget consumption.
"""

from __future__ import annotations

import json
import threading
import time
from functools import partial
from typing import Any
from unittest.mock import patch

import anyio
from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp import server
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine
from pyocd_debug_mcp.kernel.finalizers import build_finalizer
from pyocd_debug_mcp.kernel.operations import (
    BoardBusyError,
    OperationCancelledError,
    OperationTimeoutError,
    current_operation,
    dispatch,
    operation_manager,
)
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP, ToolRegistry
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.tools.batch import build_batch_handlers
from test_vi02_operation_resource_ownership import (
    BOARD,
    HANG_GUARD_SECONDS,
    OTHER_BOARD,
    _OwnershipFixture,
    _until,
)

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

    def assert_busy(self, error: Any, tool: str, budget: float = BUDGET) -> None:
        self.assertIs(type(error), BoardBusyError)
        self.assertEqual(
            (error.tool_name, error.board_id, error.timeout_seconds), (tool, BOARD, budget)
        )
        self.assertIn("without starting", str(error))

    def test_direct_queue_expiry_has_no_late_entry_or_owner_effects(self) -> None:
        owner = self.connect("owner")
        other = self.connect("other", OTHER_BOARD)
        held, release = threading.Event(), threading.Event()

        async def handler() -> str:
            self.events.append("handler")
            return "late"

        async def scenario() -> None:
            async with anyio.create_task_group() as group:
                a = self.launch(
                    group,
                    partial(self.call, "halt", self.holder(held, release), HANG_GUARD_SECONDS),
                )
                try:
                    await _until(held.is_set)
                    b = self.launch(group, partial(self.async_call, handler, BUDGET))
                    await _until(lambda: self.queued("read_memory"))
                    await self.done(b)
                    self.assert_busy(b["error"], "read_memory")
                    self.assertEqual(self.events, [])
                    self.assert_healthy_owner(owner)
                    self.assertFalse(a["done"].is_set())
                    self.facts["other_board_result"] = await dispatch(
                        "read_memory",
                        OTHER_BOARD,
                        partial(self.read_current, OTHER_BOARD),
                        HANG_GUARD_SECONDS,
                        resource_binder=server._bind_managed_board_resources,
                    )
                    self.assertIs(self.connections.connection_for(OTHER_BOARD), other)
                    self.facts["before_release"] = self.board_state()
                finally:
                    release.set()
                await self.done(a)
            self.assertEqual(self.events, [])
            # The same board is usable after the expired waiter is gone.
            self.assertEqual(await self.async_call(handler, HANG_GUARD_SECONDS), "late")

        anyio.run(scenario)
        expired = next(op for op in self.managed if op.tool_name == "read_memory")
        self.assertIsNone(expired.execution_started_at)
        self.assertIsNone(expired.handler_started_at)
        self.assertEqual(expired.resources.owned_count, 0)
        self.assertEqual(self.events, ["bind", "preflight", "handler", "finalizer"])

    def successful_call(self, queued: bool, public: bool) -> None:
        owner = self.connect("owner")
        held, release = threading.Event(), threading.Event()

        async def handler(board_id: str = BOARD) -> str:
            self.events.append("handler")
            self.read_current(board_id)
            return "success"

        mcp = self.make_mcp({"read_memory": handler}, HANG_GUARD_SECONDS)
        factory = (
            partial(self.batch, mcp, ["read_memory"])
            if public
            else partial(self.async_call, handler, HANG_GUARD_SECONDS)
        )

        async def scenario() -> None:
            async with anyio.create_task_group() as group:
                if queued:
                    a = self.launch(
                        group,
                        partial(self.call, "halt", self.holder(held, release), HANG_GUARD_SECONDS),
                    )
                    await _until(held.is_set)
                try:
                    b = self.launch(group, factory)
                    if queued:
                        await _until(
                            lambda: self.queued("action_batch" if public else "read_memory")
                        )
                        self.assertEqual(self.events, [])
                        self.assert_healthy_owner(owner)
                finally:
                    release.set()
                await self.done(b)
                if queued:
                    await self.done(a)
                self.assertIsNone(b["error"])
                result = b["result"]
                if public:
                    result = json.loads(result[0].text.splitlines()[0])["status"]
                self.assertEqual(result, "batch_completed" if public else "success")

        anyio.run(scenario)
        self.assertIs(self.connections.connection_for(BOARD), owner)
        self.assertIsNone(owner.runtime_session.closed_at)
        self.assertEqual(
            self.events,
            ["guard:read_memory", "handler"]
            if public
            else ["bind", "preflight", "handler", "finalizer"],
        )

    def test_direct_immediate_success(self) -> None:
        self.successful_call(queued=False, public=False)

    def test_direct_queued_success(self) -> None:
        self.successful_call(queued=True, public=False)

    def test_public_batch_immediate_async_child_success(self) -> None:
        self.successful_call(queued=False, public=True)

    def test_public_batch_queued_async_child_success(self) -> None:
        self.successful_call(queued=True, public=True)

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

    def test_direct_queue_and_execution_use_one_budget(self) -> None:
        self.partial_budget(public=False)

    def test_public_batch_queue_and_execution_use_one_budget(self) -> None:
        self.partial_budget(public=True)

    def test_public_batch_expiry_keeps_real_plan_and_eligible_finalizer_pending(self) -> None:
        owner = self.connect("owner")
        registry = ToolRegistry()
        registry.register("read_serial", locked=True)
        plans = PlanEngine(ServerRun(), registry)
        self.addCleanup(plans.close_run)
        parameters = {
            "expected_text": None,
            "read_seconds": 0.1,
            "baudrate": None,
            "port": None,
            "reset_on_open": False,
            "on_exit": {"action": "reset_and_run"},
        }
        plans.null_response("read_serial-plan")
        plans.submit(
            "read_serial-plan",
            {
                "board_id": BOARD,
                "hypothesis": "Queued batch expiry preserves serial authority.",
                "strategy": "Expire the batch, then execute the still-pending serial plan.",
                "hypothesis_made": True,
                "strategy_evaluated": True,
                "expected_fail_return": "No UART or finalizer entry while queued.",
                "expected_success_return": "One later serial action and finalizer.",
                "max_calls": 1,
                "max_calls_buffer": 0,
                "action_parameters": parameters,
            },
        )

        async def read_serial(
            board_id: str,
            expected_text: str | None = None,
            read_seconds: float = 0.1,
            baudrate: int | None = None,
            port: str | None = None,
            reset_on_open: bool = False,
            on_exit: dict[str, str] | None = None,
        ) -> str:
            self.events.append("serial-handler")
            return "serial-data"

        mcp = self.make_mcp({"read_serial": read_serial})
        mcp.configure_guarded_dispatch(
            "read_serial",
            guard=lambda name, board, args: plans.enforce(name, board, parameters),
            lock_for_board=self.connections.lock_for,
        )
        mcp.configure_finalizers(
            lambda name, board, args: build_finalizer(
                name,
                board,
                args["on_exit"],
                uart_write=lambda *_: self.fail("unexpected UART finalizer"),
                reset_and_run=lambda board: self.backend.note("reset_and_run", board),
            )
        )
        arguments = {"board_id": BOARD, **parameters}
        held, release = threading.Event(), threading.Event()

        async def scenario() -> None:
            async with anyio.create_task_group() as group:
                a = self.launch(
                    group,
                    partial(self.call, "halt", self.holder(held, release), HANG_GUARD_SECONDS),
                )
                try:
                    await _until(held.is_set)
                    with self.assertRaises(ToolError) as caught:
                        await mcp.call_tool(
                            "action_batch",
                            {
                                "board_id": BOARD,
                                "actions": [{"tool_name": "read_serial", "arguments": arguments}],
                            },
                        )
                    self.assert_busy(caught.exception.__cause__, "action_batch")
                    plan = plans.active_plan("read_serial", BOARD)
                    self.assertIsNotNone(plan)
                    self.assertEqual(plan.remaining_calls, 1)
                    self.assertTrue(registry.is_unlocked("read_serial", BOARD))
                    self.assertEqual(self.events, [])
                    self.assert_healthy_owner(owner)
                    self.facts["before_release"] = self.board_state()
                finally:
                    release.set()
                await self.done(a)
            await mcp.call_tool("read_serial", arguments)

        anyio.run(scenario)
        self.assertIsNone(plans.active_plan("read_serial", BOARD))
        self.assertFalse(registry.is_unlocked("read_serial", BOARD))
        self.assertEqual(self.events, ["serial-handler"])
        self.assertEqual(self.backend.snapshot().count(("reset_and_run", BOARD)), 1)
        self.assertIs(self.connections.connection_for(BOARD), owner)

    def test_second_public_batch_expires_behind_active_batch(self) -> None:
        owner = self.connect("owner")
        held, release = threading.Event(), threading.Event()

        def hold_board(board_id: str) -> str:
            return self.holder(held, release)()

        async def read_memory(board_id: str) -> str:
            self.events.append("second-handler")
            return str(self.read_current(board_id))

        mcp = self.make_mcp(
            {"hold_board": hold_board, "read_memory": read_memory},
            lambda args: (
                HANG_GUARD_SECONDS if args["actions"][0]["tool_name"] == "hold_board" else BUDGET
            ),
        )

        async def scenario() -> None:
            async with anyio.create_task_group() as group:
                a = self.launch(group, partial(self.batch, mcp, ["hold_board"]))
                try:
                    await _until(held.is_set)
                    with self.assertRaises(ToolError) as caught:
                        await self.batch(mcp, ["read_memory"])
                    self.assert_busy(caught.exception.__cause__, "action_batch")
                    self.assertFalse(a["done"].is_set())
                    self.assert_healthy_owner(owner)
                    self.assertEqual(self.events, ["guard:hold_board"])
                finally:
                    release.set()
                await self.done(a)
                self.assertIsNone(a["error"])

        anyio.run(scenario)
        self.assertEqual(self.events, ["guard:hold_board"])

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

    def test_async_request_cancel_checkpoint_prevents_entry(self) -> None:
        self.connect("owner")
        held, release = threading.Event(), threading.Event()

        async def handler() -> str:
            self.events.append("handler")
            return "late"

        async def scenario() -> None:
            async with anyio.create_task_group() as group:
                a = self.launch(
                    group,
                    partial(self.call, "halt", self.holder(held, release), HANG_GUARD_SECONDS),
                )
                try:
                    await _until(held.is_set)
                    b = self.launch(
                        group,
                        partial(
                            dispatch,
                            "read_memory",
                            BOARD,
                            handler,
                            HANG_GUARD_SECONDS,
                            request_id="queued-async",
                            resource_binder=server._bind_managed_board_resources,
                        ),
                    )
                    await _until(lambda: self.queued("read_memory"))
                    self.assertEqual(operation_manager.cancel_request("queued-async"), 1)
                    await self.done(b)
                    self.assertIsInstance(b["error"], OperationCancelledError)
                finally:
                    release.set()
                await self.done(a)

        anyio.run(scenario)
        self.assertEqual(self.events, [])

    def test_async_preflight_spending_remaining_budget_never_enters_handler(self) -> None:
        owner = self.connect("owner")
        # Simulate a slow physical supported-register read in the real preflight path.
        self.backend.during[("supported_core_registers", "owner")] = lambda: time.sleep(BUDGET * 2)

        async def handler() -> str:
            self.events.append("handler")
            return "late"

        async def scenario() -> None:
            with self.assertRaises(OperationTimeoutError):
                await dispatch(
                    "write_cpu_register",
                    BOARD,
                    handler,
                    BUDGET,
                    before_execution=self.register_guard(),
                    resource_binder=server._bind_managed_board_resources,
                    finalizer=lambda: self.events.append("finalizer"),
                )

        anyio.run(scenario)
        self.assertEqual(self.events, [])
        self.assertIsNone(self.managed[0].handler_started_at)
        self.assertIsNotNone(self.managed[0].execution_started_at)
        self.assertIsNotNone(owner.runtime_session.closed_at)
        self.assertEqual(
            self.backend.snapshot(), [("supported_core_registers", "owner"), ("close", "owner")]
        )

    def transaction_boundary(self, *, committed: bool, cancel: bool = False) -> None:
        """Actual managed transaction/commit flags, with simulated physical mutation."""
        self.connect("owner")
        held, release, entered = threading.Event(), threading.Event(), threading.Event()

        def transaction(board_id: str) -> str:
            managed = current_operation()
            assert managed is not None
            managed.begin_non_interruptible()
            self.read_current(board_id)
            entered.set()
            if not managed.cancellation_requested.wait(HANG_GUARD_SECONDS):
                raise AssertionError("parent interruption never reached transaction child")
            # Completion is observed after the parent's deadline/cancellation arrives.
            if committed:
                managed.run_if_not_cancelled(lambda: self.events.append("transaction-committed"))
            else:
                self.events.append("transaction-finished")
            return "transaction-result"

        mcp = self.make_mcp({"transaction": transaction}, PARTIAL_BUDGET)

        async def scenario() -> None:
            async with anyio.create_task_group() as group:
                a = self.launch(
                    group,
                    partial(self.call, "halt", self.holder(held, release), HANG_GUARD_SECONDS),
                )
                try:
                    await _until(held.is_set)
                    b = self.launch(group, partial(self.batch, mcp, ["transaction"]))
                    await _until(lambda: self.queued("action_batch"))
                    await self.witness(PARTIAL_BUDGET * 0.65)
                finally:
                    release.set()
                await self.done(a)
                await _until(entered.is_set)
                if cancel:
                    b["scope"].cancel()
                await self.done(b)
                if committed:
                    self.assertIsNone(b["error"])
                    payload = json.loads(b["result"][0].text.splitlines()[0])
                    self.assertEqual(payload["status"], "batch_completed")
                    self.assertIn("transaction-result", payload["completed"][0]["result"])
                else:
                    self.assertIs(type(b["error"]), ToolError)
                    self.assertIs(type(b["error"].__cause__), OperationTimeoutError)

        anyio.run(scenario)
        child = next(op for op in self.managed if op.tool_name == "transaction")
        self.assertTrue(child.non_interruptible)
        self.assertEqual(child.completion_committed, committed)
        self.assertTrue(child.cancellation_requested.is_set())
        self.assertTrue(child.done.is_set())
        self.assertEqual(child.resources.cleanup_errors, [])
        self.assertEqual(self.backend.snapshot().count(("close", "owner")), 1)
        self.assertEqual(self.backend.snapshot().count(("release_reset", "owner")), 2)

    def test_parent_deadline_waits_for_non_interruptible_child_cleanup(self) -> None:
        self.transaction_boundary(committed=False)

    def test_parent_deadline_preserves_committed_child_completion(self) -> None:
        self.transaction_boundary(committed=True)

    def test_parent_cancellation_preserves_committed_child_completion(self) -> None:
        self.transaction_boundary(committed=True, cancel=True)
