"""Host-only raw flash dispatch through real RPC, programming and readback.

Only the worker's probe/session and FileProgrammer use the existing controlled
memory boundary. Backdating a managed start simulates time already spent; it
does not assert real hardware throughput or replace the public timeout policy.
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
from pyocd_debug_mcp.kernel.operations import current_operation
from pyocd_debug_mcp.kernel.processes import _start_token
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from tests.m02_independent_support import BASE, BOARD, UID, pattern
from tests.tiered_acceptance_support import isolated_project, public_text, tiered_test_server


WORKER = r"""
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch
sys.path.insert(0, sys.argv[3])
from pyocd_debug_mcp.adapters import provider_worker, provider_worker_runtime as runtime, swd_pyocd
from tests import m02_independent_support as boundary

boundary.SIZE = 1024 * 1024
target = boundary.MemoryBoundary()
target.program_mode = sys.argv[1]
if target.program_mode == "hang":
    target.before_program = lambda: time.sleep(60)
dispatch = runtime._dispatch

def observed_dispatch(*args):
    try:
        return dispatch(*args)
    finally:
        Path(sys.argv[2]).write_text(json.dumps({
            "memory_sha256": hashlib.sha256(target.memory).hexdigest(),
            "reads": target.reads, "state": target.state,
            "python": sys.executable, "boundary_import": boundary.__file__,
            "adapter_import": swd_pyocd.__file__, "runtime_import": runtime.__file__,
        }), encoding="utf-8")

runtime._dispatch = observed_dispatch
runtime.PyOCDSWDInterface = lambda: boundary.BoundaryAdapter(target)
with patch.object(swd_pyocd, "FileProgrammer", side_effect=target.programmer):
    provider_worker.main()
"""


class _TimedBackend(swd_process.ProcessIsolatedSWDInterface):
    elapsed_seconds = 0.0
    remaining_seconds = None
    hold = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.entered = threading.Event()
        self.flashes = []

    def flash(self, *args, **kwargs):
        operation = current_operation()
        assert operation is not None
        self.flashes.append(operation)
        operation.started_at -= self.elapsed_seconds
        if self.remaining_seconds is not None:
            operation.started_at = (
                time.monotonic()
                - operation.timeout_seconds
                + swd_process.CANCELLATION_CLEANUP_GRACE_SECONDS
                + self.remaining_seconds
            )
        self.entered.set()
        if self.hold:
            while not operation.cancellation_requested.wait(0.01):
                pass
            operation.checkpoint()  # Cancel before entering the programming transaction.
        return super().flash(*args, **kwargs)


@contextlib.contextmanager
def _fixture(test, mode="write"):
    with isolated_project() as root, tiered_test_server(root) as server:
        script = root / "controlled_worker.py"
        script.write_text(WORKER, encoding="utf-8")
        journal = root / "worker.json"
        backend = _TimedBackend(
            worker_argv=(
                sys.executable,
                str(script),
                mode,
                str(journal),
                str(Path(__file__).resolve().parents[1]),
            )
        )
        server.configure_tiered_test_seams(backend=backend)
        f = SimpleNamespace(root=root, server=server, backend=backend, wire=[], rpc=[])
        original_call = swd_process._WorkerClient.call

        def observed_call(client, name, arguments, **kwargs):
            operation = current_operation()
            if operation is not None:
                f.rpc.append(
                    {
                        "tool": operation.tool_name,
                        "rpc": name,
                        "budget": operation.timeout_seconds,
                        "started_at": operation.started_at,
                        "deadline": kwargs.get("deadline"),
                    }
                )
            return original_call(client, name, arguments, **kwargs)

        def call(tool, **arguments):
            reply = public_text(server, tool, {"board_id": BOARD, **arguments})
            f.wire.append({"tool": tool, "reply": reply})
            return reply

        f.call = call
        inventory = ValidationInventory(probes=(ValidationProbe(UID, "Controlled", "stlink", UID),))
        with (
            patch.object(server, "_validation_inventory", return_value=inventory),
            patch.object(
                server,
                "_hardware_inventory",
                SimpleNamespace(
                    snapshot=lambda: snapshot_from_validation_inventory(inventory),
                    validation_inventory=lambda: inventory,
                ),
            ),
            patch.object(swd_process._WorkerClient, "call", observed_call),
        ):

            def connect():
                test.assertIn("Connected", call("connect", probe_uid=UID, target="stm32l476rgtx"))
                worker = server.connection_manager.maybe_connection(BOARD).handle.worker
                f.workers.append(
                    {
                        "pid": worker._process.pid,
                        "birth": _start_token(worker._process.pid),
                        "client": worker,
                    }
                )

            f.workers = []
            f.connect = connect
            connect()
            try:
                yield f
            finally:
                if server.connection_manager.maybe_connection(BOARD) is not None:
                    call("disconnect")
                if path := os.environ.get("RAW_FLASH_BUDGET_JOURNAL"):
                    workers = [
                        {
                            "pid": row["pid"],
                            "birth": row["birth"],
                            "exit": row["client"]._process.poll(),
                            "cleanup_confirmed": row["client"]._cleanup_confirmed,
                        }
                        for row in f.workers
                    ]
                    with Path(path).open("a", encoding="utf-8") as stream:
                        stream.write(
                            json.dumps(
                                {
                                    "test": test.id(),
                                    "wire": f.wire,
                                    "rpc": f.rpc,
                                    "workers": workers,
                                    "memory": json.loads(journal.read_text()),
                                }
                            )
                            + "\n"
                        )


class RawFlashBudgetTests(unittest.TestCase):
    def test_raw_flash_uses_flash_budget_and_shared_provider_deadline(self):
        with _fixture(self) as f:
            data = pattern(1024 * 1024, 41)
            artifact = f.root / "whole.bin"
            artifact.write_bytes(data)
            f.backend.elapsed_seconds = 31.0
            reply = json.loads(f.call("flash_raw", artifact=str(artifact), base_address=BASE))
            self.assertEqual(reply["status"], "ok", reply)
            self.assertEqual(reply["result"]["programming"]["verified_bytes"], len(data))
            self.assertEqual(reply["result"]["target_state"], "running")
            self.assertEqual(
                json.loads((f.root / "worker.json").read_text())["memory_sha256"],
                hashlib.sha256(data).hexdigest(),
            )
            flash_calls = [row for row in f.rpc if row["tool"] == "flash_raw"]
            self.assertTrue(any(row["rpc"] == "flash" for row in flash_calls))
            for row in flash_calls:
                self.assertEqual(row["budget"], 286.0)
                self.assertEqual(row["deadline"], row["started_at"] + 285.0)
            # The same board's ordinary read retains its 30s/29s bound.
            reply = json.loads(f.call("read_memory_raw", address=BASE))
            self.assertEqual(reply["status"], "ok", reply)
            reads = [row for row in f.rpc if row["tool"] == "read_memory_raw"]
            self.assertTrue(reads)
            for row in reads:
                self.assertEqual(row["budget"], 30.0)
                self.assertEqual(row["deadline"], row["started_at"] + 29.0)

    def test_noop_programmer_still_fails_readback_and_successor_is_usable(self):
        with _fixture(self, "noop") as f:
            artifact = f.root / "noop.bin"
            artifact.write_bytes(pattern(9001, 41))
            reply = json.loads(f.call("flash_raw", artifact=str(artifact), base_address=BASE))
            self.assertEqual(
                (reply["status"], reply["code"]), ("error", "flash/verification-failed"), reply
            )
            self.assertEqual(json.loads(f.call("read_memory_raw", address=BASE))["status"], "ok")

    def test_provider_timeout_is_error_cleans_worker_and_allows_reconnect(self):
        with _fixture(self, "hang") as f:
            artifact = f.root / "hang.bin"
            artifact.write_bytes(pattern(64, 41))
            f.backend.remaining_seconds = 0.12
            reply = json.loads(f.call("flash_raw", artifact=str(artifact), base_address=BASE))
            self.assertEqual((reply["status"], reply["code"]), ("error", "backend/flash-failed"))
            self.assertIn("deadline", reply["message"])
            worker = f.workers[0]["client"]
            self.assertIsNotNone(worker._process.poll())
            self.assertTrue(worker._cleanup_confirmed)
            self.assertIsNone(f.server.connection_manager.maybe_connection(BOARD))
            f.connect()
            self.assertEqual(json.loads(f.call("read_memory_raw", address=BASE))["status"], "ok")

    def test_explicit_request_cancellation_before_programming_releases_board(self):
        with _fixture(self) as f:
            artifact = f.root / "cancel.bin"
            artifact.write_bytes(pattern(64, 41))
            f.backend.hold = True
            returned = []

            async def cancel_request():
                scopes = []

                async def request():
                    with anyio.CancelScope() as scope:
                        scopes.append(scope)
                        returned.append(
                            await f.server.mcp.call_tool(
                                "flash_raw",
                                {
                                    "board_id": BOARD,
                                    "artifact": str(artifact),
                                    "base_address": BASE,
                                },
                            )
                        )

                with anyio.fail_after(10):
                    async with anyio.create_task_group() as group:
                        group.start_soon(request)
                        while not f.backend.entered.is_set():
                            await anyio.sleep(0.01)
                        scopes[0].cancel()

            anyio.run(cancel_request)
            self.assertEqual(returned, [])
            operation = f.backend.flashes[0]
            self.assertTrue(operation.cancellation_requested.is_set())
            self.assertTrue(operation.done.is_set())
            self.assertIsNone(f.server.connection_manager.maybe_connection(BOARD))
            self.assertIsNotNone(f.workers[0]["client"]._process.poll())
            f.connect()
            self.assertEqual(json.loads(f.call("read_memory_raw", address=BASE))["status"], "ok")
