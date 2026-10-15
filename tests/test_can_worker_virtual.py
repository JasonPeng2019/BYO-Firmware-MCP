"""Software proof rung through the real spawned python-can worker."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import can

from pyocd_debug_mcp.adapters.can_interface import CanBusHandle, CanDirection, CanFrame
from pyocd_debug_mcp.adapters.can_pythoncan import PythonCanAdapter
from pyocd_debug_mcp.can_config import LoadedCanFixtureConfig, load_can_fixture_config
from pyocd_debug_mcp.can_errors import CanCleanupError, CanOperationError
from pyocd_debug_mcp.can_startup import load_can_startup
from pyocd_debug_mcp.services.can_session import CanSessionManager
from pyocd_debug_mcp.services.can_worker import (
    AsyncCanWorkerClient,
    CanWorkerClient,
    _Connection,
    _WorkerControlChannels,
    _run_worker,
)

ROOT = Path(__file__).resolve().parents[1]
TX_PROFILE = ROOT / "fixtures" / "can" / "virtual_tx_allowed.yaml"


class _FailingCloseVirtualAdapter(PythonCanAdapter):
    """Real virtual adapter whose native close succeeds but receipt is negative."""

    def close(self, handle: CanBusHandle) -> None:
        super().close(handle)
        raise RuntimeError("injected cleanup receipt failure")


class _BlockedCloseVirtualAdapter(PythonCanAdapter):
    def close(self, handle: CanBusHandle) -> None:
        while True:
            time.sleep(0.05)


def _run_with_cleanup_signals(*args, adapter_factory) -> None:
    original_join = threading.Thread.join

    def signalled_join(thread, timeout=None):
        if os.name == "posix" and thread.name == "byo-can-parent-watchdog":
            # Reproduce the late watchdog/parent TERM at the finalizer boundary.
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGTERM)
        return original_join(thread, timeout)

    with mock.patch.object(threading.Thread, "join", signalled_join):
        _run_worker(*args, adapter_factory)


def _signalled_cleanup_worker(*args) -> None:
    _run_with_cleanup_signals(*args, adapter_factory=PythonCanAdapter)


def _blocked_cleanup_worker(*args) -> None:
    _run_worker(*args, _BlockedCloseVirtualAdapter)


class _PeerCheckingVirtualAdapter(PythonCanAdapter):
    """Child-local peer that independently proves production adapter traffic.

    python-can's virtual interface is process-local. Because the production
    adapter correctly lives in a spawned worker, a peer in the unittest parent
    would not share its channel. This adapter creates the independent peer in
    the worker and makes a send succeed only after that peer observed the exact
    frame; its first receive call injects one exact allowlisted frame.
    """

    def __init__(self) -> None:
        super().__init__()
        self._peers: dict[int, can.BusABC] = {}
        self._injected: set[int] = set()

    def open(self, config: LoadedCanFixtureConfig, timeout_seconds: float) -> CanBusHandle:
        handle = super().open(config, timeout_seconds)
        self._peers[id(handle)] = can.Bus(
            interface="virtual",
            channel=config.config.adapter.channel,
            ignore_config=True,
            receive_own_messages=False,
            fd=False,
        )
        return handle

    def send(
        self,
        handle: CanBusHandle,
        frame: CanFrame,
        timeout_seconds: float,
        *,
        not_after_monotonic: float | None = None,
    ) -> CanFrame:
        receipt = super().send(
            handle,
            frame,
            timeout_seconds,
            not_after_monotonic=not_after_monotonic,
        )
        observed = self._peers[id(handle)].recv(timeout=timeout_seconds)
        if (
            observed is None
            or observed.arbitration_id != frame.arbitration_id
            or observed.is_extended_id != frame.extended
            or bytes(observed.data) != frame.data
        ):
            raise CanOperationError("child-hosted virtual peer did not observe the exact frame")
        return receipt

    def receive(self, handle: CanBusHandle, timeout_seconds: float) -> CanFrame | None:
        if id(handle) not in self._injected:
            self._peers[id(handle)].send(
                can.Message(
                    arbitration_id=0x321,
                    is_extended_id=False,
                    data=b"peer-rx",
                    is_fd=False,
                    check=True,
                ),
                timeout=timeout_seconds,
            )
            self._injected.add(id(handle))
        return super().receive(handle, timeout_seconds)

    def close(self, handle: CanBusHandle) -> None:
        peer = self._peers.pop(id(handle), None)
        self._injected.discard(id(handle))
        try:
            super().close(handle)
        finally:
            if peer is not None:
                peer.shutdown()


def _failing_cleanup_worker(
    connection: _Connection,
    control_channels: _WorkerControlChannels,
    readiness_connection: _Connection,
    loaded: LoadedCanFixtureConfig,
    expected_parent_pid: int,
) -> None:
    """Spawn-safe worker target exercising the production cleanup protocol."""

    _run_with_cleanup_signals(
        connection,
        control_channels,
        readiness_connection,
        loaded,
        expected_parent_pid,
        adapter_factory=_FailingCloseVirtualAdapter,
    )


def _peer_checking_worker(
    connection: _Connection,
    control_channels: _WorkerControlChannels,
    readiness_connection: _Connection,
    loaded: LoadedCanFixtureConfig,
    expected_parent_pid: int,
) -> None:
    """Spawn-safe production-worker probe with its independent peer in-child."""

    _run_worker(
        connection,
        control_channels,
        readiness_connection,
        loaded,
        expected_parent_pid,
        _PeerCheckingVirtualAdapter,
    )


class _FailingCleanupClient(CanWorkerClient):
    def _worker_process_target(self) -> Callable[..., None]:
        return _failing_cleanup_worker


class _SignalledCleanupClient(CanWorkerClient):
    def _worker_process_target(self) -> Callable[..., None]:
        return _signalled_cleanup_worker


class _BlockedCleanupClient(CanWorkerClient):
    def _worker_process_target(self) -> Callable[..., None]:
        return _blocked_cleanup_worker


class _PeerCheckingClient(CanWorkerClient):
    def _worker_process_target(self) -> Callable[..., None]:
        return _peer_checking_worker


async def _wait_for_active_worker_request(
    worker: CanWorkerClient, *, timeout_seconds: float = 2.0
) -> None:
    """Wait until the spawned client owns IPC instead of sleeping by guesswork."""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while worker._request_owner is None:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("spawned CAN worker request did not become active")
        await asyncio.sleep(0.01)


def _retire_test_worker(worker: CanWorkerClient) -> None:
    try:
        worker.cancel()
    except CanCleanupError:
        # Negative receipts are intentional fixtures, but leaked children are not.
        if worker.is_alive:
            raise


class VirtualCanWorkerTests(unittest.TestCase):
    def test_console_bootstrap_is_lazy_and_spawn_does_not_repeat_server_startup(self) -> None:
        script = """
from pyocd_debug_mcp.cli import main as _console_main

if __name__ == "__main__":
    import json
    from pathlib import Path
    from pyocd_debug_mcp import server
    from pyocd_debug_mcp.services.can_worker import CanWorkerClient

    root = Path(__import__("os").environ["BYO_MCP_ARTIFACT_ROOT"])
    epoch = root / ".agent-workspace" / "runtime" / "manual-permissions" / "epoch.json"
    epoch_absent_before = not epoch.exists()
    snapshots_before = sorted((root / ".firm" / "runs").glob("*/can-profile/*.json"))
    fixture = server._can_startup.fixture
    assert fixture is not None
    Path(__import__("os").environ["PYOCD_CAN_CONFIG"]).unlink()
    worker = CanWorkerClient(fixture)
    worker.inspect(timeout_seconds=2.0)
    worker.close(timeout_seconds=2.0)
    snapshots_after = sorted((root / ".firm" / "runs").glob("*/can-profile/*.json"))
    print(json.dumps({
        "epoch_absent_before": epoch_absent_before,
        "epoch_absent_after": not epoch.exists(),
        "snapshots_before": len(snapshots_before),
        "snapshots_after": len(snapshots_after),
    }, sort_keys=True))
"""
        with tempfile.TemporaryDirectory(prefix="byo-can-console-spawn-") as temporary:
            root = Path(temporary).resolve()
            profile = root / "can.yaml"
            profile.write_bytes(TX_PROFILE.read_bytes())
            launcher = root / "pyocd-debug-mcp"
            launcher.write_text(script, encoding="utf-8")
            environment = {
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    filter(None, (str(ROOT), str(ROOT / "src"), os.environ.get("PYTHONPATH", "")))
                ),
                "BYO_MCP_ARTIFACT_ROOT": str(root),
                "BYO_MCP_MONITOR_ROOT": str(root / "monitor"),
                "PYOCD_CAN_MODE": "available",
                "PYOCD_CAN_CONFIG": str(profile),
                "PYOCD_CAN_CONFIG_SHA256": "",
                "PYOCD_CAN_RUNTIME_SHA256": "",
                "PYOCD_CAN_READ_SECONDS": "",
            }

            completed = subprocess.run(
                [sys.executable, str(launcher)],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )

        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertTrue(result["epoch_absent_before"])
        self.assertTrue(result["epoch_absent_after"])
        self.assertEqual(result["snapshots_before"], 1)
        self.assertEqual(result["snapshots_after"], 1)

    def test_in_process_adapter_exchanges_exact_frames_with_peer(self) -> None:
        loaded = load_can_fixture_config(TX_PROFILE)
        adapter = PythonCanAdapter()
        handle = adapter.open(loaded, timeout_seconds=1.0)
        peer = can.Bus(
            interface="virtual",
            channel=loaded.config.adapter.channel,
            ignore_config=True,
            receive_own_messages=False,
            fd=False,
        )
        try:
            receipt = adapter.send(
                handle,
                CanFrame(0x123, False, bytes.fromhex("CAFE")),
                timeout_seconds=0.2,
                not_after_monotonic=time.monotonic() + 0.2,
            )
            self.assertEqual(receipt.direction, CanDirection.TX)
            observed_tx = peer.recv(timeout=0.5)
            self.assertIsNotNone(observed_tx)
            assert observed_tx is not None
            self.assertEqual(observed_tx.arbitration_id, 0x123)
            self.assertEqual(bytes(observed_tx.data), bytes.fromhex("CAFE"))
            self.assertFalse(observed_tx.is_fd)
            self.assertFalse(observed_tx.is_remote_frame)

            peer.send(
                can.Message(
                    arbitration_id=0x321,
                    is_extended_id=False,
                    data=b"rx",
                    is_fd=False,
                    check=True,
                ),
                timeout=0.2,
            )
            observed_rx = adapter.receive(handle, timeout_seconds=0.5)
            self.assertIsNotNone(observed_rx)
            assert observed_rx is not None
            self.assertEqual(observed_rx.arbitration_id, 0x321)
            self.assertEqual(observed_rx.data, b"rx")
            self.assertEqual(observed_rx.direction, CanDirection.RX)
            self.assertFalse(observed_rx.is_fd)
            self.assertFalse(observed_rx.is_remote)
            self.assertFalse(observed_rx.is_error)
        finally:
            peer.shutdown()
            adapter.close(handle)

    def test_spawned_worker_connect_send_and_proven_close(self) -> None:
        async def scenario() -> None:
            manager = CanSessionManager(
                load_can_startup(
                    {
                        "PYOCD_CAN_MODE": "available",
                        "PYOCD_CAN_CONFIG": str(TX_PROFILE),
                    }
                )
            )
            try:
                await manager.connect("nucleo_l476rg", None)
                frame = await manager.send(
                    "nucleo_l476rg",
                    0x123,
                    "CA FE",
                    extended=False,
                    timeout_seconds=None,
                )
                self.assertEqual(frame.data, bytes.fromhex("CAFE"))
                self.assertEqual(manager.status("nucleo_l476rg")["phase"], "open")
                await manager.disconnect("nucleo_l476rg")
                self.assertEqual(manager.status("nucleo_l476rg")["phase"], "closed")
            finally:
                self.assertTrue(manager.shutdown_sync())

        asyncio.run(scenario())

    def test_spawned_worker_exchanges_exact_frames_with_child_hosted_peer(self) -> None:
        """Exercise both directions across the real production IPC boundary."""

        loaded = load_can_fixture_config(TX_PROFILE)
        worker = _PeerCheckingClient(loaded)
        try:
            capabilities = worker.inspect(timeout_seconds=2.0)
            self.assertFalse(capabilities.physical)
            worker.open(timeout_seconds=2.0)

            sent = worker.send(
                CanFrame(0x123, False, bytes.fromhex("CAFE")),
                timeout_seconds=0.5,
                operation_timeout_seconds=1.0,
                not_after_monotonic=time.monotonic() + 0.5,
            )
            self.assertEqual(sent.direction, CanDirection.TX)
            self.assertEqual(sent.data, bytes.fromhex("CAFE"))

            received = worker.receive(
                timeout_seconds=0.5,
                operation_timeout_seconds=1.0,
            )
            self.assertIsNotNone(received)
            assert received is not None
            self.assertEqual(received.direction, CanDirection.RX)
            self.assertEqual(received.arbitration_id, 0x321)
            self.assertEqual(received.data, b"peer-rx")
        finally:
            worker.close(timeout_seconds=2.0)

    def test_spawned_worker_receive_cancellation_reaps_child_with_clean_ack(self) -> None:
        async def scenario(worker_type: type[CanWorkerClient]) -> None:
            loaded = load_can_fixture_config(TX_PROFILE)
            worker = worker_type(loaded)
            client = AsyncCanWorkerClient(worker)
            try:
                await client.inspect(timeout_seconds=2.0)
                await client.open(timeout_seconds=2.0)
                receive = asyncio.create_task(
                    client.receive(
                        timeout_seconds=2.0,
                        operation_timeout_seconds=2.5,
                    )
                )
                await _wait_for_active_worker_request(worker)
                receive.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await receive
                self.assertFalse(worker.is_alive)
                self.assertEqual(worker.last_cleanup_outcome, "closed")
                self.assertIn(
                    worker.last_escalation_stage,
                    {
                        "cleanup-acknowledged",
                        "term-cleanup-acknowledged",
                        "term-after-cleanup-cleanup-acknowledged",
                    },
                )
            finally:
                if worker.is_alive:
                    worker.cancel()

        for worker_type in (CanWorkerClient, _SignalledCleanupClient):
            with self.subTest(worker_type=worker_type.__name__):
                asyncio.run(scenario(worker_type))

    def test_blocked_finalizer_is_reaped_without_positive_cleanup_receipt(self) -> None:
        worker = _BlockedCleanupClient(load_can_fixture_config(TX_PROFILE))
        self.addCleanup(_retire_test_worker, worker)
        worker.open(timeout_seconds=2.0)
        started = time.monotonic()
        with self.assertRaises(CanCleanupError):
            worker.cancel()
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertFalse(worker.is_alive)
        self.assertEqual(worker.last_cleanup_outcome, "unknown")
        self.assertEqual(worker.last_escalation_stage, "kill" if os.name == "posix" else "term")
        self.assertIsNone(worker._connection)

    def test_spawned_worker_failed_cleanup_ack_reaps_and_latches_uncertainty(self) -> None:
        async def scenario() -> None:
            loaded = load_can_fixture_config(TX_PROFILE)
            worker = _FailingCleanupClient(loaded)
            self.addCleanup(_retire_test_worker, worker)
            client = AsyncCanWorkerClient(worker)
            await client.inspect(timeout_seconds=2.0)
            await client.open(timeout_seconds=2.0)
            receive = asyncio.create_task(
                client.receive(
                    timeout_seconds=2.0,
                    operation_timeout_seconds=2.5,
                )
            )
            await _wait_for_active_worker_request(worker)
            cancelled_at = time.monotonic()
            receive.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await receive

            self.assertFalse(worker.is_alive)
            self.assertTrue(client.cleanup_uncertain)
            self.assertEqual(
                worker.last_cleanup_outcome,
                "failed",
                {
                    "stage": worker.last_escalation_stage,
                    "exitcode": worker._process.exitcode if worker._process else None,
                    "elapsed": time.monotonic() - cancelled_at,
                },
            )
            self.assertIn(
                worker.last_escalation_stage,
                {
                    "cleanup-failed",
                    "term-after-cleanup-failure",
                    "term-after-cleanup-failure-cleanup-failed",
                },
            )
            # AsyncCanWorkerClient does not report cancellation until the
            # blocking request owner has left its IPC poll/recv boundary.
            self.assertIsNone(worker._request_owner)
            self.assertIsNone(worker._connection)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
