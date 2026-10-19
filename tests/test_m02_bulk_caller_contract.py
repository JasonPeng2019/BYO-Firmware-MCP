"""Independent bulk callers at real RPC and pyOCD native-I/O boundaries.

Public setup/permissions/plans, artifact parsing, provider deadlines and complete
readback run normally. Only synthetic probe/programmer I/O and the provider's
monotonic clock are controlled. File barriers represent completed work, not a
hardware throughput claim. This module never opens a physical probe.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import time
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.adapters import swd_process
from pyocd_debug_mcp.kernel.operations import (
    current_operation,
    operation_manager,
    operation_timeout_seconds,
)
from pyocd_debug_mcp.kernel.processes import _start_token
from tests import m16_independent_support as setup
from tests.m02_independent_support import pattern, write_artifact
from tests.m05_public_support import NULL_PLAN


CAPACITY = 1048576
WORK = 786432
# Independent public-contract examples: 768KiB -> 222s, full 1MiB -> 286s.
BULK_SECONDS = 222.0


def loaded():
    rows = {}
    for name, module in tuple(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if path and name.startswith(("pyocd_debug_mcp", "tests.", "pyocd.")):
            p = Path(path).resolve()
            rows[name] = {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
    return rows


def worker_main():
    from pyocd_debug_mcp.adapters import provider_worker, provider_worker_runtime, swd_pyocd
    from pyocd_debug_mcp import probe_inventory
    from tests import m02_independent_support as io

    root, control = map(Path, sys.argv[1:3])
    factory = setup.research(control, "builtin")
    identity = setup.PartitionProbe(root, "builtin", factory)
    io.BASE, io.SIZE = 0, CAPACITY
    target = io.MemoryBoundary()
    target.part_number = "nrf52840"
    target.memory[:8] = struct.pack("<II", 0x20001000, 0x101)
    program, read = target.programmer, target.read_memory_block8
    addressed = completed = 0

    def barrier(name):
        (control / name).write_text(json.dumps({"completed": completed, "addressed": addressed}))
        limit = time.monotonic() + 20
        while not (control / (name + ".release")).exists():
            if time.monotonic() >= limit:
                raise RuntimeError("independent test barrier was not released: " + name)
            time.sleep(0.005)

    def programmer(session, **options):
        inner = program(session, **options)

        def execute(path, **arguments):
            nonlocal addressed, completed
            # The retained independent parser/programmer supplies bytes, never
            # the product's decoded-work helper or timeout calculation.
            addressed = sum(
                len(data)
                for _, data in io.separate_loads(Path(path), arguments.get("base_address"))
            )
            completed = 0
            target.program_mode = (control / "mode").read_text()
            inner.program(path, **arguments)
            if addressed >= WORK:
                barrier("programmed")

        return SimpleNamespace(program=execute)

    def readback(address, count):
        nonlocal completed
        result = read(address, count)
        completed += count
        if addressed >= WORK and completed == addressed:
            barrier("verified")
        return result

    target.read_memory_block8 = readback

    class Probe:
        def __init__(self, uid):
            self.unique_id, self.description = uid, "independent synthetic probe"

        def assert_reset(self, asserted):
            target.reset_line.append(asserted)

    class Adapter(io.BoundaryAdapter):
        def open(self, **kwargs):
            return replace(
                identity.open(**kwargs),
                session=SimpleNamespace(target=target, probe=Probe(kwargs["unique_id"])),
            )

        def read_memory(self, handle, address, width_bits, **kwargs):
            if 0 <= address < CAPACITY:
                return target.read_memory(address, width_bits, **kwargs)
            return identity.read_memory(handle, address, width_bits, **kwargs)

    dispatch = provider_worker_runtime._dispatch

    def observe(adapter, handle, opcode, arguments):
        before = len(target.reads)
        programs = sum(row[0] == "program" for row in target.calls)
        try:
            return dispatch(adapter, handle, opcode, arguments)
        finally:
            row = {
                "opcode": opcode,
                "programs": sum(r[0] == "program" for r in target.calls) - programs,
                "reads": target.reads[before:],
                "state": target.state,
                "memory_sha256": hashlib.sha256(target.memory).hexdigest(),
                "pid": os.getpid(),
                "birth": _start_token(os.getpid()),
                "imports": loaded(),
            }
            with (control / "worker.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row) + "\n")

    provider_worker_runtime._dispatch = observe
    provider_worker_runtime.PyOCDSWDInterface = lambda: Adapter(target)
    with (
        patch.object(swd_pyocd, "FileProgrammer", side_effect=programmer),
        patch.dict(probe_inventory.PROBE_CLASSES, {"jlink": Probe}, clear=True),
    ):
        provider_worker.main()


@contextmanager
def fixture(test):
    with setup.full_fixture(test, "builtin") as base:
        f = cast(Any, base)
        control = f.root / "independent-io"
        control.mkdir()
        (control / "mode").write_text("write")
        launch = f.root / "independent-worker.py"
        launch.write_text(
            f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
            "from tests.test_m02_bulk_caller_contract import worker_main\nworker_main()\n",
            encoding="utf-8",
        )
        backend = swd_process.ProcessIsolatedSWDInterface(
            worker_argv=(sys.executable, str(launch), str(f.root), str(control))
        )
        frames, workers, phases = [], [], []
        pending = set()
        offset = 0.0
        f.phase_times = (70.0, 70.0)
        write, read = swd_process._WorkerClient._write, swd_process._WorkerClient._read

        def send(client, frame, deadline):
            request = json.loads(frame)
            active = current_operation()
            frames.append(
                {
                    "request": request,
                    "deadline": deadline,
                    "budget": active.timeout_seconds if active else None,
                    "start": active.started_at if active else None,
                    "tool": active.tool_name if active else None,
                }
            )
            if request["operation"] == "flash":
                for name in ("programmed", "verified", "programmed.release", "verified.release"):
                    (control / name).unlink(missing_ok=True)
                if Path(request["arguments"]["path"]).name.startswith("bulk"):
                    pending.add(client)
            return write(client, frame, deadline)

        def receive(client, deadline):
            nonlocal offset
            if client not in pending:
                return read(client, deadline)
            pending.remove(client)
            try:
                for name, elapsed in zip(("programmed", "verified"), f.phase_times):
                    limit = time.monotonic() + 15
                    while not (control / name).exists():
                        test.assertLess(time.monotonic(), limit, "worker missed " + name)
                        time.sleep(0.005)
                    observed = json.loads((control / name).read_text())
                    offset += elapsed
                    phases.append({"phase": name, "elapsed": offset, **observed})
                    # This is the real provider expiry check, held at completed
                    # native I/O. Neither RPC nor its response is synthesized.
                    swd_process._remaining(deadline)
                    (control / (name + ".release")).write_text("continue")
                return read(client, deadline)
            finally:
                offset = 0.0

        def reconnect():
            f.server.configure_tiered_test_seams(backend=backend)
            test.assertIn("Connected", f.reconnect())
            worker = f.server.connection_manager.connection_for(f.board).handle.worker
            workers.append((worker, worker._process.pid, _start_token(worker._process.pid)))
            test.assertEqual(f.validate()["status"], "validation_passed")
            return worker

        f.frames, f.workers, f.phases, f.control = frames, workers, phases, control
        f.process_connect = reconnect
        f.rows = lambda: [
            json.loads(s) for s in (control / "worker.jsonl").read_text().splitlines()
        ]
        f.call("disconnect", {"board_id": f.board})
        with (
            patch.object(
                swd_process, "time", SimpleNamespace(monotonic=lambda: time.monotonic() + offset)
            ),
            patch.object(swd_process._WorkerClient, "_write", send),
            patch.object(swd_process._WorkerClient, "_read", receive),
        ):
            reconnect()
            try:
                yield f
            finally:
                if f.server.connection_manager.maybe_connection(f.board) is not None:
                    f.call("disconnect", {"board_id": f.board})
                destination = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / test.id()
                destination.mkdir(parents=True, exist_ok=True)
                launch_copy = destination / "worker-launch.py"
                journal_copy = destination / "worker.jsonl"
                shutil.copyfile(launch, launch_copy)
                shutil.copyfile(control / "worker.jsonl", journal_copy)
                (destination / "independent-process.json").write_text(
                    json.dumps(
                        {
                            "frames": frames,
                            "phases": phases,
                            "live_imports": loaded(),
                            "live_server": {
                                "path": f.server.__file__,
                                "sha256": hashlib.sha256(
                                    Path(f.server.__file__).read_bytes()
                                ).hexdigest(),
                            },
                            "worker_launch": {
                                "path": str(launch),
                                "snapshot": str(launch_copy),
                                "sha256": hashlib.sha256(launch_copy.read_bytes()).hexdigest(),
                            },
                            "worker_journal": {
                                "path": str(control / "worker.jsonl"),
                                "snapshot": str(journal_copy),
                                "sha256": hashlib.sha256(journal_copy.read_bytes()).hexdigest(),
                            },
                            "worker_rows": f.rows(),
                            "workers": [
                                {
                                    "pid": pid,
                                    "birth": birth,
                                    "argv": w._process.args,
                                    "exit": w._process.poll(),
                                    "cleanup": w._cleanup_confirmed,
                                    "marker": str(w._marker),
                                }
                                for w, pid, birth in workers
                            ],
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )


class IndependentBulkCallerContract(unittest.TestCase):
    def plan(self, f, action, artifact, permission=None):
        guidance = f.flow.call(action + "-plan", NULL_PLAN)
        self.assertIn("minimum", guidance)
        self.assertIn("120", guidance)
        self.assertIn("addressed", guidance)
        accepted = f.flow.plan(action, f.board, {"artifact": str(artifact)}, permission=permission)
        self.assertEqual(accepted["status"], "plan_accepted")
        self.assertEqual(accepted["timeout_seconds"], BULK_SECONDS)
        self.assertIn("222s", accepted["guidance"])
        self.assertEqual(accepted["total_calls"], 1)
        self.assertEqual(
            accepted["preferred_call"],
            {"tool_name": action, "arguments": {"board_id": f.board, "artifact": str(artifact)}},
        )
        self.assertEqual(
            accepted["stable_client_fallback"],
            {
                "tool_name": "action_batch",
                "arguments": {"board_id": f.board, "actions": [accepted["preferred_call"]]},
            },
        )
        return accepted

    def execute(self, f, plan, fallback=False):
        call = plan["stable_client_fallback" if fallback else "preferred_call"]
        error = None
        reply = None
        try:
            reply = f.call(call["tool_name"], call["arguments"])
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        # Keep assertions outside the handler for the retained frozen backend exceptions.
        return reply, error

    def assert_flash(self, f, expected, runs):
        frame = next(row for row in reversed(f.frames) if row["request"]["operation"] == "flash")
        self.assertEqual(frame["budget"], BULK_SECONDS)
        self.assertAlmostEqual(frame["deadline"] - frame["start"], 221.0)
        row = next(row for row in reversed(f.rows()) if row["opcode"] == "flash")
        self.assertEqual(row["programs"], 1)
        self.assertEqual(row["memory_sha256"], hashlib.sha256(expected).hexdigest())
        self.assertEqual(
            row["reads"],
            [
                [address + offset, min(4096, len(data) - offset)]
                for address, data in runs
                for offset in range(0, len(data), 4096)
            ],
        )
        self.assertEqual(sum(n for _, n in row["reads"]), WORK)
        self.assertEqual(f.phases[-1]["completed"], WORK)
        self.assertEqual(f.phases[-1]["elapsed"], 140.0)

    def data(self):
        return struct.pack("<II", 0x20001000, 0x101) + pattern(WORK - 4104, 37) + b"\xff" * 4096

    def test_sparse_application_direct_and_fallback_match_bin_decoded_work(self):
        with fixture(self) as f:
            data = self.data()
            expected = bytearray(pattern(CAPACITY, 213))
            expected[:8] = struct.pack("<II", 0x20001000, 0x101)
            binary = write_artifact(f.root / "bulk.bin", ((0, data),))
            self.assertEqual(
                operation_timeout_seconds(
                    "flash_raw", {"artifact": str(binary), "base_address": 0}
                ),
                BULK_SECONDS,
            )
            sparse = ((0, data[: WORK // 2]), (0xA0000, data[WORK // 2 :]))
            for suffix, runs in ((".elf", sparse), (".hex", sparse)):
                artifact = write_artifact(f.root / ("bulk" + suffix), runs)
                if suffix == ".hex":
                    write_artifact(artifact.with_suffix(".elf"), runs)
                for address, values in runs:
                    expected[address : address + len(values)] = values
                self.assertEqual(
                    operation_timeout_seconds("flash_raw", {"artifact": str(artifact)}),
                    BULK_SECONDS,
                )
                plan = self.plan(f, "flash_application", artifact)
                reply, error = self.execute(f, plan, fallback=suffix == ".hex")
                self.assertIsNone(error, error)
                self.assertIn(f"{WORK} matching addressed bytes", str(reply))
                if suffix == ".hex":
                    self.assertIn("batch_completed", str(reply))
                count = sum(r["request"]["operation"] == "flash" for r in f.frames)
                _, replay = self.execute(f, plan)
                self.assertIn("locked", str(replay))
                self.assertEqual(sum(r["request"]["operation"] == "flash" for r in f.frames), count)
                self.assert_flash(f, bytes(expected), runs)

            changed = self.plan(f, "flash_application", artifact)
            before = len(f.frames)
            # Preserve valid HEX syntax/checksums: replace with a new independently
            # encoded final addressed byte and matching companion, after acceptance.
            altered = (*sparse[:-1], (sparse[-1][0], sparse[-1][1][:-1] + b"\x7f"))
            write_artifact(artifact, altered)
            write_artifact(artifact.with_suffix(".elf"), altered)
            _, error = self.execute(f, changed)
            self.assertIn("changed", str(error))
            self.assertIn("replacement plan", str(error))
            self.assertFalse(any(r["request"]["operation"] == "flash" for r in f.frames[before:]))
            self.assertFalse(f.server.tool_registry.is_unlocked("flash_application", f.board))
            self.plan(f, "flash_application", artifact)  # The advertised remedy remains usable.
            self.assertEqual(operation_manager.snapshots(), ())

    def test_bootloader_permission_ff_corruption_and_genuine_expiry_have_distinct_cleanup(self):
        with fixture(self) as f:
            f.factory.request.update(start=0, end=WORK)
            self.assertEqual(f.admit()["status"], "safety_refresh_completed")
            self.assertEqual(f.validate()["status"], "validation_passed")
            data = self.data()
            runs = ((0, data),)
            artifact = write_artifact(f.root / "bulk-boot.hex", runs)
            write_artifact(artifact.with_suffix(".elf"), runs)
            error = None
            try:
                self.plan(f, "flash_bootloader", artifact)
            except Exception as exc:
                error = str(exc)
            self.assertIn("approval", str(error))
            self.assertFalse(any(r["request"]["operation"] == "flash" for r in f.frames))
            accepted = self.plan(f, "flash_bootloader", artifact, "one-time")
            reply, error = self.execute(f, accepted, fallback=True)
            self.assertIsNone(error, error)
            self.assertIn("batch_completed", str(reply))
            self.assertIn(f"{WORK} matching addressed bytes", str(reply))
            self.assert_flash(f, data + pattern(CAPACITY, 213)[WORK:], runs)

            original = f.workers[-1][0]
            (f.control / "mode").write_text("corrupt")
            plan = self.plan(f, "flash_bootloader", artifact, "one-time")
            _, corruption = self.execute(f, plan)
            self.assertIn(f"0x{WORK - 1:08X}", str(corruption))
            row = next(r for r in reversed(f.rows()) if r["opcode"] == "flash")
            self.assertEqual(row["programs"], 1)
            self.assertEqual(sum(n for _, n in row["reads"]), WORK)
            self.assertIs(
                f.server.connection_manager.connection_for(f.board).handle.worker, original
            )
            self.assertIsNone(original._process.poll())
            self.assertIn("halted", f.call("get_state", {"board_id": f.board}).lower())

            (f.control / "mode").write_text("write")
            plan = self.plan(f, "flash_bootloader", artifact, "one-time")
            f.phase_times = (223.0, 0.0)
            _, expiry = self.execute(f, plan)
            self.assertIn("TimeoutError", str(expiry))
            self.assertIn("terminated", str(expiry))
            self.assertIsNone(f.server.connection_manager.maybe_connection(f.board))
            self.assertIsNotNone(original._process.poll())
            self.assertTrue(original._cleanup_confirmed)
            self.assertIsNone(original._marker)
            self.assertEqual(operation_manager.snapshots(), ())
            successor = f.process_connect()
            self.assertIsNot(successor, original)
            self.assertIn("halted", f.call("get_state", {"board_id": f.board}).lower())
            frame = f.frames[-1]
            self.assertEqual(frame["budget"], 30.0)
            self.assertAlmostEqual(frame["deadline"] - frame["start"], 29.0)


if __name__ == "__main__":
    unittest.main()
