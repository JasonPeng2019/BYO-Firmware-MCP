"""Public FULL plans and real process/pyOCD dispatch with synthetic native I/O.

The retained M16 fixture owns public setup, factory research and partition
authority. Its research and register values are synthetic host inputs. A fresh
public reconnect uses the real process provider and adapter for all flash work.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.adapters import swd_process
from pyocd_debug_mcp.kernel.operations import current_operation, operation_manager
from pyocd_debug_mcp.kernel.processes import _start_token
from tests import m16_independent_support as partition
from tests.m02_independent_support import pattern, write_artifact
from tests.m05_public_support import NULL_PLAN
from tests.test_raw_flash_bulk_work import _loaded_files


SIZE = 1048576
BOOT_SIZE = SIZE * 3 // 4


def worker_main():
    """Substitute only the probe/session/programmer boundary in a real worker."""
    from pyocd_debug_mcp.adapters import provider_worker, provider_worker_runtime, swd_pyocd
    from pyocd_debug_mcp import probe_inventory
    from tests import m02_independent_support as hardware

    root, control, journal = map(Path, sys.argv[1:4])
    factory = partition.research(control, "builtin")
    probe = partition.PartitionProbe(root, "builtin", factory)
    hardware.BASE, hardware.SIZE = 0, SIZE
    target = hardware.MemoryBoundary()
    target.memory[:8] = struct.pack("<II", 0x20001000, 0x101)
    target.part_number = "nrf52840"
    original_programmer, original_read = target.programmer, target.read_memory_block8
    expected_count = completed = 0

    def barrier(name):
        (control / name).write_text(
            json.dumps(
                {
                    "program_calls": sum(c[0] == "program" for c in target.calls),
                    "completed_read_bytes": completed,
                }
            )
        )
        deadline = time.monotonic() + 15
        while not (control / (name + ".release")).exists():
            if time.monotonic() > deadline:
                raise RuntimeError("unreleased guarded test barrier " + name)
            time.sleep(0.005)

    def programmer(session, **options):
        inner = original_programmer(session, **options)

        def program(path, **kwargs):
            nonlocal expected_count, completed
            expected_count = sum(len(data) for _, data in hardware.separate_loads(Path(path)))
            completed = 0
            inner.program(path, **kwargs)
            if expected_count >= BOOT_SIZE:
                barrier("programmed")

        return SimpleNamespace(program=program)

    def read(address, count):
        nonlocal completed
        data = original_read(address, count)
        completed += count
        if expected_count >= BOOT_SIZE and completed == expected_count:
            barrier("readback-complete")
        return data

    target.read_memory_block8 = read

    class SyntheticProbe:
        def __init__(self, uid):
            self.unique_id = uid
            self.description = "synthetic J-Link I/O boundary"

        def assert_reset(self, asserted):
            target.reset_line.append(asserted)

    class Adapter(hardware.BoundaryAdapter):
        def open(self, **kwargs):
            return replace(
                probe.open(**kwargs),
                session=SimpleNamespace(target=target, probe=SyntheticProbe(kwargs["unique_id"])),
            )

        def read_memory(self, handle, address, width_bits, **kwargs):
            if 0 <= address < SIZE:
                return target.read_memory(address, width_bits, **kwargs)
            return probe.read_memory(handle, address, width_bits, **kwargs)

    dispatch = provider_worker_runtime._dispatch

    def observed(adapter, handle, opcode, arguments):
        try:
            return dispatch(adapter, handle, opcode, arguments)
        finally:
            record = {
                "opcode": opcode,
                "program_calls": sum(c[0] == "program" for c in target.calls),
                "reads": target.reads,
                "memory_sha256": hashlib.sha256(target.memory).hexdigest(),
                "state": target.state,
                "imports": _loaded_files(),
            }
            with journal.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")

    provider_worker_runtime._dispatch = observed
    provider_worker_runtime.PyOCDSWDInterface = lambda: Adapter(target)
    with (
        patch.object(swd_pyocd, "FileProgrammer", side_effect=programmer),
        patch.dict(probe_inventory.PROBE_CLASSES, {"jlink": SyntheticProbe}, clear=True),
    ):
        provider_worker.main()


@contextmanager
def process_fixture(test):
    with partition.full_fixture(test, "builtin") as base_fixture:
        f = cast(Any, base_fixture)  # This fixture adds process-observation fields.
        control = f.root / "bulk-control"
        control.mkdir()
        journal = f.root / "bulk-worker.jsonl"
        launch = f.root / "bulk-worker.py"
        launch.write_text(
            f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
            "from tests.test_guarded_flash_bulk_work import worker_main\nworker_main()\n",
            encoding="utf-8",
        )
        f.frames, f.workers, f.phases = [], [], []
        f.bulk_paths = set()
        f.phase_seconds = (110.0, 110.0)
        offset = 0.0
        pending = set()
        write, read = swd_process._WorkerClient._write, swd_process._WorkerClient._read

        def observed_write(client, frame, deadline):
            request = json.loads(frame)
            active = current_operation()
            f.frames.append(
                {
                    "request": request,
                    "deadline": deadline,
                    "start": active.started_at if active else None,
                    "budget": active.timeout_seconds if active else None,
                    "tool": active.tool_name if active else None,
                }
            )
            if request["operation"] == "flash" and request["arguments"]["path"] in f.bulk_paths:
                for path in control.iterdir():
                    if path.name in {
                        "programmed",
                        "programmed.release",
                        "readback-complete",
                        "readback-complete.release",
                    }:
                        path.unlink()
                pending.add(client)
            return write(client, frame, deadline)

        def observed_read(client, deadline):
            nonlocal offset
            if client not in pending:
                return read(client, deadline)
            pending.remove(client)
            try:
                for name, seconds in zip(("programmed", "readback-complete"), f.phase_seconds):
                    barrier = control / name
                    limit = time.monotonic() + 10
                    while not barrier.exists():
                        if time.monotonic() > limit:
                            raise AssertionError("guarded worker missed " + name)
                        time.sleep(0.005)
                    offset += seconds
                    f.phases.append(
                        {
                            "phase": name,
                            "elapsed": offset,
                            "native_io": json.loads(barrier.read_text()),
                        }
                    )
                    swd_process._remaining(deadline)
                    (control / (name + ".release")).write_text("released")
                return read(client, deadline)
            finally:
                offset = 0.0

        backend = swd_process.ProcessIsolatedSWDInterface(
            worker_argv=(sys.executable, str(launch), str(f.root), str(control), str(journal))
        )

        def reconnect():
            f.server.configure_tiered_test_seams(backend=backend)
            test.assertIn("Connected", f.reconnect())
            worker = f.server.connection_manager.connection_for(f.board).handle.worker
            f.workers.append((worker, worker._process.pid, _start_token(worker._process.pid)))
            test.assertEqual(f.validate()["status"], "validation_passed")
            return worker

        def rows():
            return [json.loads(line) for line in journal.read_text().splitlines()]

        f.process_reconnect, f.worker_rows = reconnect, rows
        f.call("disconnect", {"board_id": f.board})
        with (
            patch.object(
                swd_process, "time", SimpleNamespace(monotonic=lambda: time.monotonic() + offset)
            ),
            patch.object(swd_process._WorkerClient, "_write", observed_write),
            patch.object(swd_process._WorkerClient, "_read", observed_read),
        ):
            reconnect()
            try:
                yield f
            finally:
                if f.server.connection_manager.maybe_connection(f.board) is not None:
                    f.call("disconnect", {"board_id": f.board})
                destination = (
                    Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / test.id() / "bulk-process"
                )
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "process-proof.json").write_text(
                    json.dumps(
                        {
                            "frames": f.frames,
                            "phases": f.phases,
                            "imports": _loaded_files(),
                            "workers": [
                                {
                                    "pid": pid,
                                    "birth": birth,
                                    "exit": worker._process.poll(),
                                    "cleanup": worker._cleanup_confirmed,
                                    "marker": str(worker._marker),
                                }
                                for worker, pid, birth in f.workers
                            ],
                        },
                        indent=2,
                    )
                )


class GuardedFlashBulkWorkTests(unittest.TestCase):
    def artifact(self, f, suffix=".elf", size=SIZE):
        data = struct.pack("<II", 0x20001000, 0x101) + bytes(range(256)) * ((size - 8) // 512)
        data += b"\xff" * (size - len(data))
        path = write_artifact(
            f.root / ("bulk" + suffix if size >= BOOT_SIZE else "small" + suffix), ((0, data),)
        )
        if suffix == ".hex":
            write_artifact(path.with_suffix(".elf"), ((0, data),))
        if size >= BOOT_SIZE:
            f.bulk_paths.add(str(path))
        return path, data

    def plan(self, f, action, path, permission=None):
        initialized = f.flow.call(action + "-plan", NULL_PLAN)
        accepted = f.flow.plan(action, f.board, {"artifact": str(path)}, permission=permission)
        return initialized, accepted

    def execute(self, f, accepted, fallback=False):
        reply = error = None
        try:
            call = accepted["stable_client_fallback"] if fallback else accepted["preferred_call"]
            reply = (
                f.flow.execute(call) if fallback else f.call(call["tool_name"], call["arguments"])
            )
        except Exception as exc:
            # Assert outside the exception handler: frozen typed backend errors
            # must not become unittest's exception-formatting failure instead.
            error = f"{type(exc).__name__}: {exc}"
        return reply, error

    def assert_disclosure_and_frame(self, f, initialized, accepted, action, size):
        expected = max(120.0, 30.0 + 2 * size / 8192)
        self.assertIn("120", initialized)
        self.assertIn("minimum", initialized)
        self.assertEqual(accepted["timeout_seconds"], expected)
        self.assertIn(f"{expected:g}s", accepted["guidance"])
        self.assertEqual(accepted["total_calls"], 1)
        frame = next(row for row in reversed(f.frames) if row["request"]["operation"] == "flash")
        self.assertEqual((frame["tool"], frame["budget"]), (action, expected))
        self.assertAlmostEqual(frame["deadline"] - frame["start"], expected - 1)
        preferred = accepted["preferred_call"]
        fallback = accepted["stable_client_fallback"]
        self.assertEqual(fallback["arguments"], {"board_id": f.board, "actions": [preferred]})

    def test_application_direct_bulk_small_and_changed_artifact_keep_one_call_binding(self):
        with process_fixture(self) as f:
            artifact, data = self.artifact(f)
            initialized, accepted = self.plan(f, "flash_application", artifact)
            reply, error = self.execute(f, accepted)
            self.assertIsNone(error, error)
            self.assertIn(f"{SIZE} matching addressed bytes", str(reply))
            self.assert_disclosure_and_frame(f, initialized, accepted, "flash_application", SIZE)
            flash = next(row for row in f.worker_rows() if row["opcode"] == "flash")
            self.assertEqual(flash["program_calls"], 1)
            self.assertEqual(flash["memory_sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(flash["reads"], [[offset, 4096] for offset in range(0, SIZE, 4096)])
            _, error = self.execute(f, accepted)
            assert isinstance(error, str)
            self.assertIn("locked", error)
            self.assertEqual(sum(row["request"]["operation"] == "flash" for row in f.frames), 1)

            small, _ = self.artifact(f, size=8193)
            initialized, accepted_small = self.plan(f, "flash_application", small)
            reply, error = self.execute(f, accepted_small)
            self.assertIsNone(error, error)
            self.assertIn("8193 matching addressed bytes", str(reply))
            self.assert_disclosure_and_frame(
                f, initialized, accepted_small, "flash_application", 8193
            )
            f.call("get_state", {"board_id": f.board})
            ordinary = f.frames[-1]
            self.assertEqual(ordinary["budget"], 30.0)
            self.assertAlmostEqual(ordinary["deadline"] - ordinary["start"], 29.0)

            _, changed_plan = self.plan(f, "flash_application", artifact)
            artifact.write_bytes(data[:-1] + bytes([data[-1] ^ 0x80]))
            calls = sum(row["request"]["operation"] == "flash" for row in f.frames)
            _, error = self.execute(f, changed_plan)
            assert isinstance(error, str)
            self.assertIn("changed", error)
            self.assertIn("replacement plan", error)
            self.assertEqual(sum(row["request"]["operation"] == "flash" for row in f.frames), calls)
            self.assertFalse(f.server.tool_registry.is_unlocked("flash_application", f.board))
            self.assertEqual(operation_manager.snapshots(), ())

    def test_permission_approved_bootloader_bulk_fallback_and_true_expiry_cleanup(self):
        with process_fixture(self) as f:
            # Public admission requires a proper subset of physical flash.
            # Leave the final quarter outside the synthetic bootloader.
            f.factory.request.update(start=0, end=BOOT_SIZE)
            self.assertEqual(f.admit()["status"], "safety_refresh_completed")
            self.assertEqual(f.validate()["status"], "validation_passed")
            self.assertEqual(f.caps()["identity"]["capability"], "exact")
            artifact, data = self.artifact(f, ".hex", size=BOOT_SIZE)
            f.phase_seconds = (90.0, 90.0)
            refusal = None
            try:
                self.plan(f, "flash_bootloader", artifact)
            except Exception as exc:
                refusal = str(exc)
            assert isinstance(refusal, str)
            self.assertIn("approval", refusal)
            self.assertFalse(any(row["request"]["operation"] == "flash" for row in f.frames))
            initialized, accepted = self.plan(
                f, "flash_bootloader", artifact, permission="one-time"
            )
            reply, error = self.execute(f, accepted, fallback=True)
            self.assertIsNone(error, error)
            self.assertIn("batch_completed", str(reply))
            self.assertIn(f"{BOOT_SIZE} matching addressed bytes", str(reply))
            self.assert_disclosure_and_frame(
                f, initialized, accepted, "flash_bootloader", BOOT_SIZE
            )
            flash = next(row for row in f.worker_rows() if row["opcode"] == "flash")
            self.assertEqual(flash["program_calls"], 1)
            self.assertEqual(
                flash["memory_sha256"],
                hashlib.sha256(data + pattern(SIZE, 213)[BOOT_SIZE:]).hexdigest(),
            )
            self.assertEqual(sum(count for _, count in flash["reads"]), BOOT_SIZE)

            _, accepted_hang = self.plan(f, "flash_bootloader", artifact, permission="one-time")
            original = f.workers[-1][0]
            f.phase_seconds = (1_000_000.0, 110.0)
            _, error = self.execute(f, accepted_hang)
            assert isinstance(error, str)
            self.assertIn("TimeoutError", error)
            self.assertIn("terminated", error)
            self.assertIsNone(f.server.connection_manager.maybe_connection(f.board))
            self.assertIsNotNone(original._process.poll())
            self.assertTrue(original._cleanup_confirmed)
            self.assertIsNone(original._marker)
            self.assertEqual(operation_manager.snapshots(), ())
            successor = f.process_reconnect()
            self.assertIsNot(successor, original)
            self.assertIn("halted", f.call("get_state", {"board_id": f.board}).lower())


if __name__ == "__main__":
    unittest.main()
