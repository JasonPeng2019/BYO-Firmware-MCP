"""Independent public deadline oracles at the provider's actual pipe writes.

The retained M02 memory fixture substitutes native probe/programmer I/O only.
No implementer regression or timeout classifier is used as an expected value.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import anyio

from pyocd_debug_mcp.adapters import swd_process
from pyocd_debug_mcp.hardware_inventory import snapshot_from_validation_inventory
from pyocd_debug_mcp.kernel.operations import current_operation, operation_manager
from pyocd_debug_mcp.kernel.processes import _start_token
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from tests.tiered_acceptance_support import isolated_project, public_text, tiered_test_server


BOARD = "independent-budget"
UID = "independent-provider-probe"
BASE = 0x08000000
SIZE = 1048576

# Reuse the earlier independent low-level hardware fixture, not the repair's
# tests. Real worker protocol, adapter parsing/programming/readback remain live.
WORKER = r"""
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from tests import m02_independent_support as hardware
from pyocd_debug_mcp.adapters import provider_worker, provider_worker_runtime, swd_pyocd
hardware.SIZE = 1048576
target = hardware.MemoryBoundary()
target.program_mode = sys.argv[2]
if target.program_mode == "hang":
    def hang():
        Path(sys.argv[3] + ".entered").write_text("programming entered")
        time.sleep(60)
    target.before_program = hang
if target.program_mode == "unsupported":
    target.memory_map.get_region_for_address(hardware.BASE).flash.is_valid = False
real_dispatch = provider_worker_runtime._dispatch
def dispatch(adapter, handle, opcode, arguments):
    try:
        return real_dispatch(adapter, handle, opcode, arguments)
    finally:
        record = {
            "opcode": opcode, "memory_sha256": hashlib.sha256(target.memory).hexdigest(),
            "program_calls": sum(c[0] == "program" for c in target.calls),
            "reads": target.reads, "state": target.state,
            "imports": {m.__name__: m.__file__ for m in
                        (hardware, provider_worker_runtime, swd_pyocd)},
        }
        with Path(sys.argv[3]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
provider_worker_runtime._dispatch = dispatch
provider_worker_runtime.PyOCDSWDInterface = lambda: hardware.BoundaryAdapter(target)
with patch.object(swd_pyocd, "FileProgrammer", side_effect=target.programmer):
    provider_worker.main()
"""


class _ExpiringProvider(swd_process.ProcessIsolatedSWDInterface):
    expire_flash = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.failures = []

    def _call(self, handle, operation, arguments=None, **kwargs):
        if operation == "flash" and self.expire_flash:
            active = current_operation()
            assert active is not None
            # Deterministically exhaust all but 200ms of the existing provider
            # time. Neither the public timeout classifier nor RPC is mocked.
            active.started_at = time.monotonic() - active.timeout_seconds + 1.2
        try:
            return super()._call(handle, operation, arguments, **kwargs)
        except Exception as exc:
            self.failures.append(
                {
                    "operation": operation,
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            raise


class PublicProviderBudgetContract(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = self.stack.enter_context(isolated_project())
        self.server = self.stack.enter_context(tiered_test_server(self.root))
        self.frames = []
        self.responses = []
        self.workers = []
        self.failure_states = []
        inventory = ValidationInventory(probes=(ValidationProbe(UID, "Controlled", "stlink", UID),))
        self.stack.enter_context(
            patch.object(
                self.server,
                "_hardware_inventory",
                SimpleNamespace(
                    snapshot=lambda: snapshot_from_validation_inventory(inventory),
                    validation_inventory=lambda: inventory,
                ),
            )
        )
        self.stack.enter_context(
            patch.object(self.server, "_validation_inventory", return_value=inventory)
        )
        original = swd_process._WorkerClient._write

        def record_write(client, frame, deadline):
            active = current_operation()
            request = json.loads(frame)
            self.frames.append(
                {
                    "request": request,
                    "deadline": deadline,
                    "observed_at": time.monotonic(),
                    "tool": active.tool_name if active else None,
                    "budget": active.timeout_seconds if active else None,
                    "start": active.started_at if active else None,
                }
            )
            return original(client, frame, deadline)

        self.stack.enter_context(patch.object(swd_process._WorkerClient, "_write", record_write))
        (self.root / "worker.py").write_text(WORKER, encoding="utf-8")
        self.worker_log = self.root / "worker.jsonl"

    def connect(self, mode="write"):
        self.backend = _ExpiringProvider(
            worker_argv=(
                sys.executable,
                str(self.root / "worker.py"),
                str(Path(__file__).resolve().parents[1]),
                mode,
                str(self.worker_log),
            )
        )
        self.server.configure_tiered_test_seams(backend=self.backend)
        reply = self.call("connect", probe_uid=UID, target="stm32l476rgtx")
        self.assertIn("Connected", reply)
        worker = self.server.connection_manager.maybe_connection(BOARD).handle.worker
        self.workers.append((worker, worker._process.pid, _start_token(worker._process.pid)))
        return worker

    def call(self, name, **arguments):
        result = public_text(self.server, name, {"board_id": BOARD, **arguments})
        self.responses.append({"tool": name, "arguments": arguments, "primary": result})
        return result

    def flash(self, size=8219, *, addressed=True):
        # Independent expected bytes, deliberately including the final partial
        # verification chunk and all byte values.
        self.expected = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
        image = self.root / "image.bin"
        image.write_bytes(self.expected)
        arguments = {"artifact": str(image)}
        if addressed:
            arguments["base_address"] = BASE
        return json.loads(self.call("flash_raw", **arguments))

    def rows(self):
        return [json.loads(line) for line in self.worker_log.read_text().splitlines()]

    def assert_closed(self, worker):
        self.assertIsNotNone(worker._process.poll())
        self.assertTrue(worker._cleanup_confirmed)
        self.assertIsNone(worker._marker)
        self.assertIsNone(self.server.connection_manager.maybe_connection(BOARD))
        self.assertEqual(operation_manager.snapshots(), ())

    def tearDown(self):
        try:
            if self.server.connection_manager.maybe_connection(BOARD) is not None:
                self.call("disconnect")
            self.server._monitor.closeout("independent provider test finished")
        finally:
            if path := os.environ.get("RAW_FLASH_BUDGET_JOURNAL"):
                record = {
                    "test": self.id(),
                    "frames": self.frames,
                    "responses": self.responses,
                    "failure_states": self.failure_states,
                    "workers": [
                        {
                            "pid": pid,
                            "birth": birth,
                            "exit": worker._process.poll(),
                            "cleanup": worker._cleanup_confirmed,
                        }
                        for worker, pid, birth in self.workers
                    ],
                    "hardware_observations": self.rows() if self.worker_log.exists() else [],
                }
                with Path(path).open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record) + "\n")

    def test_full_image_gets_existing_flash_budget_and_ordinary_successor_stays_bounded(self):
        self.connect()
        result = self.flash(SIZE)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["result"]["programming"]["verified_bytes"], SIZE)
        flash = next(row for row in self.frames if row["request"]["operation"] == "flash")
        self.assertEqual(flash["tool"], "flash_raw")
        self.assertEqual(flash["budget"], 286.0)
        self.assertAlmostEqual(flash["deadline"] - flash["start"], 285.0, places=5)
        self.assertGreaterEqual(flash["start"] + flash["budget"] - flash["deadline"], 1.0)
        observed = next(row for row in self.rows() if row["opcode"] == "flash")
        self.assertEqual(observed["memory_sha256"], hashlib.sha256(self.expected).hexdigest())
        self.assertEqual(observed["program_calls"], 1)
        self.assertEqual(sum(size for _, size in observed["reads"]), SIZE)
        self.assertEqual(json.loads(self.call("read_memory_raw", address=BASE))["status"], "ok")
        read = next(
            row for row in reversed(self.frames) if row["request"]["operation"] == "read_memory"
        )
        self.assertEqual(read["tool"], "read_memory_raw")
        self.assertEqual(read["budget"], 30.0)
        self.assertAlmostEqual(read["deadline"] - read["start"], 29.0, places=5)

    def test_corruption_remains_failed_verification_without_a_second_program_attempt(self):
        self.connect("corrupt")
        result = self.flash()
        self.assertEqual((result["status"], result["code"]), ("error", "flash/verification-failed"))
        row = next(row for row in self.rows() if row["opcode"] == "flash")
        self.assertEqual(row["program_calls"], 1)
        self.assertNotEqual(row["memory_sha256"], hashlib.sha256(self.expected).hexdigest())

    def test_ordinary_read_keeps_its_original_bound(self):
        self.connect()
        self.assertEqual(json.loads(self.call("read_memory_raw", address=BASE))["status"], "ok")
        row = next(row for row in self.frames if row["request"]["operation"] == "read_memory")
        self.assertEqual(row["budget"], 30.0)
        self.assertAlmostEqual(row["deadline"] - row["start"], 29.0, places=5)

    def test_unsupported_target_and_unaddressed_bin_refuse_before_programming(self):
        self.connect("unsupported")
        unaddressed = self.flash(addressed=False)
        unsupported = self.flash()
        self.assertEqual(
            (unaddressed["status"], unaddressed["code"]), ("refused", "flash/unsupported-artifact")
        )
        self.assertEqual(
            (unsupported["status"], unsupported["code"]), ("refused", "flash/unsupported-target")
        )
        self.assertTrue(all(row["program_calls"] == 0 for row in self.rows()))
        self.assertIn("halted", self.call("get_state").lower())

    def test_programmer_error_evicts_connection_and_allows_a_new_worker(self):
        original = self.connect("raise")
        result = self.flash()
        self.assertEqual((result["status"], result["code"]), ("error", "backend/flash-failed"))
        self.assertIn("controlled programmer exception", result["message"])
        self.assertEqual(
            next(row for row in self.rows() if row["opcode"] == "flash")["program_calls"], 1
        )
        state = {
            "exception": self.backend.failures,
            "connected_after_error": self.server.connection_manager.maybe_connection(BOARD)
            is not None,
            "original_exit_after_error": original._process.poll(),
            "cleanup_after_error": original._cleanup_confirmed,
            "closed_after_error": original._closed,
            "active_operations": len(operation_manager.snapshots()),
        }
        self.failure_states.append(state)
        # Capture the actual failed state, then prove explicit owned cleanup
        # and a successor before asserting the missing automatic invalidation.
        if state["connected_after_error"]:
            self.call("disconnect")
        self.assert_closed(original)
        successor = self.connect()
        self.assertIsNot(successor, original)
        self.assertEqual(json.loads(self.call("read_memory_raw", address=BASE))["status"], "ok")
        self.assertEqual(state["exception"][0]["type"], "TargetConnectionError")
        self.assertFalse(state["connected_after_error"], state)
        self.assertIsNotNone(state["original_exit_after_error"], state)

    def test_actual_remaining_time_expiry_terminates_worker_and_releases_board(self):
        worker = self.connect("hang")
        self.backend.expire_flash = True
        result = self.flash()
        self.assertTrue(Path(str(self.worker_log) + ".entered").exists())
        self.assertEqual((result["status"], result["code"]), ("error", "backend/flash-failed"))
        self.assertIn("TimeoutError", result["message"])
        self.assertIn("terminated", result["message"])
        self.assert_closed(worker)
        self.connect()
        self.assertEqual(json.loads(self.call("read_memory_raw", address=BASE))["status"], "ok")

    def test_cancellation_at_artifact_admission_never_programs_and_cleanup_finishes(self):
        worker = self.connect()
        image = self.root / "cancel.bin"
        image.write_bytes(bytes(range(64)))
        from pyocd_debug_mcp.tools import raw

        entered = threading.Event()
        captured = []

        def hold_admission(*_args):
            active = current_operation()
            captured.append(active)
            entered.set()
            self.assertTrue(active.cancellation_requested.wait(5))
            active.checkpoint()

        async def scenario():
            scopes = []
            returned = []

            async def request():
                with anyio.CancelScope() as scope:
                    scopes.append(scope)
                    returned.append(
                        await self.server.mcp.call_tool(
                            "flash_raw",
                            {
                                "board_id": BOARD,
                                "artifact": str(image),
                                "base_address": BASE,
                            },
                        )
                    )

            with anyio.fail_after(10):
                async with anyio.create_task_group() as group:
                    group.start_soon(request)
                    while not entered.is_set():
                        await anyio.sleep(0.005)
                    scopes[0].cancel()
            self.assertEqual(returned, [])

        with patch.object(raw, "check_flash_request", hold_admission):
            anyio.run(scenario)
        self.assertTrue(captured[0].done.is_set())
        self.assertFalse(any(row["request"]["operation"] == "flash" for row in self.frames))
        self.assert_closed(worker)
        self.connect()
        self.assertEqual(json.loads(self.call("read_memory_raw", address=BASE))["status"], "ok")


if __name__ == "__main__":
    unittest.main()
