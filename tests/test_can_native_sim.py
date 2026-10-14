"""Native simulation acceptance: real TCP peer, broker, and spawned CAN worker.

Run unchanged on Linux, macOS and Windows. No sys.platform mocks or physical
hardware are used. The peer uses the documented binary protocol directly.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp.adapters.can_interface import CanFrame, CanDirection
from pyocd_debug_mcp.can_config import load_can_fixture_config
from pyocd_debug_mcp.can_errors import CanCleanupError, CanError, CanTimeoutError
from pyocd_debug_mcp.services.can_worker import AsyncCanWorkerClient, CanWorkerClient

ROOT = Path(__file__).resolve().parents[1]
FRAME = struct.Struct("!IBB8s")


def recv_exact(sock: socket.socket, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        chunk = sock.recv(count - len(result))
        if not chunk:
            raise ConnectionError("simulator closed")
        result.extend(chunk)
    return bytes(result)


class NativeSimulatorFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="byo-native-can-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.broker = subprocess.Popen(
            [sys.executable, "-m", "pyocd_debug_mcp.adapters.can_sim"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=ROOT,
        )
        self.addCleanup(self._cleanup_broker)
        assert self.broker.stdout is not None
        self.channel = self.broker.stdout.readline().strip()
        if not self.channel:
            self.fail(f"simulator failed: {self.broker.communicate(timeout=5)}")
        self.profile = self.root / "can.yaml"
        self.profile.write_text(
            (ROOT / "fixtures/can/tcp_sim.yaml")
            .read_text()
            .replace("127.0.0.1:43113", self.channel),
            encoding="utf-8",
        )
        self.peer = socket.create_connection(("127.0.0.1", int(self.channel.split(":")[1])), 2)
        self.addCleanup(self.peer.close)
        self.peer.sendall(b"BYOCAN1\x00")
        self.assertEqual(recv_exact(self.peer, 7), b"BYOCAN1")
        self.worker = CanWorkerClient(load_can_fixture_config(self.profile))
        self.addCleanup(self._cleanup_worker)

    def _cleanup_worker(self) -> None:
        try:
            self.worker.close(timeout_seconds=2)
        except CanError:
            pass

    def _cleanup_broker(self) -> None:
        self.broker.terminate()
        try:
            self.broker.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.broker.kill()
            self.broker.communicate(timeout=5)

    def open_worker(self) -> None:
        capabilities = self.worker.inspect(timeout_seconds=2)
        self.assertFalse(capabilities.physical)
        self.assertEqual(capabilities.driver, "tcp_sim")
        self.worker.open(timeout_seconds=2)


class NativeSimulatorTests(NativeSimulatorFixture):
    def test_cross_process_exact_tx_rx_and_format_filters(self) -> None:
        self.open_worker()
        sent = self.worker.send(
            CanFrame(0x123, False, b"\xca\xfe"), timeout_seconds=0.5, operation_timeout_seconds=1
        )
        self.assertEqual(sent.direction, CanDirection.TX)
        self.assertEqual(
            FRAME.unpack(recv_exact(self.peer, FRAME.size + 1)[1:]),
            (0x123, 0, 2, b"\xca\xfe" + bytes(6)),
        )
        for identifier, extended, payload in (
            (0x222, 0, b"bad"),
            (0x321, 1, b"bad"),
            (0x321, 0, b"good"),
        ):
            self.peer.sendall(FRAME.pack(identifier, extended, len(payload), payload))
        received = self.worker.receive(timeout_seconds=0.5, operation_timeout_seconds=1)
        self.assertIsNotNone(received)
        assert received is not None
        self.assertEqual(
            (received.arbitration_id, received.extended, received.data), (0x321, False, b"good")
        )
        self.assertEqual(received.direction, CanDirection.RX)
        self.assertIsNone(self.worker.receive(timeout_seconds=0.1, operation_timeout_seconds=0.5))
        self.worker.close(timeout_seconds=2)
        self.assertFalse(self.worker.is_alive)
        self.assertEqual(self.worker.last_cleanup_outcome, "closed")

    def test_empty_read_is_bounded_and_disconnect_allows_reopen(self) -> None:
        self.open_worker()
        started = time.monotonic()
        self.assertIsNone(self.worker.receive(timeout_seconds=0.15, operation_timeout_seconds=0.5))
        self.assertLess(time.monotonic() - started, 0.8)
        self.worker.close(timeout_seconds=2)
        self.worker.open(timeout_seconds=2)
        self.worker.close(timeout_seconds=2)
        self.assertFalse(self.worker.is_alive)

    def test_cancel_receive_reaps_worker_and_acknowledges_close(self) -> None:
        self.open_worker()

        async def scenario() -> None:
            client = AsyncCanWorkerClient(self.worker)
            task = asyncio.create_task(
                client.receive(timeout_seconds=5, operation_timeout_seconds=6)
            )
            while self.worker._request_owner is None:
                await asyncio.sleep(0.01)
            started = time.monotonic()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(self.worker.is_alive)
            self.assertEqual(self.worker.last_cleanup_outcome, "closed")

        asyncio.run(scenario())

    def test_own_echo_is_tx_not_external_rx(self) -> None:
        self.profile.write_text(
            self.profile.read_text()
            .split("health_check:")[0]
            .replace("receive_own_messages: false", "receive_own_messages: true")
            .replace("arbitration_id: 0x321", "arbitration_id: 0x123")
        )
        self.worker = CanWorkerClient(load_can_fixture_config(self.profile))
        self.open_worker()
        self.worker.send(
            CanFrame(0x123, False, b"echo"), timeout_seconds=0.2, operation_timeout_seconds=0.5
        )
        frame = self.worker.receive(timeout_seconds=0.5, operation_timeout_seconds=1)
        self.assertIsNotNone(frame)
        assert frame is not None
        self.assertEqual(frame.direction, CanDirection.TX)
        self.assertEqual(frame.data, b"echo")

    def test_broker_loss_is_reported_without_reconnecting(self) -> None:
        self.open_worker()
        self.broker.terminate()
        self.broker.wait(timeout=5)
        with self.assertRaises(CanError):
            self.worker.receive(timeout_seconds=0.5, operation_timeout_seconds=1)

    def test_policy_refusal_never_reaches_peer(self) -> None:
        self.open_worker()
        with self.assertRaises(CanError):
            self.worker.send(
                CanFrame(0x456, False, b"bad"), timeout_seconds=0.2, operation_timeout_seconds=0.5
            )
        self.peer.settimeout(0.1)
        with self.assertRaises(TimeoutError):
            self.peer.recv(1)

    def test_split_peer_packet_is_preserved(self) -> None:
        self.open_worker()
        packet = FRAME.pack(0x321, 0, 3, b"abc")
        self.peer.sendall(packet[:5])
        self.assertIsNone(self.worker.receive(timeout_seconds=0.15, operation_timeout_seconds=0.5))
        self.peer.sendall(packet[5:])
        received = self.worker.receive(timeout_seconds=0.5, operation_timeout_seconds=1)
        self.assertIsNotNone(received)
        assert received is not None
        self.assertEqual(received.data, b"abc")


class CaptureRetentionTests(NativeSimulatorFixture):
    def test_hash_only_capture_and_artifact_count_budget(self) -> None:
        self._capture_case(metadata=True, fail_write=False, byte_limit=False)

    def test_capture_byte_budget_refuses_publication(self) -> None:
        self._capture_case(metadata=False, fail_write=False, byte_limit=True)

    def test_write_failure_is_not_reported_as_success(self) -> None:
        self._capture_case(metadata=False, fail_write=True, byte_limit=False)

    def _capture_case(self, *, metadata: bool, fail_write: bool, byte_limit: bool) -> None:
        from pyocd_debug_mcp.can_startup import load_can_startup
        from pyocd_debug_mcp.services.can_session import CanSessionManager

        text = self.profile.read_text().split("health_check:")[0]
        text = text.replace("max_capture_artifacts: 256", "max_capture_artifacts: 1")
        if byte_limit:
            text = text.replace("max_capture_bytes: 16777216", "max_capture_bytes: 1")
        if metadata:
            text = text.replace("artifact_payloads: full", "artifact_payloads: hash_only")
        self.profile.write_text(text)
        manager = CanSessionManager(
            load_can_startup(
                {"PYOCD_CAN_MODE": "available", "PYOCD_CAN_CONFIG": str(self.profile)},
                snapshot_dir=self.root / "can-profile",
            )
        )
        self.addCleanup(manager.shutdown_sync)

        async def scenario() -> None:
            await manager.connect("nucleo_l476rg", None)
            self.peer.sendall(FRAME.pack(0x321, 0, 4, b"data"))
            if fail_write:
                with patch(
                    "pyocd_debug_mcp.services.can_session.write_immutable_private_file",
                    side_effect=OSError("disk unavailable"),
                ):
                    with self.assertRaises(CanError):
                        await manager.read("nucleo_l476rg", read_seconds=0.5, max_frames=1)
            elif byte_limit:
                with self.assertRaises(CanError) as caught:
                    await manager.read("nucleo_l476rg", read_seconds=0.5, max_frames=1)
                self.assertEqual(caught.exception.code, "can/process-artifact-limit")
            else:
                captured = await manager.read("nucleo_l476rg", read_seconds=0.5, max_frames=1)
                assert captured.artifact is not None
                data = (self.root / "can-captures" / str(captured.artifact["name"])).read_text()
                self.assertNotIn("data_hex", data)
                self.assertEqual(
                    json.loads(data)["frames"][0]["payload_sha256"],
                    __import__("hashlib").sha256(b"data").hexdigest(),
                )
                self.assertEqual(len(json.loads(data)["frames"]), 1)
                await manager.disconnect("nucleo_l476rg")
                await manager.connect("nucleo_l476rg", None)
                with self.assertRaises(CanError) as caught:
                    await manager.read("nucleo_l476rg", read_seconds=0.01, max_frames=1)
                self.assertEqual(caught.exception.code, "can/process-artifact-limit")
            await manager.disconnect("nucleo_l476rg")

        asyncio.run(scenario())
        artifacts = list((self.root / "can-captures").glob("*.json"))
        self.assertEqual(len(artifacts), 0 if fail_write or byte_limit else 1)


class StartupDeadlineTests(unittest.TestCase):
    def test_pipe_allocation_failure_closes_previously_allocated_endpoints(self) -> None:
        import multiprocessing
        from unittest.mock import Mock

        context = multiprocessing.get_context("spawn")
        pair = context.Pipe()
        fake = Mock(wraps=context)
        fake.Pipe.side_effect = [pair, OSError("pipe allocation failed")]
        worker = CanWorkerClient(
            load_can_fixture_config(ROOT / "fixtures/can/virtual_tx_allowed.yaml")
        )
        with patch(
            "pyocd_debug_mcp.services.can_worker.multiprocessing.get_context", return_value=fake
        ):
            with self.assertRaises(CanError):
                worker.open(timeout_seconds=0.2)
        self.assertTrue(all(item.closed for item in pair))
        self.assertFalse(worker.is_alive)
        self.assertEqual(worker.last_cleanup_outcome, "not-running")

    def test_launch_thread_start_failure_before_and_after_thread_creation(self) -> None:
        original = threading.Thread.start
        for created in (False, True):
            worker = CanWorkerClient(
                load_can_fixture_config(ROOT / "fixtures/can/virtual_tx_allowed.yaml")
            )
            threads = []

            def fail(thread) -> None:
                threads.append(thread)
                if created:
                    original(thread)
                raise RuntimeError("thread launch failed")

            with self.subTest(created=created), patch.object(threading.Thread, "start", fail):
                with self.assertRaises(CanError):
                    worker.open(timeout_seconds=0.2)
            if created:
                threads[0].join(2)
                self.assertFalse(threads[0].is_alive())
            self.assertFalse(worker.is_alive)
            self.assertEqual(worker.last_cleanup_outcome, "not-running")
            self.assertIsNone(worker._connection)

    def test_cancel_before_launch_claim_closes_every_endpoint(self) -> None:
        import multiprocessing

        original_start = threading.Thread.start
        context = multiprocessing.get_context("spawn")
        original_pipe = context.Pipe
        endpoints = []
        ready = threading.Event()
        release = threading.Event()

        def pipe(*, duplex):
            pair = original_pipe(duplex=duplex)
            endpoints.extend(pair)
            return pair

        def delayed(thread):
            if thread.name == "byo-can-launch-owner":
                ready.set()
                # Return as if scheduled, then actually run after cancellation.
                launch_threads.append(thread)
            else:
                original_start(thread)

        launch_threads = []
        worker = CanWorkerClient(
            load_can_fixture_config(ROOT / "fixtures/can/virtual_tx_allowed.yaml")
        )
        errors = []

        def request():
            try:
                worker.open(timeout_seconds=1)
            except CanError as exc:
                errors.append(exc)
            finally:
                release.set()

        with patch.object(context, "Pipe", pipe), patch.object(threading.Thread, "start", delayed):
            caller = threading.Thread(target=request)
            caller.start()
            self.assertTrue(ready.wait(1))
            with self.assertRaises(CanCleanupError):
                worker.cancel()
            self.assertTrue(release.wait(1))
            original_start(launch_threads[0])
            launch_threads[0].join(2)
            caller.join(2)
        self.assertTrue(errors)
        self.assertEqual(len(endpoints), 8)
        self.assertTrue(all(endpoint.closed for endpoint in endpoints))
        self.assertEqual(worker.last_cleanup_outcome, "not-running")
        self.assertFalse(worker.is_alive)

    def test_slow_os_start_times_out_and_late_child_is_reaped(self) -> None:
        self._delayed_start(cancel=False, raise_after=False)

    def test_cancel_during_os_start_returns_before_spawn_and_reaps_late_child(self) -> None:
        self._delayed_start(cancel=True, raise_after=False)

    def test_start_raises_after_creating_child_still_reaps(self) -> None:
        self._delayed_start(cancel=False, raise_after=True)

    def _delayed_start(self, *, cancel: bool, raise_after: bool) -> None:
        import multiprocessing.process

        loaded = load_can_fixture_config(ROOT / "fixtures/can/virtual_tx_allowed.yaml")
        worker = CanWorkerClient(loaded)
        original = multiprocessing.process.BaseProcess.start
        entered = threading.Event()
        release = threading.Event()
        launched = threading.Event()
        processes = []
        failures: list[BaseException] = []

        def delayed(process) -> None:
            processes.append(process)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release spawn")
            original(process)
            launched.set()
            if raise_after:
                raise RuntimeError("simulated start error after OS creation")

        def request() -> None:
            try:
                worker.open(timeout_seconds=1)
            except BaseException as exc:
                failures.append(exc)

        with (
            patch.object(multiprocessing.process.BaseProcess, "start", delayed),
            patch(
                "pyocd_debug_mcp.services.can_worker.MAX_WORKER_READINESS_SECONDS",
                0.2,
            ),
        ):
            caller = threading.Thread(target=request)
            caller.start()
            self.assertTrue(entered.wait(1))
            try:
                started = time.monotonic()
                if cancel:
                    with self.assertRaises(CanCleanupError):
                        worker.cancel()
                caller.join(1)
                self.assertFalse(caller.is_alive(), "caller waited for the OS spawn")
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(worker.last_cleanup_outcome, "unknown")
                with self.assertRaises(CanCleanupError):
                    worker.close(timeout_seconds=0.2)
                self.assertTrue(failures)
                if not cancel:
                    self.assertIsInstance(failures[0], CanTimeoutError)
            finally:
                release.set()
                self.assertTrue(launched.wait(3))
                caller.join(3)
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    if (
                        worker._launch_done is not None
                        and worker._launch_done.is_set()
                        and not worker.is_alive
                    ):
                        break
                    time.sleep(0.02)
                self.assertFalse(worker.is_alive, "late worker leaked")
                self.assertEqual(len(processes), 1)
                self.assertFalse(processes[0].is_alive())
                # A cancelled generation can never send even after late startup.
                with self.assertRaises(CanError):
                    worker.open(timeout_seconds=0.2)


if __name__ == "__main__":
    unittest.main()
