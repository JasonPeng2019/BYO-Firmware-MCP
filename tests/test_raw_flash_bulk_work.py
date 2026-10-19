"""Bulk raw flashing through registered dispatch, real RPC and the pyOCD adapter.

Only probe/programmer I/O is simulated. Two independently specified 110-second
work phases advance the provider's clock at file barriers; no minutes-long sleep,
global profiler, production timer override or physical throughput claim is used.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.adapters import swd_process
from pyocd_debug_mcp.hardware_inventory import snapshot_from_validation_inventory
from pyocd_debug_mcp.kernel.operations import operation_manager, operation_timeout_seconds
from pyocd_debug_mcp.kernel.processes import _start_token
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from tests.m02_independent_support import BASE, BOARD, UID, write_artifact
from tests.tiered_acceptance_support import _content_text, isolated_project, tiered_test_server


SIZE = 1024 * 1024

WORKER = r"""
import hashlib, json, sys, time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from tests import m02_independent_support as hardware
from pyocd_debug_mcp.adapters import provider_worker, provider_worker_runtime as runtime, swd_pyocd
hardware.SIZE = 1048576
target = hardware.MemoryBoundary()
target.program_mode = sys.argv[2]
control, journal = Path(sys.argv[3]), Path(sys.argv[4])
original_programmer, original_read = target.programmer, target.read_memory_block8
expected_reads = 0
completed_reads = 0
barriers = False

def barrier(name):
    (control / name).write_text('entered')
    deadline = time.monotonic() + 15
    while not (control / (name + '.release')).exists():
        if time.monotonic() > deadline:
            raise RuntimeError('host test failed to release ' + name)
        time.sleep(0.005)

def programmer(session, **kwargs):
    inner = original_programmer(session, **kwargs)
    def program(name, **options):
        global expected_reads, completed_reads, barriers
        expected_reads = sum(len(data) for _, data in hardware.separate_loads(
            Path(name), options.get('base_address')))
        completed_reads = 0
        barriers = expected_reads == 1048576
        inner.program(name, **options)
        if barriers:
            barrier('programmed')
    return type('Programmer', (), {'program': staticmethod(program)})()

def read(address, count):
    global completed_reads
    data = original_read(address, count)
    completed_reads += count
    if barriers and completed_reads == expected_reads:
        barrier('readback-complete')
    return data

target.read_memory_block8 = read
dispatch = runtime._dispatch
def observe(adapter, handle, opcode, arguments):
    try:
        return dispatch(adapter, handle, opcode, arguments)
    finally:
        imports = []
        for name, module in tuple(sys.modules.items()):
            path = getattr(module, '__file__', None)
            if path and (name.startswith('pyocd_debug_mcp') or name in (
                    'tests.m02_independent_support', 'tests.test_raw_flash_bulk_work',
                    'pyocd.flash.file_programmer', 'pyocd.flash.builder')):
                p = Path(path).resolve()
                imports.append({'module': name, 'path': str(p),
                                'sha256': hashlib.sha256(p.read_bytes()).hexdigest()})
        row = {'opcode': opcode, 'program_calls': sum(c[0] == 'program' for c in target.calls),
               'reads': target.reads, 'state': target.state, 'imports': imports,
               'memory_sha256': hashlib.sha256(target.memory).hexdigest()}
        with journal.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row) + '\n')
runtime._dispatch = observe
runtime.PyOCDSWDInterface = lambda: hardware.BoundaryAdapter(target)
with patch.object(swd_pyocd, 'FileProgrammer', side_effect=programmer):
    provider_worker.main()
"""


def _loaded_files():
    rows = []
    for name, module in tuple(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if path and (name.startswith("pyocd_debug_mcp") or name.startswith("tests.")):
            resolved = Path(path).resolve()
            rows.append(
                {
                    "module": name,
                    "path": str(resolved),
                    "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
                }
            )
    return rows


class RawFlashBulkWorkTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = self.stack.enter_context(isolated_project())
        self.server = self.stack.enter_context(tiered_test_server(self.root))
        self.wire, self.frames, self.workers, self.phases = [], [], [], []
        self.offset = 0.0
        self.phase_seconds = (110.0, 110.0)
        self.pending = set()
        self.control = self.root / "control"
        self.control.mkdir()
        self.worker_journal = self.root / "worker.jsonl"
        (self.root / "worker.py").write_text(WORKER, encoding="utf-8")
        self.sealed_inputs = []
        inventory = ValidationInventory(probes=(ValidationProbe(UID, "Controlled", "stlink", UID),))
        self.stack.enter_context(
            patch.object(self.server, "_validation_inventory", return_value=inventory)
        )
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
            patch.object(
                swd_process,
                "time",
                SimpleNamespace(monotonic=lambda: time.monotonic() + self.offset),
            )
        )
        original_write, original_read = (
            swd_process._WorkerClient._write,
            swd_process._WorkerClient._read,
        )

        def write(client, frame, deadline):
            from pyocd_debug_mcp.kernel.operations import current_operation

            request = json.loads(frame)
            active = current_operation()
            self.frames.append(
                {
                    "request": request,
                    "deadline": deadline,
                    "start": active.started_at if active else None,
                    "budget": active.timeout_seconds if active else None,
                }
            )
            if (
                request["operation"] == "flash"
                and Path(request["arguments"]["path"]).stat().st_size == SIZE
            ):
                self.pending.add(client)
            return original_write(client, frame, deadline)

        def read(client, deadline):
            if client not in self.pending:
                return original_read(client, deadline)
            self.pending.remove(client)
            try:
                for name, seconds in zip(("programmed", "readback-complete"), self.phase_seconds):
                    self.await_barrier(name)
                    self.offset += seconds
                    self.phases.append(
                        {"phase": name, "elapsed": self.offset, "deadline": deadline}
                    )
                    # Exercise the real provider's expiry decision while the
                    # child remains held at the corresponding native-I/O boundary.
                    swd_process._remaining(deadline)
                    (self.control / (name + ".release")).write_text("released")
                return original_read(client, deadline)
            finally:
                self.offset = 0.0

        self.stack.enter_context(patch.object(swd_process._WorkerClient, "_write", write))
        self.stack.enter_context(patch.object(swd_process._WorkerClient, "_read", read))

    def await_barrier(self, name):
        deadline = time.monotonic() + 10
        while not (self.control / name).exists():
            if time.monotonic() >= deadline:
                self.fail(f"worker never reached {name}")
            time.sleep(0.005)

    def call(self, name, **arguments):
        arguments = {"board_id": BOARD, **arguments}
        reply = asyncio.run(self.server.mcp.call_tool(name, arguments))

        def document(value):
            if hasattr(value, "model_dump"):
                return value.model_dump(mode="json")
            if isinstance(value, (list, tuple)):
                return [document(item) for item in value]
            return value

        row = {
            "tool": name,
            "arguments": arguments,
            "sdk_reply": document(reply),
            "sdk_type": type(reply).__name__,
            "isError": getattr(reply, "isError", None),
        }
        self.wire.append(row)
        row["primary"] = _content_text(reply)
        return row["primary"]

    def seal(self, path):
        if journal := os.environ.get("RAW_FLASH_BUDGET_JOURNAL"):
            destination = (
                Path(journal).parent
                / "fixtures"
                / self.id()
                / f"{len(self.sealed_inputs):02d}-{path.name}"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            data = path.read_bytes()
            destination.write_bytes(data)
            self.sealed_inputs.append(
                {
                    "path": str(path),
                    "snapshot": str(destination),
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )

    def connect(self, mode="write"):
        self.seal(self.root / "worker.py")
        backend = swd_process.ProcessIsolatedSWDInterface(
            worker_argv=(
                sys.executable,
                str(self.root / "worker.py"),
                str(Path(__file__).resolve().parents[1]),
                mode,
                str(self.control),
                str(self.worker_journal),
            )
        )
        self.server.configure_tiered_test_seams(backend=backend)
        self.assertIn("Connected", self.call("connect", probe_uid=UID, target="stm32l476rgtx"))
        worker = self.server.connection_manager.connection_for(BOARD).handle.worker
        self.workers.append((worker, worker._process.pid, _start_token(worker._process.pid)))
        return worker

    def flash(self, size=SIZE):
        # Include a long all-FF addressed suffix; it must still be read in full.
        data = bytes(range(256)) * (size // 512) + b"\xff" * (size - 256 * (size // 512))
        self.expected = data
        image = self.root / "full.bin"
        image.write_bytes(data)
        self.seal(image)
        return json.loads(self.call("flash_raw", artifact=str(image), base_address=BASE))

    def rows(self):
        return [json.loads(line) for line in self.worker_journal.read_text().splitlines()]

    def tearDown(self):
        try:
            if self.server.connection_manager.maybe_connection(BOARD) is not None:
                self.call("disconnect")
            self.server._monitor.closeout("bulk work test finished")
        finally:
            if path := os.environ.get("RAW_FLASH_BUDGET_JOURNAL"):
                record = {
                    "test": self.id(),
                    "wire": self.wire,
                    "frames": self.frames,
                    "phases": self.phases,
                    "sealed_inputs": self.sealed_inputs,
                    "live_imports": _loaded_files(),
                    "live_server_identity": {
                        "module": self.server.__name__,
                        "path": self.server.__file__,
                        "sha256": hashlib.sha256(
                            Path(self.server.__file__).read_bytes()
                        ).hexdigest(),
                    },
                    "worker_rows": self.rows() if self.worker_journal.exists() else [],
                    "workers": [
                        {
                            "pid": pid,
                            "birth": birth,
                            "exit": worker._process.poll(),
                            "cleanup": worker._cleanup_confirmed,
                            "marker": str(worker._marker),
                        }
                        for worker, pid, birth in self.workers
                    ],
                }
                with Path(path).open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record) + "\n")

    def test_bulk_program_and_complete_readback_share_finite_work_budget(self):
        self.connect()
        result = self.flash()
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["result"]["programming"]["verified_bytes"], SIZE)
        self.assertEqual(result["result"]["target_state"], "running")
        frame = next(row for row in self.frames if row["request"]["operation"] == "flash")
        self.assertGreater(frame["deadline"] - frame["start"], 220.0)
        self.assertLess(frame["budget"], 600.0)
        self.assertAlmostEqual(frame["start"] + frame["budget"] - frame["deadline"], 1.0)
        row = next(row for row in self.rows() if row["opcode"] == "flash")
        self.assertEqual(row["program_calls"], 1)
        self.assertEqual(row["memory_sha256"], hashlib.sha256(self.expected).hexdigest())
        self.assertEqual(
            row["reads"],
            [[BASE + offset, min(4096, SIZE - offset)] for offset in range(0, SIZE, 4096)],
        )
        # The same worker supports an ordinary read and a healthy small flash.
        self.assertEqual(json.loads(self.call("read_memory_raw", address=BASE))["status"], "ok")
        ordinary = self.frames[-1]
        self.assertEqual(ordinary["budget"], 30.0)
        self.assertAlmostEqual(ordinary["deadline"] - ordinary["start"], 29.0)
        self.assertEqual(self.flash(8193)["status"], "ok")
        small = next(row for row in reversed(self.frames) if row["request"]["operation"] == "flash")
        self.assertEqual(small["budget"], 120.0)
        self.assertAlmostEqual(small["deadline"] - small["start"], 119.0)
        self.assertEqual(operation_manager.snapshots(), ())

    def test_last_addressed_ff_byte_corruption_remains_failure_without_retry(self):
        self.connect("corrupt")
        result = self.flash()
        self.assertEqual(
            (result["status"], result["code"]), ("error", "flash/verification-failed"), result
        )
        self.assertIn(f"0x{BASE + SIZE - 1:08X}", result["message"])
        row = next(row for row in self.rows() if row["opcode"] == "flash")
        self.assertEqual(row["program_calls"], 1)
        self.assertEqual(sum(count for _, count in row["reads"]), SIZE)

    def test_true_expiry_terminates_original_worker_and_successor_reads(self):
        original = self.connect()
        self.phase_seconds = (1_000_000.0, 110.0)
        result = self.flash()
        self.assertEqual(
            (result["status"], result["code"]), ("error", "backend/flash-failed"), result
        )
        self.assertIn("TimeoutError", result["message"])
        self.assertIn("terminated", result["message"])
        self.assertIsNone(self.server.connection_manager.maybe_connection(BOARD))
        self.assertIsNotNone(original._process.poll())
        self.assertTrue(original._cleanup_confirmed)
        self.assertIsNone(original._marker)
        self.assertEqual(operation_manager.snapshots(), ())
        successor = self.connect()
        self.assertIsNot(successor, original)
        self.assertEqual(json.loads(self.call("read_memory_raw", address=BASE))["status"], "ok")


class RawFlashWorkAccountingTests(unittest.TestCase):
    def test_decoded_sparse_formats_and_bin_have_equal_work_without_counting_holes(self):
        with isolated_project() as root:
            payload = bytes(range(256)) * 4096
            runs = ((BASE, payload[: SIZE // 2]), (BASE + 2 * SIZE, payload[SIZE // 2 :]))
            budgets = []
            for suffix in (".bin", ".elf", ".hex"):
                path = write_artifact(
                    root / ("image" + suffix), ((BASE, payload),) if suffix == ".bin" else runs
                )
                arguments = {
                    "artifact": str(path),
                    **({"base_address": hex(BASE)} if suffix == ".bin" else {}),
                }
                budgets.append(operation_timeout_seconds("flash_raw", arguments))
            self.assertGreater(budgets[0], 220.0)
            self.assertEqual(budgets, [budgets[0]] * 3)
            self.assertEqual(
                operation_timeout_seconds(
                    "action_batch",
                    {
                        "actions": [
                            {"tool_name": "flash_raw", "arguments": arguments},
                            {
                                "tool_name": "read_memory_raw",
                                "arguments": {"board_id": BOARD, "address": BASE},
                            },
                        ]
                    },
                ),
                budgets[0] + 35.0,
            )


if __name__ == "__main__":
    unittest.main()
