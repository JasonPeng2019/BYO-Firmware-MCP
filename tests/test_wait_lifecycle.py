"""M10 implementation checks for bounded admission and owned wait cancellation."""

from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import Mock, patch

import anyio
from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.kernel import operations
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP
from pyocd_debug_mcp.tools.misc import MiscToolServices, build_misc_handlers
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    tiered_test_server,
)


def _wait_handler(**overrides):
    return build_misc_handlers(
        MiscToolServices(
            **{
                "runtime_for": lambda _: None,
                "duration_ms": lambda _: 0,
                "record_event": Mock(),
                **overrides,
            }
        )
    )["wait"]


async def _until(event: threading.Event) -> None:
    with anyio.fail_after(3):
        while not event.is_set():
            await anyio.sleep(0)


class WaitAdmissionTests(unittest.TestCase):
    def test_public_maximum_and_existing_short_input_normalization(self) -> None:
        sleep = Mock()
        mcp = RegistryFastMCP("wait-admission")
        mcp.add_tool(_wait_handler(sleep=sleep), name="wait", structured_output=False)

        async def run() -> None:
            for value, expected in ((1, 1), (100, 100), (30_000, 30_000), ("100", 100), (True, 1)):
                result = await mcp.call_tool("wait", {"board_id": "a", "ms": value})
                self.assertIn(f"Waited {expected} ms", result[0].text)
                sleep.assert_called_with(expected / 1000)
            tool = next(tool for tool in await mcp.list_tools() if tool.name == "wait")
            self.assertEqual(tool.inputSchema["properties"]["ms"]["maximum"], 30_000)

        asyncio.run(run())

    def test_oversized_public_wait_never_creates_an_operation_or_calls_handler(self) -> None:
        sleep, runtime, record = Mock(), Mock(return_value=None), Mock()
        mcp = RegistryFastMCP("wait-refusal")
        mcp.add_tool(
            _wait_handler(sleep=sleep, runtime_for=runtime, record_event=record),
            name="wait",
            structured_output=False,
        )

        async def run() -> None:
            for value in (30_001, "30001", 30_001.0, 3_600_000, 2_000_000_000, 10**400):
                with self.subTest(value=value), patch.object(
                    operations.operation_manager,
                    "create",
                    side_effect=AssertionError("invalid wait created a managed operation"),
                ) as create:
                    with self.assertRaises(ToolError) as raised:
                        await mcp.call_tool("wait", {"board_id": "a", "ms": value})
                    self.assertIn("30000", str(raised.exception))
                    create.assert_not_called()

        asyncio.run(run())
        sleep.assert_not_called()
        runtime.assert_not_called()
        record.assert_not_called()

    def test_direct_handler_refuses_invalid_duration_without_sleep(self) -> None:
        sleep = Mock()
        wait = _wait_handler(sleep=sleep)
        for value in (0, -1, True, 1.5, "1", 30_001, 2_000_000_000):
            with self.subTest(value=value):
                self.assertIn("Refused [wait/out-of-range]", wait("a", value))
        sleep.assert_not_called()

    def test_wait_timeout_is_finite_even_for_huge_requests_or_extension_arguments(self) -> None:
        for value in (1, 30_000, 30_001, 2_000_000_000, 10**400):
            for extra in ({}, {"on_exit": {"action": "uart_write", "timeout_seconds": 1e12}}):
                with self.subTest(value=value, extra=extra):
                    self.assertEqual(
                        operations.operation_timeout_seconds("wait", {"ms": value, **extra}),
                        35.0,
                    )


class WaitCancellationTests(unittest.TestCase):
    def test_public_cancel_preserves_session_for_successor_and_real_disconnect(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(backend=backend)
            # Seed a connected fake transport, avoiding probe discovery/live I/O.
            runtime = server._session_store.start_session(
                board_id="a", connection_id="seed", probe_uid="fake-probe", route_used="test",
            )
            handle = TargetSessionHandle(None, None, "fake-probe", "test", None)
            connection = server.connection_manager.assign("a", handle, runtime)
            runtime.connection_id = connection.connection_id
            entered = threading.Event()
            owned: list[operations.ManagedOperation] = []

            def bind(operation: operations.ManagedOperation) -> None:
                server._bind_managed_board_resources(operation)
                if operation.tool_name != "wait":
                    return
                owned.append(operation)
                real_wait = operation.cancellation_requested.wait

                def observe(seconds: float) -> bool:
                    entered.set()
                    return real_wait(seconds)

                operation.cancellation_requested.wait = observe

            server.mcp.configure_operation_resources(bind)

            async def run() -> None:
                task = asyncio.create_task(
                    server.mcp.call_tool("wait", {"board_id": "a", "ms": 10_000})
                )
                try:
                    try:
                        await _until(entered)
                    finally:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            with anyio.fail_after(3):
                                await task
                    self.assertTrue(owned[0].done.is_set())
                    self.assertIs(server.connection_manager.connection_for("a"), connection)
                    self.assertIsNone(runtime.closed_at)
                    self.assertEqual(backend.calls, [])
                    with anyio.fail_after(3):
                        reply = await server.mcp.call_tool("wait", {"board_id": "a", "ms": 1})
                    self.assertIn("Waited 1 ms", reply[0].text)
                finally:
                    with anyio.fail_after(3):
                        reply = await server.mcp.call_tool("disconnect", {"board_id": "a"})
                    self.assertIn("Disconnected board 'a'", reply[0].text)
                self.assertIsNone(server.connection_manager.maybe_connection("a"))
                self.assertIsNotNone(runtime.closed_at)
                self.assertEqual(backend.calls, [("close", "fake-probe")])

            asyncio.run(run())

    def test_cancellation_retains_reservation_until_handler_and_cleanup_exit(self) -> None:
        manager = operations.OperationManager()
        entered, woke, release = threading.Event(), threading.Event(), threading.Event()
        successor_queued = threading.Event()
        journal: list[str] = []
        owned: list[operations.ManagedOperation] = []
        record = Mock()
        wait = _wait_handler(record_event=record)

        def bind(operation: operations.ManagedOperation) -> None:
            owned.append(operation)
            real_wait = operation.cancellation_requested.wait

            def hold_after_wake(seconds: float) -> bool:
                entered.set()
                cancelled = real_wait(seconds)
                woke.set()
                if not release.wait(3):
                    raise AssertionError("handler exit barrier was not released")
                journal.append("delay-return")
                return cancelled

            operation.cancellation_requested.wait = hold_after_wake
            operation.resources.stop_io.append(lambda: journal.append("cleanup"))

        async def run() -> None:
            task = asyncio.create_task(
                operations.dispatch(
                    "wait", "a", lambda: wait("a", 10_000), 30,
                    request_id="cancel-wait", resource_binder=bind, manager=manager,
                )
            )
            successor = None
            try:
                await _until(entered)
                self.assertEqual(manager.cancel_request("cancel-wait"), 1)
                await _until(woke)
                self.assertFalse(owned[0].done.is_set())
                self.assertTrue(manager.batch_lock("a").locked())
                self.assertTrue(manager.worker_lock("a").locked())
                successor = asyncio.create_task(
                    operations.dispatch(
                        "successor", "a", lambda: journal.append("successor"), 3,
                        resource_binder=lambda _: successor_queued.set(), manager=manager,
                    )
                )
                await _until(successor_queued)
                self.assertNotIn("successor", journal)
                self.assertEqual(
                    await operations.dispatch("peer", "b", lambda: "peer-ok", 3, manager=manager),
                    "peer-ok",
                )
                self.assertNotIn("successor", journal)
            finally:
                release.set()
                with self.assertRaises(operations.OperationCancelledError):
                    with anyio.fail_after(3):
                        await task
                if successor is not None:
                    with anyio.fail_after(3):
                        await successor
            self.assertEqual(journal, ["delay-return", "cleanup", "successor"])
            self.assertTrue(owned[0].done.is_set())
            self.assertEqual(manager.snapshots(), ())
            self.assertFalse(manager.batch_lock("a").locked())
            self.assertFalse(manager.worker_lock("a").locked())

        asyncio.run(run())
        record.assert_not_called()

    def test_request_scope_cancellation_exits_wait_before_dispatch_returns(self) -> None:
        manager = operations.OperationManager()
        entered = threading.Event()
        owned: list[operations.ManagedOperation] = []
        wait = _wait_handler()

        def bind(operation: operations.ManagedOperation) -> None:
            owned.append(operation)
            real_wait = operation.cancellation_requested.wait

            def observe(seconds: float) -> bool:
                entered.set()
                return real_wait(seconds)

            operation.cancellation_requested.wait = observe

        async def run() -> None:
            task = asyncio.create_task(
                operations.dispatch(
                    "wait", "a", lambda: wait("a", 10_000), 30,
                    resource_binder=bind, manager=manager,
                )
            )
            try:
                await _until(entered)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    with anyio.fail_after(3):
                        await task
            self.assertTrue(owned[0].done.is_set(), "terminal alone is insufficient")
            self.assertEqual(manager.snapshots(), ())
            self.assertFalse(manager.batch_lock("a").locked())
            self.assertFalse(manager.worker_lock("a").locked())

        asyncio.run(run())

    def test_operation_deadline_wakes_wait_and_finishes_owned_worker(self) -> None:
        manager = operations.OperationManager()
        owned: list[operations.ManagedOperation] = []
        record = Mock()
        wait = _wait_handler(record_event=record)

        async def run() -> None:
            with self.assertRaises(operations.OperationTimeoutError):
                await operations.dispatch(
                    "wait", "a", lambda: wait("a", 10_000), 0.1,
                    resource_binder=owned.append, manager=manager,
                )
            self.assertIsNotNone(owned[0].handler_started_at)
            self.assertTrue(owned[0].done.is_set())
            self.assertEqual(manager.snapshots(), ())
            self.assertFalse(manager.batch_lock("a").locked())

        asyncio.run(run())
        record.assert_not_called()
