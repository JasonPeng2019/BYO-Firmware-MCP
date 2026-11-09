"""Shared operation fixture support fixtures; no collected cases."""

from __future__ import annotations
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
from pyocd_debug_mcp.kernel.operations import OperationState, dispatch, operation_manager
from pyocd_debug_mcp.services import target_control
from pyocd_debug_mcp.services.connections import (
    ConnectionManager,
    ManagedConnection,
    stable_connection_identity,
)
from pyocd_debug_mcp.services.session_runtime import InMemorySessionStore
from pyocd_debug_mcp.tools.registers import validate_guarded_register_call


HANG_GUARD_SECONDS = 10.0


BOARD = "vi02_board_a"


OTHER_BOARD = "vi02_board_b"


SUPPORTED_REGISTERS = tuple(f"r{index}" for index in range(13))


async def _until(predicate: Callable[[], bool]) -> None:
    with anyio.fail_after(HANG_GUARD_SECONDS):
        while not predicate():
            await anyio.sleep(0.005)


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
