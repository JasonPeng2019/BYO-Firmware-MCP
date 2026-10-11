"""CAN service/manager ownership, budget, and cleanup tests."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
import unittest
from collections import deque
from dataclasses import replace
from pathlib import Path
from unittest import mock

from pyocd_debug_mcp.adapters.can_interface import (
    CanBackendCapabilities,
    CanBusHandle,
    CanDirection,
    CanFrame,
    CapabilityStatus,
)
from pyocd_debug_mcp.can_config import load_can_fixture_config
from pyocd_debug_mcp.can_errors import (
    CanCleanupError,
    CanOperationError,
    CanPolicyError,
    CanWorkerError,
)
from pyocd_debug_mcp.can_startup import load_can_startup
from pyocd_debug_mcp.services.can_session import CanSessionManager

ROOT = Path(__file__).resolve().parents[1]
TX_PROFILE = ROOT / "fixtures" / "can" / "virtual_tx_allowed.yaml"


def capabilities() -> CanBackendCapabilities:
    supported = CapabilityStatus.SUPPORTED
    return CanBackendCapabilities(
        bounded_open=supported,
        bounded_send=supported,
        bounded_receive=supported,
        bounded_close=supported,
        pending_tx_quiescence=supported,
        physical=False,
        pending_tx_empty=True,
        driver="virtual",
        device_identity=("virtual",),
        listen_only=False,
        one_shot=True,
        loopback=False,
        bus_state="active",
    )


class FakeWorker:
    def __init__(self, *, close_fails: bool = False) -> None:
        self.close_fails = close_fails
        self.cleanup_uncertain = False
        self.last_escalation_stage = "none"
        self.last_cleanup_outcome = "none"
        self.inspect_calls = 0
        self.open_calls = 0
        self.send_calls = 0
        self.close_calls = 0
        self.cancel_calls = 0
        self.receive_timeouts: list[float] = []
        self.receives: deque[CanFrame | None] = deque()
        self.profile_sha256 = load_can_fixture_config(TX_PROFILE).canonical_sha256

    async def inspect(self, *, timeout_seconds: float) -> CanBackendCapabilities:
        del timeout_seconds
        self.inspect_calls += 1
        return capabilities()

    async def open(self, *, timeout_seconds: float) -> CanBusHandle:
        del timeout_seconds
        self.open_calls += 1
        return CanBusHandle(
            native_handle=object(),
            profile_id="virtual_tx_allowed",
            profile_sha256=self.profile_sha256,
            interface="virtual",
            channel="byo-tx-allowed",
            protocol="classic",
            filters=(),
        )

    async def send(
        self,
        frame: CanFrame,
        *,
        timeout_seconds: float,
        operation_timeout_seconds: float,
        not_after_monotonic: float | None = None,
    ) -> CanFrame:
        del timeout_seconds, operation_timeout_seconds, not_after_monotonic
        self.send_calls += 1
        return CanFrame(
            frame.arbitration_id,
            frame.extended,
            frame.data,
            direction=CanDirection.TX,
            channel="byo-tx-allowed",
        )

    async def receive(
        self, *, timeout_seconds: float, operation_timeout_seconds: float
    ) -> CanFrame | None:
        del operation_timeout_seconds
        self.receive_timeouts.append(timeout_seconds)
        return self.receives.popleft() if self.receives else None

    async def close(self, *, timeout_seconds: float) -> None:
        del timeout_seconds
        self.close_calls += 1
        if self.close_fails:
            self.cleanup_uncertain = True
            self.last_escalation_stage = "cleanup-unknown"
            self.last_cleanup_outcome = "unknown"
            raise RuntimeError("SECRET:/tmp/backend")
        self.last_escalation_stage = "graceful-shutdown"
        self.last_cleanup_outcome = "closed"

    async def cancel(self) -> None:
        self.cancel_calls += 1
        if self.close_fails:
            self.cleanup_uncertain = True
            self.last_escalation_stage = "cleanup-unknown"
            self.last_cleanup_outcome = "unknown"
            raise RuntimeError("SECRET:/tmp/backend")
        self.last_escalation_stage = "cleanup-acknowledged"
        self.last_cleanup_outcome = "closed"


class BlockingWorker(FakeWorker):
    def __init__(self, *, close_fails: bool = False) -> None:
        super().__init__(close_fails=close_fails)
        self.receive_started = threading.Event()

    async def receive(
        self, *, timeout_seconds: float, operation_timeout_seconds: float
    ) -> CanFrame | None:
        del timeout_seconds, operation_timeout_seconds
        self.receive_started.set()
        await asyncio.sleep(3600)
        return None


class PausingOpenWorker(FakeWorker):
    def __init__(self) -> None:
        super().__init__()
        self.open_started = threading.Event()
        self.release_open = threading.Event()

    async def open(self, *, timeout_seconds: float) -> CanBusHandle:
        self.open_started.set()
        await asyncio.to_thread(self.release_open.wait)
        return await super().open(timeout_seconds=timeout_seconds)


class PausingCloseWorker(FakeWorker):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = threading.Event()
        self.release_close = threading.Event()

    async def close(self, *, timeout_seconds: float) -> None:
        self.close_started.set()
        await asyncio.to_thread(self.release_close.wait)
        await super().close(timeout_seconds=timeout_seconds)


class NonClassicalReceiptWorker(FakeWorker):
    async def send(
        self,
        frame: CanFrame,
        *,
        timeout_seconds: float,
        operation_timeout_seconds: float,
        not_after_monotonic: float | None = None,
    ) -> CanFrame:
        del frame, timeout_seconds, operation_timeout_seconds, not_after_monotonic
        self.send_calls += 1
        return CanFrame(
            0x123,
            False,
            b"",
            dlc=2,
            direction=CanDirection.TX,
            is_remote=True,
        )


class WrongDirectionReceiptWorker(FakeWorker):
    async def send(
        self,
        frame: CanFrame,
        *,
        timeout_seconds: float,
        operation_timeout_seconds: float,
        not_after_monotonic: float | None = None,
    ) -> CanFrame:
        del timeout_seconds, operation_timeout_seconds, not_after_monotonic
        self.send_calls += 1
        return CanFrame(
            frame.arbitration_id,
            frame.extended,
            frame.data,
            direction=CanDirection.RX,
        )


class OperationFailureWorker(FakeWorker):
    def __init__(
        self,
        operation: str,
        error: type[CanOperationError],
        *,
        cleanup_outcome: str = "none",
    ) -> None:
        super().__init__()
        self.operation = operation
        self.error = error
        self.cleanup_outcome = cleanup_outcome

    def _raise(self) -> None:
        if self.cleanup_outcome == "closed":
            self.last_escalation_stage = "cleanup-acknowledged"
            self.last_cleanup_outcome = "closed"
        elif self.cleanup_outcome == "unknown":
            self.cleanup_uncertain = True
            self.last_escalation_stage = "cleanup-unknown"
            self.last_cleanup_outcome = "unknown"
        raise self.error("injected operation failure")

    async def send(
        self,
        frame: CanFrame,
        *,
        timeout_seconds: float,
        operation_timeout_seconds: float,
        not_after_monotonic: float | None = None,
    ) -> CanFrame:
        if self.operation == "send":
            self.send_calls += 1
            self._raise()
        return await super().send(
            frame,
            timeout_seconds=timeout_seconds,
            operation_timeout_seconds=operation_timeout_seconds,
            not_after_monotonic=not_after_monotonic,
        )

    async def receive(
        self, *, timeout_seconds: float, operation_timeout_seconds: float
    ) -> CanFrame | None:
        if self.operation == "read":
            self._raise()
        return await super().receive(
            timeout_seconds=timeout_seconds,
            operation_timeout_seconds=operation_timeout_seconds,
        )


class ConnectFailureWorker(FakeWorker):
    def __init__(self, *, cleanup_fails: bool) -> None:
        super().__init__(close_fails=cleanup_fails)

    async def inspect(self, *, timeout_seconds: float) -> CanBackendCapabilities:
        del timeout_seconds
        self.inspect_calls += 1
        raise CanOperationError("injected connect failure")


class CanSessionManagerTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(
        self,
        worker: FakeWorker,
        *,
        board_ids: tuple[str, ...] | None = None,
        max_can_tool_calls: int | None = None,
        max_read_calls: int | None = None,
        max_received_frames: int | None = None,
        max_target_policy_refusals: int | None = None,
        max_tx_attempts: int | None = None,
        max_tx_per_second: int | None = None,
    ) -> CanSessionManager:
        startup = load_can_startup(
            {"PYOCD_CAN_MODE": "available", "PYOCD_CAN_CONFIG": str(TX_PROFILE)}
        )
        fixture = startup.fixture
        assert fixture is not None
        config = fixture.config
        limits = config.process_limits
        limits = replace(
            limits,
            max_can_tool_calls=(
                max_can_tool_calls if max_can_tool_calls is not None else limits.max_can_tool_calls
            ),
            max_read_calls=(
                max_read_calls if max_read_calls is not None else limits.max_read_calls
            ),
            max_received_frames=(
                max_received_frames
                if max_received_frames is not None
                else limits.max_received_frames
            ),
            max_target_policy_refusals=(
                max_target_policy_refusals
                if max_target_policy_refusals is not None
                else limits.max_target_policy_refusals
            ),
        )
        transmit = replace(
            config.transmit,
            max_tx_attempts_per_process=(
                max_tx_attempts
                if max_tx_attempts is not None
                else config.transmit.max_tx_attempts_per_process
            ),
            max_frames_per_second=(
                max_tx_per_second
                if max_tx_per_second is not None
                else config.transmit.max_frames_per_second
            ),
        )
        config = replace(
            config,
            board_ids=board_ids if board_ids is not None else config.board_ids,
            process_limits=limits,
            transmit=transmit,
        )
        startup = replace(startup, fixture=replace(fixture, config=config))
        return CanSessionManager(startup, worker_factory=lambda: worker)

    async def test_connect_is_idempotent_and_cross_board_is_refused(self) -> None:
        worker = FakeWorker()
        manager = self.make_manager(worker)
        try:
            await manager.connect("nucleo_l476rg", None)
            await manager.connect("nucleo_l476rg", None)
            self.assertEqual(worker.open_calls, 1)
            self.assertEqual(worker.inspect_calls, 1)
            self.assertEqual(manager.status("nucleo_l476rg")["phase"], "open")
            with self.assertRaisesRegex(CanPolicyError, "can/board-mismatch"):
                await manager.connect("another_board", None)
        finally:
            manager.shutdown_sync()

    async def test_unconnected_operations_use_not_connected_without_policy_charge(self) -> None:
        manager = self.make_manager(FakeWorker())
        try:
            with self.assertRaisesRegex(CanPolicyError, "can/not-connected"):
                await manager.send(
                    "nucleo_l476rg",
                    0x123,
                    "CAFE",
                    extended=False,
                    timeout_seconds=None,
                )
            with self.assertRaisesRegex(CanPolicyError, "can/not-connected"):
                await manager.read("nucleo_l476rg", read_seconds=0.1, max_frames=1)
            snapshot = manager.budget.snapshot()
            self.assertEqual(snapshot.target_policy_refusals, 0)
            self.assertEqual(snapshot.can_tool_calls, 0)
        finally:
            manager.shutdown_sync()

    async def test_cross_owner_mismatches_are_bounded_target_policy_refusals(self) -> None:
        manager = self.make_manager(
            FakeWorker(),
            board_ids=("nucleo_l476rg", "another_board"),
            max_target_policy_refusals=4,
        )
        try:
            await manager.connect("nucleo_l476rg", None)
            with self.assertRaisesRegex(CanPolicyError, "can/board-mismatch"):
                await manager.connect("another_board", None)
            with self.assertRaisesRegex(CanPolicyError, "can/board-mismatch"):
                await manager.send(
                    "another_board",
                    0x123,
                    "CAFE",
                    extended=False,
                    timeout_seconds=None,
                )
            with self.assertRaisesRegex(CanPolicyError, "can/board-mismatch"):
                await manager.read("another_board", read_seconds=0.1, max_frames=1)
            with self.assertRaisesRegex(CanPolicyError, "can/board-mismatch"):
                await manager.disconnect("another_board")

            snapshot = manager.budget.snapshot()
            self.assertEqual(snapshot.target_policy_refusals, 4)
            self.assertEqual(snapshot.can_tool_calls, 1)
            self.assertEqual(manager.owner_board_id, "nucleo_l476rg")

            with self.assertRaisesRegex(CanPolicyError, "can/process-call-limit"):
                await manager.connect("another_board", None)
            exhausted = manager.budget.snapshot()
            self.assertEqual(exhausted.target_policy_refusals, 4)
            self.assertEqual(exhausted.can_tool_calls, 1)
        finally:
            manager.shutdown_sync()

    async def test_status_publishes_immutable_profile_limits_and_remaining_budget(self) -> None:
        manager = self.make_manager(FakeWorker())
        try:
            status = manager.status("nucleo_l476rg")
            profile = status["profile"]
            self.assertIsInstance(profile, dict)
            assert isinstance(profile, dict)
            self.assertEqual(profile["profile_id"], "virtual_tx_allowed")
            self.assertEqual(profile["receive_ids"], ("0x321/standard",))
            self.assertEqual(profile["transmit_ids"], ("0x123/standard",))
            self.assertEqual(profile["default_read_seconds"], 0.25)
            self.assertEqual(profile["max_read_seconds"], 2.0)
            self.assertEqual(profile["default_max_frames"], 8)
            self.assertEqual(profile["default_send_timeout_seconds"], 0.1)
            self.assertEqual(profile["max_send_timeout_seconds"], 0.5)

            limits = status["budget_limits"]
            remaining = status["budget_remaining"]
            self.assertIsInstance(limits, dict)
            self.assertIsInstance(remaining, dict)
            assert isinstance(limits, dict) and isinstance(remaining, dict)
            self.assertEqual(limits["max_can_tool_calls"], 512)
            self.assertEqual(limits["max_target_policy_refusals"], 128)
            self.assertEqual(limits["max_tx_attempts_per_process"], 100)
            self.assertNotIn("max_health_transactions", limits)
            self.assertNotIn("max_capture_artifacts", limits)
            self.assertEqual(remaining["can_tool_calls"], 512)
            self.assertEqual(remaining["target_policy_refusals"], 128)
            self.assertEqual(remaining["tx_attempts"], 100)
            self.assertEqual(remaining["tx_attempts_in_current_window"], 10)
        finally:
            manager.shutdown_sync()

    async def test_send_and_filtered_read_are_bounded(self) -> None:
        worker = FakeWorker()
        worker.receives.extend(
            [
                CanFrame(0x322, False, b"x", direction=CanDirection.RX),
                CanFrame(0x321, False, b"ok", direction=CanDirection.RX),
                None,
            ]
        )
        manager = self.make_manager(worker)
        try:
            await manager.connect("nucleo_l476rg", None)
            sent = await manager.send(
                "nucleo_l476rg", 0x123, "CA FE", extended=False, timeout_seconds=None
            )
            self.assertEqual(sent.data, bytes.fromhex("CAFE"))
            captured = await manager.read("nucleo_l476rg", read_seconds=0.25, max_frames=8)
            self.assertEqual(captured.observed_frames, 2)
            self.assertEqual([frame.arbitration_id for frame in captured.frames], [0x321])
            snapshot = manager.budget.snapshot()
            self.assertEqual(snapshot.can_tool_calls, 3)
            self.assertEqual(snapshot.read_calls, 1)
            self.assertEqual(snapshot.received_frames, 2)
            self.assertEqual(snapshot.tx_attempts, 1)
        finally:
            manager.shutdown_sync()

    async def test_frame_cap_reports_a_truncated_capture(self) -> None:
        worker = FakeWorker()
        worker.receives.extend(
            [
                CanFrame(0x321, False, b"one", direction=CanDirection.RX),
                CanFrame(0x321, False, b"two", direction=CanDirection.RX),
            ]
        )
        manager = self.make_manager(worker)
        try:
            await manager.connect("nucleo_l476rg", None)
            captured = await manager.read("nucleo_l476rg", read_seconds=0.25, max_frames=1)
            self.assertEqual(captured.stop_reason, "frame_cap")
            self.assertTrue(captured.truncated)
            self.assertEqual(captured.observed_frames, 1)
            self.assertEqual(len(captured.frames), 1)
            self.assertEqual(len(worker.receives), 1)
        finally:
            manager.shutdown_sync()

    async def test_read_does_not_expose_non_classical_data_frames(self) -> None:
        worker = FakeWorker()
        worker.receives.extend(
            [
                CanFrame(
                    0x321,
                    False,
                    b"",
                    direction=CanDirection.RX,
                    is_error=True,
                ),
                CanFrame(
                    0x321,
                    False,
                    b"",
                    dlc=1,
                    direction=CanDirection.RX,
                    is_remote=True,
                ),
                CanFrame(
                    0x321,
                    False,
                    b"x" * 9,
                    direction=CanDirection.RX,
                    is_fd=True,
                ),
                CanFrame(0x321, False, b"ok", direction=CanDirection.RX),
                None,
            ]
        )
        manager = self.make_manager(worker)
        try:
            await manager.connect("nucleo_l476rg", None)
            captured = await manager.read("nucleo_l476rg", read_seconds=0.25, max_frames=8)
            self.assertEqual(captured.observed_frames, 4)
            self.assertEqual(len(captured.frames), 1)
            frame = captured.frames[0]
            self.assertEqual(frame.data, b"ok")
            self.assertFalse(frame.is_error)
            self.assertFalse(frame.is_remote)
            self.assertFalse(frame.is_fd)
        finally:
            manager.shutdown_sync()

    async def test_non_classical_send_receipt_is_rejected_and_retired(self) -> None:
        worker = NonClassicalReceiptWorker()
        manager = self.make_manager(worker)
        try:
            await manager.connect("nucleo_l476rg", None)
            with self.assertRaises(CanWorkerError):
                await manager.send(
                    "nucleo_l476rg", 0x123, "CAFE", extended=False, timeout_seconds=None
                )
            self.assertEqual(worker.cancel_calls, 1)
            self.assertEqual(manager.phase, "closed")
            self.assertIsNone(manager.owner_board_id)
        finally:
            manager.shutdown_sync()

    async def test_rx_direction_send_receipt_is_rejected_and_retired(self) -> None:
        worker = WrongDirectionReceiptWorker()
        manager = self.make_manager(worker)
        try:
            await manager.connect("nucleo_l476rg", None)
            with self.assertRaises(CanWorkerError):
                await manager.send(
                    "nucleo_l476rg", 0x123, "CAFE", extended=False, timeout_seconds=None
                )
            self.assertEqual(worker.cancel_calls, 1)
            self.assertEqual(manager.phase, "closed")
            self.assertIsNone(manager.owner_board_id)
        finally:
            manager.shutdown_sync()

    async def test_cleanup_failure_retains_owner_and_redacts_error(self) -> None:
        worker = FakeWorker(close_fails=True)
        manager = self.make_manager(worker)
        await manager.connect("nucleo_l476rg", None)
        with self.assertRaises(CanCleanupError):
            await manager.disconnect("nucleo_l476rg")
        status = manager.status("nucleo_l476rg")
        self.assertEqual(status["phase"], "uncertain")
        self.assertEqual(status["owner_board_id"], "nucleo_l476rg")
        rendered = str(status)
        self.assertNotIn("SECRET", rendered)
        self.assertNotIn("/tmp/backend", rendered)
        self.assertIn("can/cleanup-failed", rendered)
        with self.assertRaises(CanCleanupError):
            await manager.disconnect("nucleo_l476rg")
        with self.assertRaises(CanCleanupError):
            await manager.disconnect("nucleo_l476rg")
        self.assertEqual(worker.close_calls, 3)
        exhausted = manager.status("nucleo_l476rg")["cleanup"]
        self.assertIsInstance(exhausted, dict)
        assert isinstance(exhausted, dict)
        self.assertEqual(exhausted["attempt_count"], 3)
        with self.assertRaises(CanCleanupError):
            await manager.disconnect("nucleo_l476rg")
        self.assertEqual(worker.close_calls, 3)
        self.assertFalse(manager.shutdown_sync())
        self.assertEqual(worker.close_calls, 3)

    async def test_initial_connect_failure_distinguishes_clean_and_uncertain_cleanup(
        self,
    ) -> None:
        clean_worker = ConnectFailureWorker(cleanup_fails=False)
        clean_manager = self.make_manager(clean_worker)
        try:
            with self.assertRaises(CanOperationError):
                await clean_manager.connect("nucleo_l476rg", None)
            self.assertEqual(clean_worker.cancel_calls, 1)
            self.assertEqual(clean_manager.phase, "closed")
            self.assertIsNone(clean_manager.owner_board_id)
        finally:
            clean_manager.shutdown_sync()

        uncertain_worker = ConnectFailureWorker(cleanup_fails=True)
        uncertain_manager = self.make_manager(uncertain_worker)
        with self.assertRaises(CanCleanupError):
            await uncertain_manager.connect("nucleo_l476rg", None)
        self.assertEqual(uncertain_worker.cancel_calls, 1)
        self.assertEqual(uncertain_manager.phase, "uncertain")
        self.assertEqual(uncertain_manager.owner_board_id, "nucleo_l476rg")
        self.assertFalse(uncertain_manager.shutdown_sync())

    async def test_terminal_failures_reconcile_clean_or_uncertain_for_read_and_send(
        self,
    ) -> None:
        for operation in ("read", "send"):
            for cleanup_outcome, expected_phase in (
                ("closed", "closed"),
                ("unknown", "uncertain"),
            ):
                with self.subTest(operation=operation, cleanup=cleanup_outcome):
                    worker = OperationFailureWorker(
                        operation,
                        CanWorkerError,
                        cleanup_outcome=cleanup_outcome,
                    )
                    manager = self.make_manager(worker)
                    await manager.connect("nucleo_l476rg", None)
                    with self.assertRaises(CanWorkerError):
                        if operation == "read":
                            await manager.read("nucleo_l476rg", read_seconds=0.1, max_frames=1)
                        else:
                            await manager.send(
                                "nucleo_l476rg",
                                0x123,
                                "CAFE",
                                extended=False,
                                timeout_seconds=None,
                            )
                    self.assertEqual(manager.phase, expected_phase)
                    if cleanup_outcome == "closed":
                        self.assertIsNone(manager.owner_board_id)
                        self.assertTrue(manager.shutdown_sync())
                    else:
                        self.assertEqual(manager.owner_board_id, "nucleo_l476rg")
                        self.assertFalse(manager.shutdown_sync())

    async def test_nonterminal_failures_keep_live_generation_for_read_and_send(self) -> None:
        for operation in ("read", "send"):
            with self.subTest(operation=operation):
                worker = OperationFailureWorker(operation, CanOperationError)
                manager = self.make_manager(worker)
                try:
                    await manager.connect("nucleo_l476rg", None)
                    with self.assertRaises(CanOperationError):
                        if operation == "read":
                            await manager.read("nucleo_l476rg", read_seconds=0.1, max_frames=1)
                        else:
                            await manager.send(
                                "nucleo_l476rg",
                                0x123,
                                "CAFE",
                                extended=False,
                                timeout_seconds=None,
                            )
                    self.assertEqual(manager.phase, "open")
                    self.assertEqual(manager.owner_board_id, "nucleo_l476rg")
                    observed = manager.status("nucleo_l476rg")["capabilities"]
                    self.assertIsInstance(observed, dict)
                    assert isinstance(observed, dict)
                    self.assertEqual(observed["driver"], "virtual")
                finally:
                    manager.shutdown_sync()

    async def test_read_cancellation_proves_cleanup_before_clearing_owner(self) -> None:
        worker = BlockingWorker()
        manager = self.make_manager(worker)
        try:
            await manager.connect("nucleo_l476rg", None)
            task = asyncio.create_task(
                manager.read("nucleo_l476rg", read_seconds=1.0, max_frames=1)
            )
            await asyncio.to_thread(worker.receive_started.wait)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(worker.cancel_calls, 1)
            self.assertEqual(manager.status("nucleo_l476rg")["phase"], "closed")
            self.assertIsNone(manager.owner_board_id)
        finally:
            manager.shutdown_sync()

    async def test_read_cancellation_retains_uncertain_owner_on_cleanup_failure(self) -> None:
        worker = BlockingWorker(close_fails=True)
        manager = self.make_manager(worker)
        await manager.connect("nucleo_l476rg", None)
        task = asyncio.create_task(manager.read("nucleo_l476rg", read_seconds=1.0, max_frames=1))
        await asyncio.to_thread(worker.receive_started.wait)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        status = manager.status("nucleo_l476rg")
        self.assertEqual(status["phase"], "uncertain")
        self.assertEqual(status["owner_board_id"], "nucleo_l476rg")
        self.assertFalse(manager.shutdown_sync())

    async def test_startup_read_override_is_the_effective_default(self) -> None:
        worker = FakeWorker()
        startup = load_can_startup(
            {
                "PYOCD_CAN_MODE": "available",
                "PYOCD_CAN_CONFIG": str(TX_PROFILE),
                "PYOCD_CAN_READ_SECONDS": "0.4",
            }
        )
        manager = CanSessionManager(startup, worker_factory=lambda: worker)
        try:
            await manager.connect("nucleo_l476rg", None)
            await manager.read("nucleo_l476rg", read_seconds=None, max_frames=1)
            self.assertTrue(worker.receive_timeouts)
            self.assertAlmostEqual(worker.receive_timeouts[0], 0.4, places=2)
        finally:
            manager.shutdown_sync()

    async def test_process_budget_persists_across_proven_rebind(self) -> None:
        workers: list[FakeWorker] = []

        def make_worker() -> FakeWorker:
            worker = FakeWorker()
            workers.append(worker)
            return worker

        startup = load_can_startup(
            {"PYOCD_CAN_MODE": "available", "PYOCD_CAN_CONFIG": str(TX_PROFILE)}
        )
        manager = CanSessionManager(startup, worker_factory=make_worker)
        try:
            await manager.connect("nucleo_l476rg", None)
            await manager.disconnect("nucleo_l476rg")
            await manager.connect("nucleo_l476rg", None)
            self.assertEqual(len(workers), 2)
            self.assertEqual(manager.budget.snapshot().can_tool_calls, 2)
        finally:
            manager.shutdown_sync()

    async def test_process_budget_persists_across_board_rebind_and_exhaustion(self) -> None:
        workers: list[FakeWorker] = []

        def make_worker() -> FakeWorker:
            worker = FakeWorker()
            workers.append(worker)
            return worker

        startup = load_can_startup(
            {"PYOCD_CAN_MODE": "available", "PYOCD_CAN_CONFIG": str(TX_PROFILE)}
        )
        fixture = startup.fixture
        assert fixture is not None
        config = fixture.config
        config = replace(
            config,
            board_ids=("nucleo_l476rg", "another_board"),
            transmit=replace(config.transmit, max_tx_attempts_per_process=2),
            process_limits=replace(
                config.process_limits,
                max_can_tool_calls=2,
                max_read_calls=2,
            ),
        )
        startup = replace(startup, fixture=replace(fixture, config=config))
        manager = CanSessionManager(startup, worker_factory=make_worker)
        budget_id = manager.budget.snapshot().budget_id
        try:
            await manager.connect("nucleo_l476rg", None)
            await manager.disconnect("nucleo_l476rg")
            await manager.connect("another_board", None)
            with self.assertRaisesRegex(CanPolicyError, "can/process-call-limit"):
                await manager.connect("another_board", None)
            self.assertEqual(manager.phase, "open")
            self.assertEqual(manager.owner_board_id, "another_board")
            await manager.disconnect("another_board")
            with self.assertRaisesRegex(CanPolicyError, "can/process-call-limit"):
                await manager.connect("nucleo_l476rg", None)
            self.assertEqual(manager.phase, "closed")
            self.assertIsNone(manager.owner_board_id)
            snapshot = manager.budget.snapshot()
            self.assertEqual(snapshot.budget_id, budget_id)
            self.assertEqual(snapshot.can_tool_calls, 2)
            self.assertEqual(len(workers), 2)
        finally:
            manager.shutdown_sync()

    async def test_receive_quota_stops_without_overcount_and_remains_exhausted(self) -> None:
        worker = FakeWorker()
        worker.receives.extend(
            [
                CanFrame(0x321, False, b"one", direction=CanDirection.RX),
                CanFrame(0x321, False, b"two", direction=CanDirection.RX),
            ]
        )
        manager = self.make_manager(
            worker,
            max_can_tool_calls=3,
            max_read_calls=2,
            max_received_frames=1,
        )
        try:
            await manager.connect("nucleo_l476rg", None)
            first = await manager.read("nucleo_l476rg", read_seconds=0.1, max_frames=8)
            self.assertEqual(first.observed_frames, 1)
            self.assertEqual(len(first.frames), 1)
            self.assertEqual(first.stop_reason, "can/process-rx-limit")
            self.assertTrue(first.truncated)
            second = await manager.read("nucleo_l476rg", read_seconds=0.1, max_frames=8)
            self.assertEqual(second.observed_frames, 0)
            self.assertEqual(second.stop_reason, "can/process-rx-limit")
            snapshot = manager.budget.snapshot()
            self.assertEqual(snapshot.can_tool_calls, 3)
            self.assertEqual(snapshot.read_calls, 2)
            self.assertEqual(snapshot.received_frames, 1)
        finally:
            manager.shutdown_sync()

    async def test_transmit_process_and_rate_limits_refuse_without_tx_increment(self) -> None:
        process_worker = FakeWorker()
        process_manager = self.make_manager(
            process_worker,
            max_can_tool_calls=4,
            max_tx_attempts=1,
        )
        try:
            await process_manager.connect("nucleo_l476rg", None)
            await process_manager.send(
                "nucleo_l476rg",
                0x123,
                "CAFE",
                extended=False,
                timeout_seconds=None,
            )
            with self.assertRaisesRegex(CanPolicyError, "can/process-call-limit"):
                await process_manager.send(
                    "nucleo_l476rg",
                    0x123,
                    "CAFE",
                    extended=False,
                    timeout_seconds=None,
                )
            process_snapshot = process_manager.budget.snapshot()
            self.assertEqual(process_snapshot.can_tool_calls, 3)
            self.assertEqual(process_snapshot.tx_attempts, 1)
            self.assertEqual(process_worker.send_calls, 1)
            self.assertEqual(process_manager.phase, "open")
        finally:
            process_manager.shutdown_sync()

        rate_worker = FakeWorker()
        rate_manager = self.make_manager(
            rate_worker,
            max_can_tool_calls=4,
            max_tx_attempts=4,
            max_tx_per_second=1,
        )
        try:
            await rate_manager.connect("nucleo_l476rg", None)
            await rate_manager.send(
                "nucleo_l476rg",
                0x123,
                "CAFE",
                extended=False,
                timeout_seconds=None,
            )
            with self.assertRaisesRegex(CanPolicyError, "can/rate-limited"):
                await rate_manager.send(
                    "nucleo_l476rg",
                    0x123,
                    "CAFE",
                    extended=False,
                    timeout_seconds=None,
                )
            rate_snapshot = rate_manager.budget.snapshot()
            self.assertEqual(rate_snapshot.can_tool_calls, 3)
            self.assertEqual(rate_snapshot.tx_attempts, 1)
            self.assertEqual(rate_worker.send_calls, 1)
            self.assertEqual(rate_manager.phase, "open")
        finally:
            rate_manager.shutdown_sync()

    async def test_target_policy_refusal_limit_is_enforced_and_survives_reconnect(self) -> None:
        worker = FakeWorker()
        manager = self.make_manager(worker, max_target_policy_refusals=2)
        try:
            await manager.connect("nucleo_l476rg", None)
            for identifier in (0x124, 0x125):
                with self.assertRaisesRegex(CanPolicyError, "can/id-forbidden"):
                    await manager.send(
                        "nucleo_l476rg",
                        identifier,
                        "CAFE",
                        extended=False,
                        timeout_seconds=None,
                    )
            first = manager.budget.snapshot()
            self.assertEqual(first.target_policy_refusals, 2)
            self.assertEqual(first.can_tool_calls, 1)

            await manager.disconnect("nucleo_l476rg")
            await manager.connect("nucleo_l476rg", None)
            with self.assertRaisesRegex(CanPolicyError, "can/process-call-limit"):
                await manager.send(
                    "nucleo_l476rg",
                    0x126,
                    "CAFE",
                    extended=False,
                    timeout_seconds=None,
                )

            status = manager.status("nucleo_l476rg")
            self.assertEqual(status["budget"]["target_policy_refusals"], 2)
            self.assertEqual(status["budget_remaining"]["target_policy_refusals"], 0)
            self.assertEqual(worker.send_calls, 0)
        finally:
            manager.shutdown_sync()

    async def test_pre_admission_limit_preserves_open_generation(self) -> None:
        worker = FakeWorker()
        manager = self.make_manager(worker)
        try:
            await manager.connect("nucleo_l476rg", None)
            limits = manager.budget._limits
            assert limits is not None
            with manager.budget._lock:
                manager.budget._can_tool_calls = limits.max_can_tool_calls
            with self.assertRaisesRegex(CanPolicyError, "can/process-call-limit"):
                await manager.connect("nucleo_l476rg", None)
            status = manager.status("nucleo_l476rg")
            self.assertEqual(status["phase"], "open")
            self.assertEqual(status["owner_board_id"], "nucleo_l476rg")
            self.assertEqual(worker.open_calls, 1)
        finally:
            manager.shutdown_sync()

    async def test_committed_connect_wins_caller_cancellation_race(self) -> None:
        worker = FakeWorker()
        manager = self.make_manager(worker)
        try:
            task = asyncio.create_task(manager.connect("nucleo_l476rg", None))
            await asyncio.sleep(0)
            # Block this event loop until the manager thread commits the open,
            # leaving only result delivery pending here.
            deadline = time.monotonic() + 2.0
            while manager.phase != "open" and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(manager.phase, "open")
            time.sleep(0.01)
            task.cancel()
            result = await task
            self.assertEqual(result.driver, "virtual")
            self.assertEqual(manager.phase, "open")
            self.assertEqual(worker.cancel_calls, 0)
        finally:
            manager.shutdown_sync()

    async def test_committed_connect_tail_wins_destination_cancellation(self) -> None:
        worker = FakeWorker()
        manager = self.make_manager(worker)
        committed = threading.Event()
        release_tail = threading.Event()
        original_set_state = manager._set_state

        def pausing_set_state(owner: str | None, phase: str) -> None:
            original_set_state(owner, phase)
            if phase == "open":
                committed.set()
                release_tail.wait(timeout=2.0)

        try:
            with mock.patch.object(manager, "_set_state", side_effect=pausing_set_state):
                task = asyncio.create_task(manager.connect("nucleo_l476rg", None))
                self.assertTrue(await asyncio.to_thread(committed.wait, 2.0))
                task.cancel()
                await asyncio.sleep(0)
                release_tail.set()
                result = await task
            self.assertEqual(result.driver, "virtual")
            self.assertEqual(manager.phase, "open")
            self.assertEqual(worker.cancel_calls, 0)
        finally:
            release_tail.set()
            manager.shutdown_sync()

    async def test_opening_and_closing_do_not_admit_bus_operations(self) -> None:
        opening_worker = PausingOpenWorker()
        opening_manager = self.make_manager(opening_worker)
        connect_task = asyncio.create_task(opening_manager.connect("nucleo_l476rg", None))
        try:
            self.assertTrue(await asyncio.to_thread(opening_worker.open_started.wait, 2.0))
            calls_before = opening_manager.budget.snapshot().can_tool_calls
            with self.assertRaisesRegex(CanPolicyError, "can/not-connected"):
                await opening_manager.connect("nucleo_l476rg", None)
            with self.assertRaisesRegex(CanPolicyError, "can/not-connected"):
                await opening_manager.send(
                    "nucleo_l476rg", 0x123, "CAFE", extended=False, timeout_seconds=None
                )
            self.assertEqual(opening_manager.budget.snapshot().can_tool_calls, calls_before)
            opening_worker.release_open.set()
            await connect_task
        finally:
            opening_worker.release_open.set()
            if not connect_task.done():
                connect_task.cancel()
            opening_manager.shutdown_sync()

        closing_worker = PausingCloseWorker()
        closing_manager = self.make_manager(closing_worker)
        await closing_manager.connect("nucleo_l476rg", None)
        disconnect_task = asyncio.create_task(closing_manager.disconnect("nucleo_l476rg"))
        try:
            self.assertTrue(await asyncio.to_thread(closing_worker.close_started.wait, 2.0))
            calls_before = closing_manager.budget.snapshot().can_tool_calls
            with self.assertRaises(CanCleanupError):
                await closing_manager.read("nucleo_l476rg", read_seconds=0.1, max_frames=1)
            self.assertEqual(closing_manager.budget.snapshot().can_tool_calls, calls_before)
            closing_worker.release_close.set()
            await disconnect_task
        finally:
            closing_worker.release_close.set()
            if not disconnect_task.done():
                disconnect_task.cancel()
            closing_manager.shutdown_sync()

    async def test_cancelled_disconnect_publishes_proven_closed_state(self) -> None:
        worker = PausingCloseWorker()
        manager = self.make_manager(worker)
        await manager.connect("nucleo_l476rg", None)
        disconnect_task = asyncio.create_task(manager.disconnect("nucleo_l476rg"))
        try:
            self.assertTrue(await asyncio.to_thread(worker.close_started.wait, 2.0))
            disconnect_task.cancel()
            worker.release_close.set()
            with self.assertRaises(asyncio.CancelledError):
                await disconnect_task
            self.assertFalse(manager._service.cleanup_needed if manager._service else True)
            self.assertEqual(manager.phase, "closed")
            self.assertIsNone(manager.owner_board_id)
        finally:
            worker.release_close.set()
            if not disconnect_task.done():
                disconnect_task.cancel()
            manager.shutdown_sync()

    async def test_shutdown_cancels_and_retires_active_submission(self) -> None:
        worker = BlockingWorker()
        manager = self.make_manager(worker)
        await manager.connect("nucleo_l476rg", None)
        task = asyncio.create_task(manager.read("nucleo_l476rg", read_seconds=1.0, max_frames=1))
        self.assertTrue(await asyncio.to_thread(worker.receive_started.wait, 2.0))
        self.assertTrue(await asyncio.to_thread(manager.shutdown_sync))
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(manager.phase, "closed")
        self.assertFalse(manager._active_submissions)
        self.assertIsNone(manager._loop_thread)


class CanOffManagerTests(unittest.TestCase):
    def test_off_status_starts_no_thread(self) -> None:
        manager = CanSessionManager(load_can_startup({}))
        status = manager.status("board")
        self.assertFalse(status["configured"])
        self.assertEqual(status["phase"], "closed")
        self.assertIsNone(status["profile"])
        self.assertIsNone(status["budget_limits"])
        self.assertIsNone(status["budget_remaining"])
        self.assertIsNone(manager._loop_thread)
        self.assertTrue(manager.shutdown_sync())

    def test_loop_start_is_singleton_and_shutdown_is_idempotent(self) -> None:
        manager = CanSessionManager(load_can_startup({}))
        original_new_event_loop = asyncio.new_event_loop
        factory_entered = threading.Event()
        release_factory = threading.Event()
        factory_calls = 0
        factory_lock = threading.Lock()
        loops: list[asyncio.AbstractEventLoop] = []
        errors: list[BaseException] = []

        def delayed_new_event_loop() -> asyncio.AbstractEventLoop:
            nonlocal factory_calls
            with factory_lock:
                factory_calls += 1
                call_number = factory_calls
            if call_number == 1:
                factory_entered.set()
                release_factory.wait(timeout=2.0)
            return original_new_event_loop()

        def ensure_loop() -> None:
            try:
                loops.append(manager._ensure_loop())
            except BaseException as exc:  # pragma: no cover - diagnostic capture
                errors.append(exc)

        try:
            with mock.patch("asyncio.new_event_loop", side_effect=delayed_new_event_loop):
                first = threading.Thread(target=ensure_loop)
                second = threading.Thread(target=ensure_loop)
                first.start()
                self.assertTrue(factory_entered.wait(timeout=2.0))
                second.start()
                time.sleep(0.02)
                release_factory.set()
                first.join(timeout=2.0)
                second.join(timeout=2.0)
            self.assertFalse(errors)
            self.assertEqual(factory_calls, 1)
            self.assertEqual(len(loops), 2)
            self.assertIs(loops[0], loops[1])
            self.assertTrue(manager.shutdown_sync())
            self.assertTrue(manager.shutdown_sync())
        finally:
            release_factory.set()
            manager.shutdown_sync()

    def test_shutdown_during_loop_start_cannot_leave_live_loop(self) -> None:
        manager = CanSessionManager(load_can_startup({}))
        original_new_event_loop = asyncio.new_event_loop
        factory_entered = threading.Event()
        release_factory = threading.Event()
        ensure_errors: list[BaseException] = []
        shutdown_results: list[bool] = []

        def delayed_new_event_loop() -> asyncio.AbstractEventLoop:
            factory_entered.set()
            release_factory.wait(timeout=2.0)
            return original_new_event_loop()

        def ensure_loop() -> None:
            try:
                manager._ensure_loop()
            except BaseException as exc:
                ensure_errors.append(exc)

        try:
            with mock.patch("asyncio.new_event_loop", side_effect=delayed_new_event_loop):
                starter = threading.Thread(target=ensure_loop)
                stopper = threading.Thread(
                    target=lambda: shutdown_results.append(manager.shutdown_sync())
                )
                starter.start()
                self.assertTrue(factory_entered.wait(timeout=2.0))
                stopper.start()
                time.sleep(0.02)
                release_factory.set()
                starter.join(timeout=2.0)
                stopper.join(timeout=2.0)
            self.assertFalse(starter.is_alive())
            self.assertFalse(stopper.is_alive())
            self.assertEqual(shutdown_results, [True])
            self.assertEqual(len(ensure_errors), 1)
            self.assertIsInstance(ensure_errors[0], RuntimeError)
            self.assertIsNone(manager._loop)
            self.assertIsNone(manager._loop_thread)
        finally:
            release_factory.set()
            manager.shutdown_sync()

    def test_schedule_race_with_shutdown_is_rejected_without_orphan(self) -> None:
        manager = CanSessionManager(load_can_startup({}))
        manager._ensure_loop()
        original_ensure_loop = manager._ensure_loop
        schedule_paused = threading.Event()
        release_schedule = threading.Event()
        schedule_errors: list[BaseException] = []
        shutdown_results: list[bool] = []

        def pausing_ensure_loop() -> asyncio.AbstractEventLoop:
            loop = original_ensure_loop()
            schedule_paused.set()
            release_schedule.wait(timeout=2.0)
            return loop

        def schedule() -> None:
            try:
                manager._schedule(lambda: asyncio.sleep(0))
            except BaseException as exc:
                schedule_errors.append(exc)

        try:
            with mock.patch.object(manager, "_ensure_loop", side_effect=pausing_ensure_loop):
                scheduler = threading.Thread(target=schedule)
                stopper = threading.Thread(
                    target=lambda: shutdown_results.append(manager.shutdown_sync())
                )
                scheduler.start()
                self.assertTrue(schedule_paused.wait(timeout=2.0))
                stopper.start()
                stopper.join(timeout=2.0)
                release_schedule.set()
                scheduler.join(timeout=2.0)
            self.assertFalse(stopper.is_alive())
            self.assertFalse(scheduler.is_alive())
            self.assertEqual(shutdown_results, [True])
            self.assertEqual(len(schedule_errors), 1)
            self.assertIsInstance(schedule_errors[0], RuntimeError)
            self.assertFalse(manager._active_submissions)
        finally:
            release_schedule.set()
            manager.shutdown_sync()

        # A queued admission must not hold the manager state lock while it
        # waits for its runner to enter. An earlier loop operation is allowed
        # to publish state before that runner starts.
        manager = CanSessionManager(load_can_startup({}))
        manager._ensure_loop()
        release_first = threading.Event()
        first_attempting_state = threading.Event()
        second_result: list[tuple[object, ...]] = []

        async def first_operation() -> None:
            await asyncio.to_thread(release_first.wait)
            first_attempting_state.set()
            manager._set_state(None, "closed")

        first_future, first_completed, _suppress, _outcome = manager._schedule(first_operation)
        original_submit = asyncio.run_coroutine_threadsafe

        def delayed_submit(coroutine, loop):  # type: ignore[no-untyped-def]
            release_first.set()
            self.assertTrue(first_attempting_state.wait(timeout=2.0))
            return original_submit(coroutine, loop)

        def submit_second() -> None:
            second_result.append(manager._schedule(lambda: asyncio.sleep(0)))

        try:
            with mock.patch("asyncio.run_coroutine_threadsafe", side_effect=delayed_submit):
                second = threading.Thread(target=submit_second, daemon=True)
                second.start()
                second.join(timeout=2.0)
            self.assertFalse(second.is_alive(), "CAN admission deadlocked the manager state lock")
            self.assertEqual(len(second_result), 1)
            first_future.result(timeout=2.0)
            self.assertTrue(first_completed.wait(timeout=2.0))
            second_future = second_result[0][0]
            assert isinstance(second_future, concurrent.futures.Future)
            second_future.result(timeout=2.0)
        finally:
            release_first.set()
            if not second.is_alive():
                manager.shutdown_sync()

    def test_shutdown_from_manager_loop_is_rejected_without_deadlock(self) -> None:
        manager = CanSessionManager(load_can_startup({}))

        async def shutdown_from_loop() -> None:
            manager.shutdown_sync()

        try:
            future, completed, _suppress, _outcome = manager._schedule(shutdown_from_loop)
            with self.assertRaisesRegex(RuntimeError, "own loop thread"):
                future.result(timeout=2.0)
            self.assertTrue(completed.wait(timeout=2.0))
            self.assertFalse(manager._shutdown_started)
            self.assertFalse(manager._active_submissions)
        finally:
            manager.shutdown_sync()


if __name__ == "__main__":
    unittest.main()
