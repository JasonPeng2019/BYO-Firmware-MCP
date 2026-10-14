"""Opt-in real Linux vcan transport checks; no physical bus or mocked backend.

Provision a dedicated vcan link, then set BYO_VCAN_TEST_INTERFACE to its name.
Set BYO_VCAN_ALLOW_LINK_MUTATION=1 only for a disposable link: two cases take
it down or delete/recreate it. These tests do not qualify a physical CAN bus or
the full MCP/debug-board route. The independent peer uses Linux raw sockets,
not python-can, and runs in a separate process from the production worker.
"""

from __future__ import annotations

import asyncio
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import psutil
import yaml

from pyocd_debug_mcp.adapters.can_interface import CanDirection, CanFrame, CapabilityStatus
from pyocd_debug_mcp.can_config import load_can_fixture_config
from pyocd_debug_mcp.can_errors import CanCleanupError, CanError, CanPolicyError
from pyocd_debug_mcp.can_startup import load_can_startup
from pyocd_debug_mcp.services.can_session import CanSessionManager, can_frame_record
from pyocd_debug_mcp.services.can_worker import AsyncCanWorkerClient, CanWorkerClient

ROOT = Path(__file__).resolve().parents[1]
INTERFACE = os.environ.get("BYO_VCAN_TEST_INTERFACE", "")
BOARD = "qualification_board"
PEER = r"""
import json, socket, struct, sys
bus = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
bus.bind((sys.argv[1],))
print(json.dumps({"ready": True}), flush=True)
try:
    for line in sys.stdin:
        command = json.loads(line)
        if command["op"] == "send":
            data = bytes.fromhex(command["data"])
            identifier = command["id"] | (0x80000000 if command.get("extended") else 0)
            bus.send(struct.pack("=IB3x8s", identifier, len(data), data))
            result = {"sent": True}
        else:
            bus.settimeout(command.get("timeout", 0.2))
            try:
                identifier, length, data = struct.unpack("=IB3x8s", bus.recv(16))
                result = {"id": identifier & 0x1fffffff,
                          "extended": bool(identifier & 0x80000000),
                          "rtr": bool(identifier & 0x40000000),
                          "error": bool(identifier & 0x20000000),
                          "dlc": length, "data": data[:length].hex()}
            except socket.timeout:
                result = None
        print(json.dumps(result), flush=True)
finally:
    bus.close()
"""


@unittest.skipUnless(sys.platform.startswith("linux") and INTERFACE, "opt-in Linux vcan link")
class VcanIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("ip") is None:
            self.skipTest("iproute2 is required")
        self.link = self.link_info()
        if self.link["linkinfo"]["info_kind"] != "vcan":
            self.fail("This suite refuses a physical or non-vcan interface")
        self.assertIn("UP", self.link["flags"])
        temporary = tempfile.TemporaryDirectory(prefix="byo-vcan-qualification-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = self.root / "profile.yaml"
        self.document = yaml.safe_load((ROOT / "fixtures/can/virtual_tx_allowed.yaml").read_text())
        self.document.update(profile_id="vcan_qualification", board_ids=[BOARD])
        self.document["adapter"] = {"kind": "socketcan_vcan", "channel": INTERFACE}
        self.write_profile()

    def write_profile(self) -> None:
        self.profile.write_text(yaml.safe_dump(self.document), encoding="utf-8")
        self.profile.chmod(0o600)

    def link_info(self) -> dict:
        result = subprocess.run(
            ["ip", "-json", "-details", "link", "show", "dev", INTERFACE],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode:
            self.skipTest("declared vcan link is absent")
        return json.loads(result.stdout)[0]

    def peer(self) -> subprocess.Popen:
        peer = subprocess.Popen(
            [sys.executable, "-u", "-c", PEER, INTERFACE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def cleanup() -> None:
            peer.stdin.close()
            try:
                peer.wait(timeout=3)
            except subprocess.TimeoutExpired:
                peer.kill()
                peer.wait(timeout=3)
                self.fail("independent peer did not exit on EOF")
            finally:
                peer.stdout.close()
                peer.stderr.close()
            self.assertEqual(peer.returncode, 0)

        self.addCleanup(cleanup)
        self.assertEqual(self.peer_read(peer), {"ready": True})
        return peer

    def peer_read(self, peer: subprocess.Popen):
        self.assertTrue(select.select([peer.stdout], [], [], 3)[0], "peer response timed out")
        return json.loads(peer.stdout.readline())

    def peer_call(self, peer: subprocess.Popen, **command):
        peer.stdin.write(json.dumps(command) + "\n")
        peer.stdin.flush()
        return self.peer_read(peer)

    def worker(self) -> CanWorkerClient:
        worker = CanWorkerClient(load_can_fixture_config(self.profile))
        self.addCleanup(worker.close, timeout_seconds=2.0)
        worker.open(timeout_seconds=2.0)
        return worker

    def manager(self) -> CanSessionManager:
        manager = CanSessionManager(load_can_startup({"PYOCD_CAN_CONFIG": str(self.profile)}))
        self.addCleanup(lambda: self.assertTrue(manager.shutdown_sync()))
        return manager

    def send(self, worker: CanWorkerClient, identifier: int = 0x123) -> CanFrame:
        return worker.send(
            CanFrame(identifier, False, bytes.fromhex("cafe")),
            timeout_seconds=0.5,
            operation_timeout_seconds=1.0,
            not_after_monotonic=time.monotonic() + 0.5,
        )

    def test_real_capability_identity_and_open_continuity(self) -> None:
        worker = self.worker()
        before = worker.inspect(timeout_seconds=2.0)
        self.assertFalse(before.physical)
        self.assertEqual(before.link_kind, "vcan")
        self.assertEqual(before.observed_channel, INTERFACE)
        self.assertEqual(before.interface_index, self.link["ifindex"])
        self.assertEqual(before.bus_state, "active")
        for field in (
            "bounded_open",
            "bounded_send",
            "bounded_receive",
            "bounded_close",
            "pending_tx_quiescence",
        ):
            self.assertIs(getattr(before, field), CapabilityStatus.SUPPORTED)
        self.assertTrue(before.pending_tx_empty)
        self.assertEqual(before, worker.inspect(timeout_seconds=2.0))

    def test_cross_process_receive_and_exact_format_filtering(self) -> None:
        worker, peer = self.worker(), self.peer()
        self.peer_call(peer, op="send", id=0x322, data="bad0")
        self.peer_call(peer, op="send", id=0x321, data="bad1", extended=True)
        self.peer_call(peer, op="send", id=0x321, data="0102030405060708")
        frame = worker.receive(timeout_seconds=0.5, operation_timeout_seconds=1.0)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.arbitration_id, 0x321)
        self.assertFalse(frame.extended)
        self.assertFalse(frame.is_fd or frame.is_remote or frame.is_error)
        self.assertEqual(frame.dlc, 8)
        self.assertEqual(frame.data.hex(), "0102030405060708")
        # SocketCAN marks frames created on this host with MSG_DONTROUTE.
        # python-can preserves that as is_rx=False, even for another process's
        # socket. vcan peer traffic is therefore correctly labelled local TX;
        # physical incoming-RX metadata still needs a real-bus qualification.
        self.assertIs(frame.direction, CanDirection.TX)
        self.assertIsNone(worker.receive(timeout_seconds=0.1, operation_timeout_seconds=0.6))

    def test_cross_process_transmit_is_exactly_one_frame(self) -> None:
        worker, peer = self.worker(), self.peer()
        self.send(worker)
        self.assertEqual(
            self.peer_call(peer, op="recv"),
            {
                "id": 0x123,
                "extended": False,
                "rtr": False,
                "error": False,
                "dlc": 2,
                "data": "cafe",
            },
        )
        self.assertIsNone(self.peer_call(peer, op="recv"))

    def test_empty_read_is_bounded_without_peer(self) -> None:
        worker = self.worker()
        started = time.monotonic()
        self.assertIsNone(worker.receive(timeout_seconds=0.2, operation_timeout_seconds=0.7))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.7)
        print(f"empty_read_elapsed_seconds={elapsed:.6f}", flush=True)

    def test_manager_rejects_forbidden_id_and_malformed_payload_without_traffic(self) -> None:
        manager, peer = self.manager(), self.peer()

        async def scenario() -> None:
            await manager.connect(BOARD, None)
            for identifier, data in ((0x124, "CA FE"), (0x123, "not-hex"), (0x123, "00 " * 9)):
                with self.assertRaises(CanPolicyError):
                    await manager.send(
                        BOARD, identifier, data, extended=False, timeout_seconds=None
                    )
            await manager.disconnect(BOARD)

        asyncio.run(scenario())
        self.assertIsNone(self.peer_call(peer, op="recv"))

    def test_disabled_transmit_produces_no_frame(self) -> None:
        self.document["transmit"]["enabled"] = False
        self.document.pop("health_check", None)
        self.write_profile()
        worker, peer = self.worker(), self.peer()
        with self.assertRaises(CanPolicyError) as caught:
            self.send(worker)
        self.assertEqual(caught.exception.code, "can/tx-disabled")
        self.assertIsNone(self.peer_call(peer, op="recv"))

    def _budget_case(self, field: str, code: str) -> None:
        self.document["transmit"][field] = 1
        self.write_profile()
        manager, peer = self.manager(), self.peer()

        async def scenario() -> None:
            await manager.connect(BOARD, None)
            await manager.send(BOARD, 0x123, "CA FE", extended=False, timeout_seconds=0.5)
            with self.assertRaises(CanPolicyError) as caught:
                await manager.send(BOARD, 0x123, "CA FE", extended=False, timeout_seconds=0.5)
            self.assertEqual(caught.exception.code, code)
            await manager.disconnect(BOARD)

        asyncio.run(scenario())
        self.assertEqual(self.peer_call(peer, op="recv")["data"], "cafe")
        self.assertIsNone(self.peer_call(peer, op="recv"))

    def test_rate_limit_blocks_extra_frame(self) -> None:
        self._budget_case("max_frames_per_second", "can/rate-limited")

    def test_process_tx_quota_blocks_extra_frame(self) -> None:
        self._budget_case("max_tx_attempts_per_process", "can/process-call-limit")

    def test_receive_quota_and_metadata_projection_on_real_frames(self) -> None:
        self.document.pop("health_check", None)
        self.document["process_limits"]["max_received_frames"] = 2
        self.write_profile()
        manager, peer = self.manager(), self.peer()

        async def scenario() -> None:
            await manager.connect(BOARD, None)
            for data in ("aa", "bb", "cc"):
                self.peer_call(peer, op="send", id=0x321, data=data)
            capture = await manager.read(BOARD, read_seconds=0.5, max_frames=8)
            self.assertEqual([frame.data.hex() for frame in capture.frames], ["aa", "bb"])
            self.assertEqual(capture.stop_reason, "can/process-rx-limit")
            self.assertTrue(capture.truncated)
            for frame in capture.frames:
                metadata = can_frame_record(frame, include_payload=False)
                self.assertNotIn("data_hex", metadata)
                self.assertNotIn("payload_sha256", metadata)
                self.assertEqual(metadata["dlc"], 1)
                full = can_frame_record(frame, include_payload=True)
                self.assertEqual(full["data_hex"], frame.data.hex())
            await manager.disconnect(BOARD)

        asyncio.run(scenario())

    def test_parent_process_death_retires_open_worker(self) -> None:
        launcher = self.root / "parent.py"
        launcher.write_text(
            "import json, sys, time\n"
            "from pyocd_debug_mcp.can_config import load_can_fixture_config\n"
            "from pyocd_debug_mcp.services.can_worker import CanWorkerClient\n"
            "if __name__ == '__main__':\n"
            "    worker = CanWorkerClient(load_can_fixture_config(sys.argv[1]))\n"
            "    worker.open(timeout_seconds=2.0)\n"
            "    print(json.dumps({'worker_pid': worker._process.pid}), flush=True)\n"
            "    time.sleep(30)\n",
            encoding="utf-8",
        )
        parent = subprocess.Popen(
            [sys.executable, "-u", str(launcher), str(self.profile)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        child_pid = None
        try:
            self.assertTrue(select.select([parent.stdout], [], [], 10)[0])
            child_pid = json.loads(parent.stdout.readline())["worker_pid"]
            self.assertTrue(psutil.pid_exists(child_pid))
            parent.kill()
            parent.wait(timeout=3)
            deadline = time.monotonic() + 5
            while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(psutil.pid_exists(child_pid), "worker outlived its killed parent")
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=3)
            if child_pid is not None and psutil.pid_exists(child_pid):
                psutil.Process(child_pid).kill()
            parent.stdout.close()
            parent.stderr.close()

    def test_clean_disconnect_reaps_worker_and_allows_reopen(self) -> None:
        worker = self.worker()
        worker.close(timeout_seconds=2.0)
        self.assertFalse(worker.is_alive)
        self.assertEqual(worker.last_cleanup_outcome, "closed")
        replacement = self.worker()
        self.assertTrue(replacement.is_alive)

    def test_receive_cancellation_reaps_real_socket_worker(self) -> None:
        async def scenario() -> None:
            worker = self.worker()
            client = AsyncCanWorkerClient(worker)
            task = asyncio.create_task(
                client.receive(timeout_seconds=2.0, operation_timeout_seconds=2.5)
            )
            deadline = time.monotonic() + 2.0
            while worker._request_owner is None:
                self.assertLess(time.monotonic(), deadline)
                await asyncio.sleep(0.01)
            started = time.monotonic()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(worker.is_alive)
            self.assertEqual(worker.last_cleanup_outcome, "closed")
            self.assertLess(time.monotonic() - started, 2.0)

        asyncio.run(scenario())

    def test_worker_crash_reports_failure_and_does_not_claim_clean_close(self) -> None:
        worker = CanWorkerClient(load_can_fixture_config(self.profile))
        worker.open(timeout_seconds=2.0)
        process = worker._process
        self.assertIsNotNone(process)
        try:
            process.kill()
            process.join(timeout=2.0)
            self.assertFalse(process.is_alive())
            with self.assertRaises(CanError):
                worker.receive(timeout_seconds=0.1, operation_timeout_seconds=0.6)
            with self.assertRaises(CanCleanupError):
                worker.close(timeout_seconds=2.0)
            self.assertFalse(worker.is_alive)
            self.assertNotEqual(worker.last_cleanup_outcome, "closed")
        finally:
            if process.is_alive():
                process.kill()
                process.join(timeout=2.0)

    def mutate_link(self, *arguments: str) -> None:
        if os.environ.get("BYO_VCAN_ALLOW_LINK_MUTATION") != "1":
            self.skipTest("link mutation requires an explicitly disposable interface")
        subprocess.run(["sudo", "-n", "ip", "link", *arguments], check=True, timeout=5)

    def test_link_down_is_refused_before_transmit(self) -> None:
        worker = self.worker()
        self.mutate_link("set", "dev", INTERFACE, "down")
        try:
            with self.assertRaises(CanError) as caught:
                self.send(worker)
            self.assertEqual(caught.exception.code, "can/capability-unproven")
        finally:
            self.mutate_link("set", "dev", INTERFACE, "up")

    def test_recreated_link_identity_is_refused(self) -> None:
        worker = self.worker()
        self.mutate_link("delete", "dev", INTERFACE)
        try:
            self.mutate_link("add", "dev", INTERFACE, "type", "vcan")
            self.mutate_link("set", "dev", INTERFACE, "up")
            self.assertNotEqual(self.link_info()["ifindex"], self.link["ifindex"])
            with self.assertRaises(CanError) as caught:
                self.send(worker)
            self.assertEqual(caught.exception.code, "can/capability-unproven")
        finally:
            if subprocess.run(
                ["ip", "link", "show", "dev", INTERFACE], capture_output=True
            ).returncode:
                self.mutate_link("add", "dev", INTERFACE, "type", "vcan")
                self.mutate_link("set", "dev", INTERFACE, "up")


if __name__ == "__main__":
    unittest.main(verbosity=2)
