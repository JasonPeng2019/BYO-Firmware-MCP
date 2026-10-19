"""Independent M10 public-dispatch oracles; no physical probe is opened.

Only elapsed-delay controls and the target backend are substituted. Observers
delegate to the real handler, binder, operation creation and ownership code.
"""

from __future__ import annotations

import asyncio
import threading
import unittest
from contextlib import ExitStack
from functools import wraps
from types import SimpleNamespace
from unittest.mock import patch

import anyio
from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.kernel import operations
from pyocd_debug_mcp.tools import misc
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    tiered_test_server,
)


# Liveness/cleanup bounds, not performance assertions. Delay completion itself
# is controlled by events; no test sleeps for the 30-second public maximum.
BOUND = 15
MAXIMUM_MS = 30_000


async def signalled(event: threading.Event) -> None:
    with anyio.fail_after(BOUND):
        while not event.is_set():
            await anyio.sleep(0.005)


def text(result) -> str:
    return "\n".join(block.text for block in result)


class IndependentWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        stack = self.stack = ExitStack()
        self.addCleanup(stack.close)
        root = stack.enter_context(isolated_project())
        self.server = stack.enter_context(tiered_test_server(root))
        self.backend = DeterministicTargetBackend()
        self.server.configure_tiered_test_seams(backend=self.backend)
        self.manager = operations.operation_manager
        self.assertEqual(self.manager.snapshots(), ())
        self.created = []
        self.journal = []
        self.handler_exits = {}
        self.handler_threads = {}
        create = self.manager.create

        def observe_create(*args, **kwargs):
            op = create(*args, **kwargs)
            self.created.append(op)
            return op

        stack.enter_context(patch.object(self.manager, "create", side_effect=observe_create))
        tool = self.server.mcp._tool_manager.get_tool("wait")
        original = tool.fn

        @wraps(original)
        def observe_handler(*args, **kwargs):
            op = operations.current_operation()
            self.handler_threads[op.operation_id] = threading.current_thread()
            self.journal.append(("handler-enter", op.operation_id, op.board_id))
            try:
                return original(*args, **kwargs)
            finally:
                self.journal.append(("handler-exit", op.operation_id, op.board_id))
                self.handler_exits.setdefault(op.operation_id, threading.Event()).set()

        stack.enter_context(patch.object(tool, "fn", observe_handler))

    def call(self, board="a", ms=100, **extra):
        return self.server.mcp.call_tool("wait", {"board_id": board, "ms": ms, **extra})

    def assert_idle(self, *boards) -> None:
        self.assertEqual(self.manager.snapshots(), ())
        for board in boards:
            self.assertFalse(self.manager.worker_lock(board).locked())
            self.assertFalse(self.manager.batch_lock(board).locked())
        for op in self.created:
            self.assertTrue(op.done.is_set())
            self.assertTrue(op.resources._cleaned)
            self.assertFalse(op.resources.cleanup_errors)

    def test_short_wait_and_existing_integer_normalization_control(self) -> None:
        async def run():
            for value, normalized in ((100, 100), ("2", 2), (True, 1)):
                with self.subTest(value=value):
                    self.assertIn(f"Waited {normalized} ms", text(await self.call(ms=value)))
            self.assertEqual([op.timeout_seconds for op in self.created], [35.0] * 3)
            self.assertEqual(self.backend.calls, [])
            self.assert_idle("a")

        asyncio.run(run())

    def test_published_maximum_runs_with_barrier_and_controlled_elapsed_clock(self) -> None:
        entered, advance = threading.Event(), threading.Event()
        elapsed = [100.0]
        requested = []
        bind = self.server._bind_managed_board_resources

        def controlled_clock(op):
            bind(op)

            def wait_for_clock(seconds):
                requested.append(seconds)
                entered.set()
                if not advance.wait(BOUND):
                    raise AssertionError("elapsed clock barrier was not advanced")
                elapsed[0] += seconds
                return False

            op.cancellation_requested.wait = wait_for_clock

        self.server.mcp.configure_operation_resources(controlled_clock)
        # Replace this module's time reference, never the process-wide clock.
        self.stack.enter_context(patch.object(misc, "time", SimpleNamespace(
            monotonic=lambda: elapsed[0],
        )))

        async def run():
            schema = next(t for t in await self.server.mcp.list_tools() if t.name == "wait")
            self.assertEqual(schema.inputSchema["properties"]["ms"]["minimum"], 1)
            self.assertEqual(schema.inputSchema["properties"]["ms"]["maximum"], MAXIMUM_MS)
            self.assertIn("milliseconds", schema.description)
            task = asyncio.create_task(self.call(ms=MAXIMUM_MS, timeout_seconds=10**12))
            try:
                await signalled(entered)
                self.assertTrue(self.manager.batch_lock("a").locked())
                self.assertTrue(self.manager.worker_lock("a").locked())
                self.assertFalse(task.done())
                self.assertEqual(self.created[0].timeout_seconds, 35.0)
            finally:
                advance.set()
                with anyio.fail_after(BOUND):
                    result = await task
            self.assertIn("Waited 30000 ms", text(result))
            self.assertEqual(requested, [30.0])
            self.assertEqual(elapsed[0], 130.0)
            self.assert_idle("a")

        asyncio.run(run())

    def test_oversized_and_nonpositive_refuse_before_operation_reservation_or_target(self) -> None:
        bind = self.server._bind_managed_board_resources
        with (
            patch.object(self.manager, "batch_lock", wraps=self.manager.batch_lock) as reservation,
            patch.object(self.manager, "worker_lock", wraps=self.manager.worker_lock) as worker,
            patch.object(self.server.mcp, "_operation_resource_binder", wraps=bind) as binder,
        ):
            async def run():
                for value in (MAXIMUM_MS + 1, 3_600_000, 2_000_000_000, 10**400, 0, -1):
                    for extras in ({}, {"timeout_seconds": 10**12}):
                        with self.subTest(value=value, extras=extras):
                            with self.assertRaises(ToolError) as refused:
                                await self.call(ms=value, **extras)
                            self.assertIn("ms", str(refused.exception))
                            self.assertIn("30000" if value > MAXIMUM_MS else "1", str(refused.exception))
                            self.assertEqual(self.created, [])
                            self.assertEqual(self.journal, [])
                            reservation.assert_not_called()
                            worker.assert_not_called()
                            binder.assert_not_called()
                            self.assertEqual(self.backend.calls, [])

            asyncio.run(run())
        # Public recovery from the refusal uses the very same board key.
        self.assertIn("Waited 100 ms", text(asyncio.run(self.call())))
        self.assert_idle("a")

    def test_explicit_timeout_and_finalizer_cannot_expand_wait_budget(self) -> None:
        for duration in (1, MAXIMUM_MS, MAXIMUM_MS + 1, 2_000_000_000, 10**400):
            with self.subTest(duration=duration):
                self.assertEqual(operations.operation_timeout_seconds("wait", {
                    "ms": duration, "timeout_seconds": 10**12,
                    "on_exit": {"action": "uart_write", "timeout_seconds": 10**12},
                }), 35.0)
        with self.assertRaises(ToolError) as refused:
            asyncio.run(self.call(on_exit={"action": "reset_and_run"}))
        self.assertIn("on_exit", str(refused.exception))
        self.assertEqual(self.created, [])
        self.assertEqual(self.backend.calls, [])

    def test_cancelled_handler_exit_and_cleanup_own_slot_until_immediate_successor(self) -> None:
        self._cancel_and_reuse(scope_cancel=True)

    def test_manager_request_cancellation_uses_same_owned_exit_path(self) -> None:
        self._cancel_and_reuse(scope_cancel=False)

    def _cancel_and_reuse(self, *, scope_cancel):
        entered, woke, release = threading.Event(), threading.Event(), threading.Event()
        cancellation_wakes = []
        successor_queued, cleanup_entered = threading.Event(), threading.Event()
        cleanup_release = threading.Event()
        predecessor = []
        original_binder = self.server._bind_managed_board_resources
        runtime = self.server._session_store.start_session(
            board_id="a", connection_id="m10-host", probe_uid="fake-only", route_used="fixture",
        )
        connection = self.server.connection_manager.assign(
            "a", TargetSessionHandle(None, None, "fake-only", "fixture", None), runtime,
        )
        runtime.connection_id = connection.connection_id

        def bind(op):
            original_binder(op)
            if op.tool_name != "wait" or op.board_id != "a":
                return
            if predecessor:
                successor_queued.set()
                return
            predecessor.append(op)
            real_wait = op.cancellation_requested.wait

            def observe_wait(seconds):
                entered.set()
                result = real_wait(seconds)  # actual cancellation event, unchanged
                cancellation_wakes.append(result)
                woke.set()
                if not release.wait(BOUND):
                    raise AssertionError("handler exit barrier never released")
                self.journal.append(("last-delay-work", op.operation_id, "a"))
                return result

            def cleanup_barrier():
                self.journal.append(("cleanup-enter", op.operation_id, "a"))
                cleanup_entered.set()
                if not cleanup_release.wait(BOUND):
                    raise AssertionError("cleanup barrier never released")
                self.journal.append(("cleanup-exit", op.operation_id, "a"))

            op.cancellation_requested.wait = observe_wait
            op.resources.stop_io.append(cleanup_barrier)

        self.server.mcp.configure_operation_resources(bind)

        async def run():
            task = asyncio.create_task(self.call(ms=10_000))
            successor = None
            try:
                await signalled(entered)
                op = predecessor[0]
                if scope_cancel:
                    task.cancel()  # real request scope cancellation into dispatch
                else:
                    self.assertEqual(self.manager.cancel_request(op.request_id), 1)
                # Submit before awaiting the cancelled task's terminal.
                successor = asyncio.create_task(self.call(ms=100))
                await signalled(woke)
                await signalled(successor_queued)
                self.assertEqual(cancellation_wakes, [True])
                queued = next(x for x in self.created if x.board_id == "a" and x is not op)
                self.assertIsNone(queued.handler_started_at)
                self.assertFalse(op.done.is_set())
                self.assertTrue(self.manager.batch_lock("a").locked())
                self.assertTrue(self.manager.worker_lock("a").locked())
                self.assertNotIn(op.operation_id, self.handler_exits)
                self.assertFalse(successor.done())
                with anyio.fail_after(BOUND):
                    self.assertIn("Waited 25 ms", text(await self.call("b", 25)))
                self.assertFalse(successor.done())
                release.set()
                await signalled(cleanup_entered)
                self.assertTrue(self.handler_exits[op.operation_id].is_set())
                self.assertFalse(op.done.is_set())
                self.assertTrue(self.manager.batch_lock("a").locked())
                self.assertTrue(self.manager.worker_lock("a").locked())
                self.assertFalse(successor.done())
                self.assertIsNone(queued.handler_started_at)
            finally:
                release.set()
                cleanup_release.set()
                if not task.done() and not predecessor:
                    task.cancel()
                with anyio.fail_after(BOUND):
                    if scope_cancel:
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        with self.assertRaises(ToolError):
                            await task
                    if successor is not None:
                        self.assertIn("Waited 100 ms", text(await successor))
            op = predecessor[0]
            self.assertTrue(op.done.is_set())
            self.handler_threads[op.operation_id].join(BOUND)
            self.assertFalse(self.handler_threads[op.operation_id].is_alive())
            # FastMCP wraps synchronous handler exceptions in ToolError. Prove
            # the real checkpoint is the cause rather than assuming a raw type.
            causes = []
            error = op.error
            while error is not None and error not in causes:
                causes.append(error)
                error = error.__cause__
            self.assertTrue(any(isinstance(e, operations.OperationCancelledError) for e in causes))
            self.assertIsNone(op.result)
            self.assertIs(self.server.connection_manager.connection_for("a"), connection)
            self.assertIsNone(runtime.closed_at)
            self.assertEqual(self.backend.calls, [])
            self.assert_idle("a", "b")
            old_events = [row for row in self.journal if row[1] == op.operation_id]
            self.assertEqual([row[0] for row in old_events], [
                "handler-enter", "last-delay-work", "handler-exit", "cleanup-enter", "cleanup-exit",
            ])
            successor_op = next(x for x in self.created if x.board_id == "a" and x is not op)
            self.assertLess(self.journal.index(old_events[-1]), self.journal.index(
                ("handler-enter", successor_op.operation_id, "a"),
            ))
            with anyio.fail_after(BOUND):
                disconnected = await self.server.mcp.call_tool("disconnect", {"board_id": "a"})
            self.assertIn("Disconnected board 'a'", text(disconnected))
            self.assertIsNone(self.server.connection_manager.maybe_connection("a"))
            self.assertIsNotNone(runtime.closed_at)
            self.assertEqual(self.backend.calls, [("close", "fake-only")])
            self.assert_idle("a", "b")
            self.assertEqual([row for row in self.journal if row[1] == op.operation_id], old_events)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
