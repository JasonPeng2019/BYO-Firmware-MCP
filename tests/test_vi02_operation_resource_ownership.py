"""VI02: interruption cleanup touches only debug resources the operation owned.

These tests run the production ``dispatch`` lifecycle, the real operation and
connection managers, the real gate and runtime session store, the server's
``_bind_managed_board_resources`` binder and, for preflight, the production
register guard. Batch controls use a real ``RegistryFastMCP`` with the
production batch handler. Only backend effects are simulated: the recording
backend observes which live handle each read/close/reset used. Threading
events (never sleeps) decide when an operation holds or waits for a board, so
queued, preflight and running interruptions are distinguished by lifecycle
state rather than by timing.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import patch

import anyio

from pyocd_debug_mcp import server
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.guardrails.gate import GateManager
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine
from pyocd_debug_mcp.kernel.operations import (
    BoardBusyError,
    OperationState,
    OperationTimeoutError,
    current_operation,
    dispatch,
    operation_manager,
)
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP, ToolRegistry
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.services import target_control
from pyocd_debug_mcp.services.connections import (
    ConnectionManager,
    ManagedConnection,
    stable_connection_identity,
)
from pyocd_debug_mcp.services.session_runtime import InMemorySessionStore
from pyocd_debug_mcp.target_errors import TargetConnectionError
from pyocd_debug_mcp.tools.batch import build_batch_handlers
from pyocd_debug_mcp.tools.registers import (
    RegisterPreconditionError,
    validate_guarded_register_call,
)


HANG_GUARD_SECONDS = 10.0
"""Upper bound that only converts a hang into a failure; never an oracle."""

QUEUE_DEADLINE_SECONDS = 0.25
"""A queued request's own finite A-11 bound while another operation holds the board."""

RUNNING_DEADLINE_SECONDS = 1.0
"""A started request's own finite A-11 bound; it waits on an event, not this value."""

BOARD = "vi02_board_a"
OTHER_BOARD = "vi02_board_b"
SUPPORTED_REGISTERS = tuple(f"r{index}" for index in range(13))
EVICTED_BY_INTERRUPTION = "operation cancelled or timed out"
EVICTED_BY_TRANSPORT_FAILURE = "target connection failed during operation"


async def _until(predicate: Callable[[], bool]) -> None:
    with anyio.fail_after(HANG_GUARD_SECONDS):
        while not predicate():
            await anyio.sleep(0.005)


def _wait_until_interrupted(entered: threading.Event) -> None:
    """Keep backend use in flight until this operation's own interruption arrives."""

    entered.set()
    operation = current_operation()
    assert operation is not None
    if not operation.cancellation_requested.wait(HANG_GUARD_SECONDS):
        raise AssertionError("operation was never interrupted")
    operation.checkpoint()


class _RecordingGate(GateManager):
    def __init__(self) -> None:
        super().__init__()
        self.clears: list[tuple[str, str]] = []

    def clear(self, board_id: str, reason: str):  # type: ignore[no-untyped-def]
        self.clears.append((board_id, reason))
        return super().clear(board_id, reason)


class _RecordingBackend:
    """Simulated SWD effects, recorded by the probe label of the handle used."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self.calls: list[tuple[str, str]] = []
        self.failures: dict[tuple[str, str], BaseException] = {}
        self.during: dict[tuple[str, str], Callable[[], None]] = {}

    def _record(self, method: str, handle: TargetSessionHandle) -> None:
        label = str(handle.probe_uid)
        with self._guard:
            self.calls.append((method, label))
            failure = self.failures.get((method, label))
            during = self.during.get((method, label))
        if during is not None:
            during()
        if failure is not None:
            raise failure

    def note(self, event: str, detail: str) -> None:
        with self._guard:
            self.calls.append((event, detail))

    def snapshot(self) -> list[tuple[str, str]]:
        with self._guard:
            return list(self.calls)

    def close(self, handle: TargetSessionHandle) -> None:
        self._record("close", handle)

    def release_reset(self, handle: TargetSessionHandle) -> None:
        self._record("release_reset", handle)

    def supported_core_registers(self, handle: TargetSessionHandle) -> tuple[str, ...]:
        self._record("supported_core_registers", handle)
        return SUPPORTED_REGISTERS

    def read_memory(
        self,
        handle: TargetSessionHandle,
        address: int,
        width_bits: int = 32,
        *,
        operation_timeout_seconds: float | None = None,
    ) -> int:
        self._record("read_memory", handle)
        return 0


class _OwnershipFixture(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.assertIs(server.target_control, target_control)
        runs = tempfile.TemporaryDirectory(prefix="vi02-runs-")
        self.addCleanup(runs.cleanup)
        self.connections = ConnectionManager()
        self.gate = _RecordingGate()
        self.sessions = InMemorySessionStore(Path(runs.name))
        self.backend = _RecordingBackend()
        self.guard_calls: list[str] = []
        original_backend = target_control._BACKEND
        target_control.configure_backend_for_tests(self.backend)
        self.addCleanup(target_control.configure_backend_for_tests, original_backend)
        for name, value in (
            ("connection_manager", self.connections),
            ("gate_manager", self.gate),
            ("_session_store", self.sessions),
        ):
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.threads_before = set(threading.enumerate())
        self.addCleanup(self.assert_test_workers_finished)

    def join_workers(self) -> list[str]:
        """Join every board worker this test started; return any still alive."""

        started = [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("mcp-") and thread not in self.threads_before
        ]
        for thread in started:
            thread.join(HANG_GUARD_SECONDS)
        return [thread.name for thread in started if thread.is_alive()]

    def assert_test_workers_finished(self) -> None:
        self.assertEqual(self.join_workers(), [])
        self.assertEqual(
            [s for s in operation_manager.snapshots() if s.board_id in {BOARD, OTHER_BOARD}],
            [],
        )

    def connect(self, label: str, board: str = BOARD) -> ManagedConnection:
        handle = TargetSessionHandle(None, None, label, label, None)
        runtime = self.sessions.start_session(
            board_id=board,
            connection_id=stable_connection_identity(handle),
            probe_uid=label,
            route_used=label,
        )
        connection = self.connections.assign(board, handle, runtime)
        self.gate.stamp_validation(
            board_id=board,
            connection_id=connection.connection_id,
            probe_identity=label,
            observed_mcu="VI02-TEST-MCU",
            validation_run=f"validation-{label}",
            map_digest=f"map-{label}",
        )
        return connection

    def board_state(self, board: str = BOARD) -> dict[str, object]:
        """Observable live-connection, gate and backend state for one board."""

        current = self.connections.maybe_connection(board)
        stamp = self.gate.snapshot(board)
        return {
            "assigned_probe": None if current is None else current.handle.probe_uid,
            "gate_stamp_connection": None if stamp is None else stamp.live_identity.connection_id,
            "gate_clears": [entry for entry in self.gate.clears if entry[0] == board],
            "backend_calls": self.backend.snapshot(),
        }

    def evicted(self, reason: str, backend_calls: list[tuple[str, str]]) -> dict[str, object]:
        return {
            "assigned_probe": None,
            "gate_stamp_connection": None,
            "gate_clears": [(BOARD, reason)],
            "backend_calls": backend_calls,
        }

    def preserved(
        self,
        owner: ManagedConnection,
        backend_calls: list[tuple[str, str]],
        board: str = BOARD,
    ) -> dict[str, object]:
        return {
            "assigned_probe": owner.handle.probe_uid,
            "gate_stamp_connection": owner.connection_id,
            "gate_clears": [],
            "backend_calls": backend_calls,
        }

    async def call(
        self,
        tool_name: str,
        operation: Callable[[], Any],
        timeout: float,
        *,
        guard: Callable[[], None] | None = None,
        board: str = BOARD,
    ) -> Any:
        # Mirrors the registry's guarded layer-2 synchronous dispatch arguments.
        # Every call also carries a recording on_exit-style finalizer, so each
        # exit route shows whether its finalizer ran.
        def recorded_guard() -> None:
            self.guard_calls.append(tool_name)
            if guard is not None:
                guard()

        return await dispatch(
            tool_name,
            board,
            operation,
            timeout,
            before_execution=recorded_guard,
            execution_lock=self.connections.lock_for(board),
            resource_binder=server._bind_managed_board_resources,
            finalizer=partial(self.backend.note, "finalizer", tool_name),
        )

    @staticmethod
    async def cancel_when(
        started: Callable[[], bool], request: Callable[[], Awaitable[Any]]
    ) -> dict[str, object]:
        """Cancel one real AnyIO request as soon as it reaches the named state."""

        outcome: dict[str, object] = {}
        async with anyio.create_task_group() as scope:

            async def run() -> None:
                try:
                    outcome["result"] = await request()
                except anyio.get_cancelled_exc_class():
                    outcome["cancelled"] = True
                    raise

            scope.start_soon(run)
            await _until(started)
            scope.cancel_scope.cancel()
        return outcome

    @staticmethod
    def queued(tool_name: str, board: str = BOARD) -> bool:
        return any(
            snapshot.board_id == board
            and snapshot.tool_name == tool_name
            and snapshot.state is OperationState.QUEUED
            for snapshot in operation_manager.snapshots()
        )

    @staticmethod
    def board_idle(board: str = BOARD) -> bool:
        return not any(s.board_id == board for s in operation_manager.snapshots())

    def read_current(self, board: str = BOARD) -> int:
        return target_control.read_memory(self.connections.handle_for(board), 0x20000000)

    def holder(
        self,
        entered: threading.Event,
        release: threading.Event,
        after_release: Callable[[], None] | None = None,
    ) -> Callable[[], str]:
        def operation() -> str:
            self.read_current()
            entered.set()
            if not release.wait(HANG_GUARD_SECONDS):
                raise AssertionError("test never released the board holder")
            if after_release is not None:
                after_release()
            return "holder-complete"

        return operation

    @staticmethod
    def register_guard(name: str = "r0") -> Callable[[], None]:
        # The production write_cpu_register preflight reads the connected core's
        # supported registers through the live board handle before the handler.
        return partial(
            validate_guarded_register_call,
            server.register_services,
            "write_cpu_register",
            BOARD,
            {"name": name, "value": 1},
        )


class QueuedInterruptionOwnershipTests(_OwnershipFixture):
    """A holds the board; B is interrupted before it ever acquires it."""

    def expected_owner_preserved(self, owner: ManagedConnection) -> dict[str, object]:
        return {
            # B never ran its preflight guard, handler or finalizer.
            "guard_calls": ["halt"],
            "queued_handler": [],
            "while_owner_holds_board": self.preserved(owner, [("read_memory", "probe-a")]),
            "holder_result": "holder-complete",
            "owner_runtime_closed": False,
            # Only A's own finalizer and handler-started reset release follow.
            "after_owner_finishes": self.preserved(
                owner,
                [
                    ("read_memory", "probe-a"),
                    ("finalizer", "halt"),
                    ("release_reset", "probe-a"),
                ],
            ),
        }

    def run_queued_interruption(
        self, interrupt: Callable[[Callable[[], Awaitable[Any]]], Awaitable[object]]
    ) -> tuple[ManagedConnection, dict[str, object]]:
        owner = self.connect("probe-a")
        entered, release = threading.Event(), threading.Event()
        queued_handler: list[str] = []
        outcome: dict[str, object] = {}

        async def scenario() -> None:
            async with anyio.create_task_group() as group:

                async def run_holder() -> None:
                    outcome["holder"] = await self.call(
                        "halt", self.holder(entered, release), HANG_GUARD_SECONDS
                    )

                group.start_soon(run_holder)
                try:
                    await _until(entered.is_set)
                    outcome["queued"] = await interrupt(
                        partial(self.call, "read_memory", lambda: queued_handler.append("handler"))
                    )
                    outcome["while_owner_holds_board"] = self.board_state()
                    outcome["guard_calls"] = list(self.guard_calls)
                finally:
                    release.set()

        anyio.run(scenario)
        self.assertEqual(self.join_workers(), [])
        return owner, {
            "guard_calls": outcome["guard_calls"],
            "queued_handler": queued_handler,
            "while_owner_holds_board": outcome["while_owner_holds_board"],
            "holder_result": outcome["holder"],
            "owner_runtime_closed": owner.runtime_session.closed_at is not None,
            "after_owner_finishes": self.board_state(),
            "queued_outcome": outcome["queued"],
        }

    def test_queued_timeout_preserves_the_active_owner(self) -> None:
        async def time_out(request: Callable[..., Awaitable[Any]]) -> object:
            try:
                await request(QUEUE_DEADLINE_SECONDS)
            except BoardBusyError as exc:
                return type(exc).__name__
            return "returned"

        owner, observed = self.run_queued_interruption(time_out)

        self.assertEqual(observed.pop("queued_outcome"), "BoardBusyError")
        self.assertEqual(observed, self.expected_owner_preserved(owner))

    def test_queued_cancellation_preserves_the_active_owner(self) -> None:
        async def cancel(request: Callable[..., Awaitable[Any]]) -> object:
            return await self.cancel_when(
                lambda: self.queued("read_memory"), partial(request, HANG_GUARD_SECONDS)
            )

        owner, observed = self.run_queued_interruption(cancel)

        self.assertEqual(observed.pop("queued_outcome"), {"cancelled": True})
        self.assertEqual(observed, self.expected_owner_preserved(owner))


class PendingPlanLifecycleTests(_OwnershipFixture):
    """Waiting and refused preflight leave a real plan available for execution."""

    def setUp(self) -> None:
        super().setUp()
        self.registry = ToolRegistry()
        self.registry.register("reset_and_halt", locked=True)
        self.plans = PlanEngine(ServerRun(), self.registry)
        self.addCleanup(self.plans.close_run)
        self.plans.null_response("reset_and_halt-plan")
        self.plans.submit(
            "reset_and_halt-plan",
            {
                "board_id": BOARD,
                "hypothesis": "The pending plan survives a request that never executes.",
                "strategy": "Queue, interrupt, refuse preflight, then execute the same plan.",
                "hypothesis_made": True,
                "strategy_evaluated": True,
                "expected_fail_return": "The queued request is interrupted without backend use.",
                "expected_success_return": "The later execution consumes exactly one allowance.",
                "max_calls": 1,
                "max_calls_buffer": 0,
                "action_parameters": {},
            },
        )

    async def planned_call(
        self, timeout: float, precondition: Callable[[], None] | None = None
    ) -> Any:
        return await self.call(
            "reset_and_halt",
            self.read_current,
            timeout,
            guard=lambda: self.plans.enforce(
                "reset_and_halt", BOARD, {}, preconditions=precondition
            ),
        )

    def assert_plan_pending(self) -> None:
        plan = self.plans.active_plan("reset_and_halt", BOARD)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.remaining_calls, 1)
        self.assertTrue(self.registry.is_unlocked("reset_and_halt", BOARD))

    def pending_plan_scenario(self, cancel: bool) -> None:
        owner = self.connect("probe-a")
        entered, release = threading.Event(), threading.Event()

        def refuse_preflight() -> None:
            raise ValueError("ordinary preflight refusal")

        async def scenario() -> None:
            async with anyio.create_task_group() as group:
                group.start_soon(
                    partial(self.call, "halt", self.holder(entered, release), HANG_GUARD_SECONDS)
                )
                try:
                    await _until(entered.is_set)
                    if cancel:
                        outcome = await self.cancel_when(
                            lambda: self.queued("reset_and_halt"),
                            partial(self.planned_call, HANG_GUARD_SECONDS),
                        )
                        self.assertEqual(outcome, {"cancelled": True})
                    else:
                        with self.assertRaises(BoardBusyError):
                            await self.planned_call(QUEUE_DEADLINE_SECONDS)
                    self.assert_plan_pending()
                    self.assertEqual(self.guard_calls, ["halt"])
                    self.assertEqual(
                        self.board_state(), self.preserved(owner, [("read_memory", "probe-a")])
                    )
                finally:
                    release.set()

            with self.assertRaisesRegex(ValueError, "ordinary preflight refusal"):
                await self.planned_call(HANG_GUARD_SECONDS, refuse_preflight)
            self.assert_plan_pending()
            before_retry = self.backend.snapshot()
            self.assertEqual(
                before_retry,
                [
                    ("read_memory", "probe-a"),
                    ("finalizer", "halt"),
                    ("release_reset", "probe-a"),
                ],
            )
            self.assertEqual(await self.planned_call(HANG_GUARD_SECONDS), 0)
            self.assertIsNone(self.plans.active_plan("reset_and_halt", BOARD))
            self.assertFalse(self.registry.is_unlocked("reset_and_halt", BOARD))
            self.assertEqual(
                self.board_state(),
                self.preserved(
                    owner,
                    before_retry
                    + [
                        ("read_memory", "probe-a"),
                        ("finalizer", "reset_and_halt"),
                        ("release_reset", "probe-a"),
                    ],
                ),
            )

        anyio.run(scenario)
        self.assertIsNone(current_operation())
        self.assertFalse(owner.runtime_session.closed_at)
        self.plans.close_run()
        self.assertEqual(self.plans.server_run.plans, {})

    def test_queued_timeout_preserves_pending_plan_until_execution(self) -> None:
        self.pending_plan_scenario(cancel=False)

    def test_queued_cancellation_preserves_pending_plan_until_execution(self) -> None:
        self.pending_plan_scenario(cancel=True)


class BinderFailureLifecycleTests(_OwnershipFixture):
    def test_partial_binding_failure_cleans_resources_and_releases_both_dispatch_paths(
        self,
    ) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                events: list[str] = []

                def fail_binding(operation: Any) -> None:
                    operation.resources.stop_io.append(lambda: events.append("cleanup"))
                    raise ValueError("resource binder failed")

                def sync_handler() -> str:
                    events.append("handler")
                    return "healthy"

                async def async_handler() -> str:
                    return sync_handler()

                handler = async_handler if asynchronous else sync_handler

                async def scenario() -> None:
                    with self.assertRaisesRegex(ValueError, "resource binder failed"):
                        await dispatch(
                            "read_memory",
                            BOARD,
                            handler,
                            HANG_GUARD_SECONDS,
                            resource_binder=fail_binding,
                            finalizer=lambda: events.append("finalizer"),
                        )
                    self.assertEqual(events, ["cleanup"])
                    self.assertTrue(self.board_idle())
                    self.assertIsNone(current_operation())
                    self.assertEqual(
                        await dispatch("read_memory", BOARD, handler, HANG_GUARD_SECONDS),
                        "healthy",
                    )

                anyio.run(scenario)
                self.assertEqual(events, ["cleanup", "handler"])
                self.assertIsNone(current_operation())


class ReplacementCaptureTests(_OwnershipFixture):
    """A connection replaced while B waits is the one B binds, uses and cleans."""

    def replace_while_queued(
        self,
        queued_operation: Callable[[], Any],
        started: threading.Event | None = None,
    ) -> dict[str, object]:
        """Hold C1, queue B, replace C1 with C2, then let B acquire the board.

        With ``started`` B is cancelled once its handler is using C2; otherwise B
        runs to its own outcome.
        """

        original = self.connect("probe-c1")
        entered, release = threading.Event(), threading.Event()
        connections: dict[str, ManagedConnection] = {"c1": original}
        outcome: dict[str, object] = {}

        def replace_connection() -> None:
            # A reconnect while B waits: the old live handle is closed and a new
            # validated connection is assigned before B acquires the board.
            old = self.connections.clear(BOARD)
            assert old is original
            target_control.close_session(old.handle)
            self.sessions.close_session(old.runtime_session)
            connections["c2"] = self.connect("probe-c2")

        async def run_queued() -> None:
            try:
                outcome["queued"] = await self.call(
                    "read_memory", queued_operation, HANG_GUARD_SECONDS
                )
            except Exception as exc:
                outcome["queued"] = type(exc).__name__

        async def scenario() -> None:
            async with anyio.create_task_group() as group:

                async def run_replacer() -> None:
                    outcome["replacer"] = await self.call(
                        "connect",
                        self.holder(entered, release, replace_connection),
                        HANG_GUARD_SECONDS,
                    )

                group.start_soon(run_replacer)
                try:
                    await _until(entered.is_set)
                    async with anyio.create_task_group() as request:
                        request.start_soon(run_queued)
                        await _until(lambda: self.queued("read_memory"))
                        release.set()
                        if started is not None:
                            await _until(started.is_set)
                            request.cancel_scope.cancel()
                            outcome["queued"] = "cancelled"
                finally:
                    release.set()

        anyio.run(scenario)
        self.assertEqual(self.join_workers(), [])
        self.assertEqual(outcome["replacer"], "holder-complete")
        return {
            **self.board_state(),
            "queued": outcome["queued"],
            "original_runtime_closed": original.runtime_session.closed_at is not None,
            "replacement_runtime_closed": connections["c2"].runtime_session.closed_at is not None,
        }

    def test_connection_replaced_while_queued_is_the_one_cleaned_after_failure(self) -> None:
        self.backend.failures[("read_memory", "probe-c2")] = TargetConnectionError(
            "probe-c2 transport lost"
        )

        observed = self.replace_while_queued(self.read_current)

        self.assertEqual(
            observed,
            {
                # The failed transport is the replacement B actually used.
                **self.evicted(
                    EVICTED_BY_TRANSPORT_FAILURE,
                    [
                        ("read_memory", "probe-c1"),
                        ("close", "probe-c1"),
                        ("finalizer", "connect"),
                        ("read_memory", "probe-c2"),
                        ("finalizer", "read_memory"),
                        ("close", "probe-c2"),
                        ("release_reset", "probe-c2"),
                    ],
                ),
                "queued": "TargetConnectionError",
                # The stale original is closed once by its replacer, never reset again.
                "original_runtime_closed": True,
                "replacement_runtime_closed": True,
            },
        )

    def test_connection_replaced_while_queued_is_the_one_evicted_on_cancellation(self) -> None:
        using_replacement = threading.Event()

        def use_then_wait() -> None:
            self.read_current()
            _wait_until_interrupted(using_replacement)

        observed = self.replace_while_queued(use_then_wait, using_replacement)

        self.assertEqual(
            observed,
            {
                **self.evicted(
                    EVICTED_BY_INTERRUPTION,
                    [
                        ("read_memory", "probe-c1"),
                        ("close", "probe-c1"),
                        ("finalizer", "connect"),
                        ("read_memory", "probe-c2"),
                        ("finalizer", "read_memory"),
                        ("close", "probe-c2"),
                        ("release_reset", "probe-c2"),
                    ],
                ),
                "queued": "cancelled",
                "original_runtime_closed": True,
                "replacement_runtime_closed": True,
            },
        )


class RunningInterruptionControlTests(_OwnershipFixture):
    """Controls that already-owned interruption cleanup still runs."""

    def test_running_timeout_evicts_the_owned_connection(self) -> None:
        owner = self.connect("probe-a")
        entered = threading.Event()
        outcome: dict[str, object] = {}

        def uncertain_backend_wait() -> None:
            self.read_current()
            _wait_until_interrupted(entered)

        async def scenario() -> None:
            try:
                await self.call("read_memory", uncertain_backend_wait, RUNNING_DEADLINE_SECONDS)
            except OperationTimeoutError as exc:
                outcome["timeout"] = exc

        anyio.run(scenario)

        self.assertEqual(self.join_workers(), [])
        self.assertTrue(entered.is_set())
        self.assertIsInstance(outcome.get("timeout"), OperationTimeoutError)
        self.assertNotIsInstance(outcome.get("timeout"), BoardBusyError)
        self.assertEqual(
            {**self.board_state(), "runtime_closed": owner.runtime_session.closed_at is not None},
            {
                **self.evicted(
                    EVICTED_BY_INTERRUPTION,
                    [
                        ("read_memory", "probe-a"),
                        ("finalizer", "read_memory"),
                        ("close", "probe-a"),
                        ("release_reset", "probe-a"),
                    ],
                ),
                "runtime_closed": True,
            },
        )

    def test_running_cancellation_evicts_the_owned_connection(self) -> None:
        owner = self.connect("probe-a")
        entered = threading.Event()

        def uncertain_backend_wait() -> None:
            self.read_current()
            _wait_until_interrupted(entered)

        outcome = anyio.run(
            self.cancel_when,
            entered.is_set,
            partial(self.call, "read_memory", uncertain_backend_wait, HANG_GUARD_SECONDS),
        )

        self.assertEqual(self.join_workers(), [])
        self.assertEqual(outcome, {"cancelled": True})
        self.assertEqual(
            {**self.board_state(), "runtime_closed": owner.runtime_session.closed_at is not None},
            {
                **self.evicted(
                    EVICTED_BY_INTERRUPTION,
                    [
                        ("read_memory", "probe-a"),
                        ("finalizer", "read_memory"),
                        ("close", "probe-a"),
                        ("release_reset", "probe-a"),
                    ],
                ),
                "runtime_closed": True,
            },
        )

    def test_stale_running_cleanup_never_closes_or_resets_a_replacement(self) -> None:
        # Identity control: another owner replaces the connection this running
        # operation captured. Compare-and-clear must leave the replacement alone.
        original = self.connect("probe-c1")
        entered = threading.Event()
        replacement: dict[str, ManagedConnection] = {}

        def replaced_while_running() -> None:
            self.read_current()
            old = self.connections.clear_if_current(BOARD, original)
            assert old is original
            target_control.close_session(old.handle)
            self.sessions.close_session(old.runtime_session)
            replacement["c2"] = self.connect("probe-c2")
            _wait_until_interrupted(entered)

        outcome = anyio.run(
            self.cancel_when,
            entered.is_set,
            partial(self.call, "read_memory", replaced_while_running, HANG_GUARD_SECONDS),
        )

        self.assertEqual(self.join_workers(), [])
        self.assertEqual(outcome, {"cancelled": True})
        self.assertEqual(
            {
                **self.board_state(),
                "original_runtime_closed": original.runtime_session.closed_at is not None,
                "replacement_runtime_closed": (
                    replacement["c2"].runtime_session.closed_at is not None
                ),
            },
            {
                # Only the captured, already replaced handle receives the reset release.
                **self.preserved(
                    replacement["c2"],
                    [
                        ("read_memory", "probe-c1"),
                        ("close", "probe-c1"),
                        ("finalizer", "read_memory"),
                        ("release_reset", "probe-c1"),
                    ],
                ),
                "original_runtime_closed": True,
                "replacement_runtime_closed": False,
            },
        )


class PreflightOwnershipTests(_OwnershipFixture):
    """The production register preflight uses the debug connection before the handler."""

    def run_register_write(
        self, interrupt: Callable[[Callable[..., Awaitable[Any]]], Awaitable[object]], name="r0"
    ) -> tuple[object, list[str]]:
        handler: list[str] = []
        request = partial(
            self.call,
            "write_cpu_register",
            lambda: handler.append("handler"),
            guard=self.register_guard(name),
        )
        outcome = anyio.run(interrupt, request)
        self.assertEqual(self.join_workers(), [])
        return outcome, handler

    def test_preflight_transport_failure_evicts_only_the_used_connection(self) -> None:
        owner = self.connect("probe-a")
        self.backend.failures[("supported_core_registers", "probe-a")] = TargetConnectionError(
            "probe-a transport lost during preflight"
        )

        async def run(request: Callable[..., Awaitable[Any]]) -> object:
            try:
                await request(HANG_GUARD_SECONDS)
            except TargetConnectionError as exc:
                return type(exc).__name__
            return "returned"

        outcome, handler = self.run_register_write(run)

        self.assertEqual(
            {
                "outcome": outcome,
                "guard_calls": self.guard_calls,
                "handler": handler,
                **self.board_state(),
                "runtime_closed": owner.runtime_session.closed_at is not None,
            },
            {
                "outcome": "TargetConnectionError",
                "guard_calls": ["write_cpu_register"],
                "handler": [],
                # Preflight used the transport, but no handler began: no finalizer
                # and no reset release on the evicted handle.
                **self.evicted(
                    EVICTED_BY_TRANSPORT_FAILURE,
                    [("supported_core_registers", "probe-a"), ("close", "probe-a")],
                ),
                "runtime_closed": True,
            },
        )

    def test_cancellation_during_preflight_backend_use_evicts_without_reset(self) -> None:
        owner = self.connect("probe-a")
        in_preflight = threading.Event()
        self.backend.during[("supported_core_registers", "probe-a")] = partial(
            _wait_until_interrupted, in_preflight
        )

        async def cancel(request: Callable[..., Awaitable[Any]]) -> object:
            return await self.cancel_when(in_preflight.is_set, partial(request, HANG_GUARD_SECONDS))

        outcome, handler = self.run_register_write(cancel)

        self.assertEqual(
            {
                "outcome": outcome,
                "guard_calls": self.guard_calls,
                "handler": handler,
                **self.board_state(),
                "runtime_closed": owner.runtime_session.closed_at is not None,
            },
            {
                "outcome": {"cancelled": True},
                "guard_calls": ["write_cpu_register"],
                "handler": [],
                **self.evicted(
                    EVICTED_BY_INTERRUPTION,
                    [("supported_core_registers", "probe-a"), ("close", "probe-a")],
                ),
                "runtime_closed": True,
            },
        )

    def test_timeout_during_preflight_backend_use_is_a_started_operation_timeout(self) -> None:
        owner = self.connect("probe-a")
        in_preflight = threading.Event()
        self.backend.during[("supported_core_registers", "probe-a")] = partial(
            _wait_until_interrupted, in_preflight
        )

        async def time_out(request: Callable[..., Awaitable[Any]]) -> object:
            try:
                await request(RUNNING_DEADLINE_SECONDS)
            except OperationTimeoutError as exc:
                return type(exc).__name__
            return "returned"

        outcome, handler = self.run_register_write(time_out)

        self.assertTrue(in_preflight.is_set())
        self.assertEqual(
            {
                "outcome": outcome,
                "guard_calls": self.guard_calls,
                "handler": handler,
                **self.board_state(),
                "runtime_closed": owner.runtime_session.closed_at is not None,
            },
            {
                # Preflight started execution, so this is not reported as BoardBusyError.
                "outcome": "OperationTimeoutError",
                "guard_calls": ["write_cpu_register"],
                "handler": [],
                **self.evicted(
                    EVICTED_BY_INTERRUPTION,
                    [("supported_core_registers", "probe-a"), ("close", "probe-a")],
                ),
                "runtime_closed": True,
            },
        )

    def test_preflight_refusal_after_backend_use_preserves_the_connection(self) -> None:
        owner = self.connect("probe-a")

        async def run(request: Callable[..., Awaitable[Any]]) -> object:
            try:
                await request(HANG_GUARD_SECONDS)
            except RegisterPreconditionError as exc:
                return str(exc)
            return "returned"

        # s0 is an ordinary FP register name that this simulated core does not report.
        outcome, handler = self.run_register_write(run, name="s0")

        self.assertEqual(
            {
                "outcome": outcome,
                "handler": handler,
                **self.board_state(),
                "runtime_closed": owner.runtime_session.closed_at is not None,
            },
            {
                "outcome": "Refused [register/unsupported]: "
                "Register 's0' is not supported by the connected core.",
                "handler": [],
                **self.preserved(owner, [("supported_core_registers", "probe-a")]),
                "runtime_closed": False,
            },
        )


class NormalAndOrdinaryErrorControlTests(_OwnershipFixture):
    """Completed and ordinarily failed handlers keep the healthy connection."""

    def test_successful_handler_keeps_the_connection_and_releases_reset(self) -> None:
        owner = self.connect("probe-a")

        result = anyio.run(partial(self.call, "read_memory", self.read_current, HANG_GUARD_SECONDS))

        self.assertEqual(self.join_workers(), [])
        self.assertEqual(
            {
                "result": result,
                "guard_calls": self.guard_calls,
                **self.board_state(),
                "runtime_closed": owner.runtime_session.closed_at is not None,
            },
            {
                "result": 0,
                "guard_calls": ["read_memory"],
                **self.preserved(
                    owner,
                    [
                        ("read_memory", "probe-a"),
                        ("finalizer", "read_memory"),
                        ("release_reset", "probe-a"),
                    ],
                ),
                "runtime_closed": False,
            },
        )

    def test_ordinary_handler_error_keeps_the_connection_and_releases_reset(self) -> None:
        owner = self.connect("probe-a")

        def fails_ordinarily() -> None:
            self.read_current()
            raise ValueError("ordinary handler failure")

        async def scenario() -> object:
            try:
                await self.call("read_memory", fails_ordinarily, HANG_GUARD_SECONDS)
            except ValueError as exc:
                return str(exc)
            return "returned"

        outcome = anyio.run(scenario)

        self.assertEqual(self.join_workers(), [])
        self.assertEqual(
            {
                "outcome": outcome,
                **self.board_state(),
                "runtime_closed": owner.runtime_session.closed_at is not None,
            },
            {
                "outcome": "ordinary handler failure",
                **self.preserved(
                    owner,
                    [
                        ("read_memory", "probe-a"),
                        ("finalizer", "read_memory"),
                        ("release_reset", "probe-a"),
                    ],
                ),
                "runtime_closed": False,
            },
        )

    def test_interrupted_uart_metadata_and_wait_operations_never_touch_swd(self) -> None:
        owner = self.connect("probe-a")
        outcomes: dict[str, object] = {}

        async def time_out(tool_name: str) -> object:
            entered = threading.Event()
            try:
                await self.call(
                    tool_name,
                    partial(_wait_until_interrupted, entered),
                    RUNNING_DEADLINE_SECONDS,
                )
            except OperationTimeoutError as exc:
                return f"{type(exc).__name__} after start={entered.is_set()}"
            return "returned"

        async def cancel(tool_name: str) -> object:
            entered = threading.Event()
            return await self.cancel_when(
                entered.is_set,
                partial(
                    self.call,
                    tool_name,
                    partial(_wait_until_interrupted, entered),
                    HANG_GUARD_SECONDS,
                ),
            )

        for tool_name, interrupt in (
            ("read_serial", time_out),
            ("serial_exchange_raw", cancel),
            ("get_board_info", cancel),
            ("wait", time_out),
        ):
            outcomes[tool_name] = anyio.run(interrupt, tool_name)

        self.assertEqual(self.join_workers(), [])
        self.assertEqual(
            {
                "outcomes": outcomes,
                **self.board_state(),
                "runtime_closed": owner.runtime_session.closed_at is not None,
            },
            {
                "outcomes": {
                    "read_serial": "OperationTimeoutError after start=True",
                    "serial_exchange_raw": {"cancelled": True},
                    "get_board_info": {"cancelled": True},
                    "wait": "OperationTimeoutError after start=True",
                },
                # Their finalizers still run; no SWD close, eviction or reset release.
                **self.preserved(
                    owner,
                    [
                        ("finalizer", "read_serial"),
                        ("finalizer", "serial_exchange_raw"),
                        ("finalizer", "get_board_info"),
                        ("finalizer", "wait"),
                    ],
                ),
                "runtime_closed": False,
            },
        )


class OtherBoardIsolationTests(_OwnershipFixture):
    def test_other_board_call_completes_while_board_a_is_interrupted(self) -> None:
        owner_a = self.connect("probe-a")
        owner_b = self.connect("probe-b", board=OTHER_BOARD)
        a_entered = threading.Event()
        b_entered, b_release = threading.Event(), threading.Event()
        outcome: dict[str, object] = {}

        def interrupted_on_a() -> None:
            self.read_current()
            _wait_until_interrupted(a_entered)

        def healthy_on_b() -> str:
            self.read_current(OTHER_BOARD)
            b_entered.set()
            if not b_release.wait(HANG_GUARD_SECONDS):
                raise AssertionError("test never released board B")
            return "board-b-complete"

        async def scenario() -> None:
            async with anyio.create_task_group() as group:

                async def run_b() -> None:
                    outcome["board_b"] = await self.call(
                        "read_memory", healthy_on_b, HANG_GUARD_SECONDS, board=OTHER_BOARD
                    )

                group.start_soon(run_b)
                try:
                    await _until(b_entered.is_set)
                    outcome["board_a"] = await self.cancel_when(
                        a_entered.is_set,
                        partial(self.call, "read_memory", interrupted_on_a, HANG_GUARD_SECONDS),
                    )
                    await _until(self.board_idle)
                    outcome["board_b_during_a_cleanup"] = self.board_state(OTHER_BOARD)
                finally:
                    b_release.set()

        anyio.run(scenario)

        self.assertEqual(self.join_workers(), [])
        board_b_final = self.board_state(OTHER_BOARD)
        all_calls = board_b_final.pop("backend_calls")
        during_a_cleanup = cast_state(outcome["board_b_during_a_cleanup"])
        during_a_cleanup.pop("backend_calls")
        self.assertEqual(
            {
                "board_a_outcome": outcome["board_a"],
                "board_b_result": outcome["board_b"],
                "board_a": {
                    key: value
                    for key, value in self.board_state().items()
                    if key != "backend_calls"
                },
                "board_b_during_a_cleanup": during_a_cleanup,
                "board_b_final": board_b_final,
                "backend_calls": all_calls,
                "board_a_runtime_closed": owner_a.runtime_session.closed_at is not None,
                "board_b_runtime_closed": owner_b.runtime_session.closed_at is not None,
            },
            {
                "board_a_outcome": {"cancelled": True},
                "board_b_result": "board-b-complete",
                "board_a": {
                    key: value
                    for key, value in self.evicted(EVICTED_BY_INTERRUPTION, []).items()
                    if key != "backend_calls"
                },
                "board_b_during_a_cleanup": {
                    key: value
                    for key, value in self.preserved(owner_b, [], OTHER_BOARD).items()
                    if key != "backend_calls"
                },
                "board_b_final": {
                    key: value
                    for key, value in self.preserved(owner_b, [], OTHER_BOARD).items()
                    if key != "backend_calls"
                },
                # Board B is held while A is interrupted and cleaned, then completes.
                "backend_calls": [
                    ("read_memory", "probe-b"),
                    ("read_memory", "probe-a"),
                    ("finalizer", "read_memory"),
                    ("close", "probe-a"),
                    ("release_reset", "probe-a"),
                    ("finalizer", "read_memory"),
                    ("release_reset", "probe-b"),
                ],
                "board_a_runtime_closed": True,
                "board_b_runtime_closed": False,
            },
        )


def cast_state(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return dict(value)


class BatchChildOwnershipTests(_OwnershipFixture):
    """Batch children own their debug use; the batch reservation owns none."""

    def setUp(self) -> None:
        super().setUp()
        self.hold_entered, self.hold_release = threading.Event(), threading.Event()
        self.addCleanup(self.hold_release.set)
        self.mcp = RegistryFastMCP(
            "vi02-batch", timeout_resolver=lambda name, arguments: HANG_GUARD_SECONDS
        )

        def hold_board(board_id: str) -> str:
            """Use the debug connection, then hold the board until released."""

            self.read_current(board_id)
            self.hold_entered.set()
            if not self.hold_release.wait(HANG_GUARD_SECONDS):
                raise AssertionError("test never released the batch child")
            return "held"

        def read_memory(board_id: str) -> str:
            """Read through the board's current debug connection."""

            return str(self.read_current(board_id))

        for name, handler in (("hold_board", hold_board), ("read_memory", read_memory)):
            self.mcp.add_tool(handler, name=name, structured_output=False)
            self.mcp.configure_layer2(name)
            self.mcp.configure_guarded_dispatch(
                name, guard=self.record_guard, lock_for_board=self.connections.lock_for
            )
        batch_handlers = build_batch_handlers(
            self.mcp.call_tool, tool_exists=self.mcp.registry.is_registered
        )
        for name, handler in batch_handlers.items():
            self.mcp.add_tool(handler, name=name, structured_output=False)
            self.mcp.configure_layer2(name)
        self.mcp.configure_operation_resources(server._bind_managed_board_resources)

    def record_guard(self, name: str, board_id: str, arguments: object) -> None:
        self.guard_calls.append(name)

    async def run_batch(self, outcome: dict[str, object]) -> None:
        result = await self.mcp.call_tool(
            "action_batch",
            {
                "board_id": BOARD,
                "actions": [
                    {"tool_name": "hold_board", "arguments": {"board_id": BOARD}},
                    {"tool_name": "read_memory", "arguments": {"board_id": BOARD}},
                ],
            },
        )
        payload = json.loads(result[0].text.split("\n", 1)[0])
        failure = payload["failure"]
        if failure is not None:
            failure["message_names_transport_loss"] = "transport lost" in failure.pop("message")
        outcome["batch"] = payload

    def test_queued_outsider_cancellation_and_child_transport_failure_stay_owned(self) -> None:
        owner = self.connect("probe-a")
        outcome: dict[str, object] = {}

        async def scenario() -> None:
            async with anyio.create_task_group() as group:
                group.start_soon(self.run_batch, outcome)
                try:
                    await _until(self.hold_entered.is_set)
                    # An outsider queued behind the batch reservation is cancelled.
                    outcome["outsider"] = await self.cancel_when(
                        lambda: self.queued("read_memory"),
                        partial(self.mcp.call_tool, "read_memory", {"board_id": BOARD}),
                    )
                    outcome["while_batch_holds_board"] = self.board_state()
                    outcome["guards_while_batch_holds_board"] = list(self.guard_calls)
                    # The next child then loses the transport it actually uses.
                    self.backend.failures[("read_memory", "probe-a")] = TargetConnectionError(
                        "probe-a transport lost in batch child"
                    )
                finally:
                    self.hold_release.set()

        anyio.run(scenario)

        self.assertEqual(self.join_workers(), [])
        self.assertEqual(
            {
                "outsider": outcome["outsider"],
                "while_batch_holds_board": outcome["while_batch_holds_board"],
                "guards_while_batch_holds_board": outcome["guards_while_batch_holds_board"],
                "guard_calls": self.guard_calls,
                "batch": outcome["batch"],
                **self.board_state(),
                "runtime_closed": owner.runtime_session.closed_at is not None,
            },
            {
                "outsider": {"cancelled": True},
                "while_batch_holds_board": self.preserved(owner, [("read_memory", "probe-a")]),
                # The cancelled outsider never ran its guard; each child ran its own.
                "guards_while_batch_holds_board": ["hold_board"],
                "guard_calls": ["hold_board", "read_memory"],
                "batch": {
                    "status": "batch_failed",
                    "board_id": BOARD,
                    "completed": [{"index": 0, "tool_name": "hold_board", "result": "held"}],
                    "failure": {
                        "index": 1,
                        "tool_name": "read_memory",
                        "error_type": "ToolError",
                        "message_names_transport_loss": True,
                    },
                },
                **self.evicted(
                    EVICTED_BY_TRANSPORT_FAILURE,
                    [
                        ("read_memory", "probe-a"),
                        ("release_reset", "probe-a"),
                        ("read_memory", "probe-a"),
                        ("close", "probe-a"),
                        ("release_reset", "probe-a"),
                    ],
                ),
                "runtime_closed": True,
            },
        )

    def test_async_operation_queued_behind_the_batch_binds_nothing(self) -> None:
        owner = self.connect("probe-a")
        events: list[str] = []
        outcome: dict[str, object] = {}

        def recording_binder(operation: Any) -> None:
            events.append(f"bind:{operation.tool_name}")
            server._bind_managed_board_resources(operation)

        async def async_probe() -> str:
            events.append("handler:async_probe")
            return "async-complete"

        def dispatch_async_probe() -> Awaitable[Any]:
            return dispatch(
                "async_probe",
                BOARD,
                async_probe,
                HANG_GUARD_SECONDS,
                resource_binder=recording_binder,
            )

        async def scenario() -> None:
            async with anyio.create_task_group() as group:
                group.start_soon(self.run_batch, outcome)
                try:
                    await _until(self.hold_entered.is_set)
                    outcome["queued_async"] = await self.cancel_when(
                        lambda: self.queued("async_probe"), dispatch_async_probe
                    )
                    outcome["while_batch_holds_board"] = self.board_state()
                    outcome["events_while_batch_holds_board"] = list(events)
                finally:
                    self.hold_release.set()
            # Positive control: the same uncontended async operation binds once.
            outcome["uncontended_async"] = await dispatch_async_probe()

        anyio.run(scenario)

        self.assertEqual(self.join_workers(), [])
        self.assertEqual(
            {
                "queued_async": outcome["queued_async"],
                "while_batch_holds_board": outcome["while_batch_holds_board"],
                "events_while_batch_holds_board": outcome["events_while_batch_holds_board"],
                "batch_status": cast_state(outcome["batch"])["status"],
                "uncontended_async": outcome["uncontended_async"],
                "events": events,
                **self.board_state(),
                "runtime_closed": owner.runtime_session.closed_at is not None,
            },
            {
                "queued_async": {"cancelled": True},
                "while_batch_holds_board": self.preserved(owner, [("read_memory", "probe-a")]),
                "events_while_batch_holds_board": [],
                "batch_status": "batch_completed",
                "uncontended_async": "async-complete",
                "events": ["bind:async_probe", "handler:async_probe"],
                **self.preserved(
                    owner,
                    [
                        ("read_memory", "probe-a"),
                        ("release_reset", "probe-a"),
                        ("read_memory", "probe-a"),
                        ("release_reset", "probe-a"),
                        ("release_reset", "probe-a"),
                    ],
                ),
                "runtime_closed": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
