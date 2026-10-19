"""Independent M02 readback cleanup via a multi-child public LITE batch.

The two LITE profiles and readiness stamps are explicitly synthetic preconditions,
not public FULL setup or hardware proof. Public dispatch, operations, process RPC,
the production adapter, readback oracle and captured-session cleanup remain real.
Only native probe/session/programmer I/O is substituted. Earlier helpers stay read-only.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.adapters import swd_process
from pyocd_debug_mcp.capabilities.policy import Tier
from pyocd_debug_mcp.hardware_inventory import snapshot_from_validation_inventory
from pyocd_debug_mcp.kernel import processes
from pyocd_debug_mcp.kernel.operations import current_operation, operation_manager
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from tests import m02_independent_support as hardware
from tests.tiered_acceptance_support import (
    _content_text,
    assert_lite_raw_warning,
    confirmed_lite_policy,
    fixture,
    isolated_project,
    tiered_test_server,
    write_json,
)

BASE = 0x1000
PAYLOAD = hashlib.shake_256(b"M02-T independent nonzero BIN oracle").digest(8192) + b"\xff" * 17
READS = [[BASE, 4096], [BASE + 4096, 4096], [BASE + 8192, 17]]
BOARDS = ("readback_owner", "readback_neighbor")


def imports():
    rows = {}
    for name, module in tuple(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if filename and name.startswith(("pyocd_debug_mcp", "tests.", "pyocd.")):
            path = Path(filename).resolve()
            rows[name] = dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    return rows


def worker_main():
    """Native boundary only: program actual BIN bytes, then lose the final read."""
    from pyocd_debug_mcp.adapters import provider_worker, provider_worker_runtime, swd_pyocd

    control = Path(sys.argv[1])
    hardware.BASE = 0
    target = hardware.MemoryBoundary()
    target.part_number = "nrf52840"
    mode_file = control / "mode"
    read = target.read_memory_block8

    def append(row):
        with (control / "worker.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    class Probe:
        def __init__(self, uid):
            self.unique_id = uid

        def assert_reset(self, asserted):
            target.reset_line.append(asserted)
            append(dict(event="probe-reset", asserted=asserted, success=True))

    probe = Probe(control.name)

    class Adapter(hardware.BoundaryAdapter):
        def open(self, **kwargs):
            handle = super().open(**kwargs)
            handle.session.probe = probe
            return handle

    def programmer(session, **options):
        assert session.target is target and options == {"chip_erase": "sector"}

        def program(filename, **arguments):
            assert arguments == {"base_address": BASE}
            data = Path(filename).read_bytes()
            target.calls.append(("program", filename, arguments))
            target.memory[BASE : BASE + len(data)] = data
            if mode_file.read_text() == "mismatch":
                target.memory[BASE + len(data) - 1] ^= 1
            append(dict(event="program", bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))

        return SimpleNamespace(program=program)

    def native_read(address, count):
        data = read(address, count)
        if address == BASE + 8192:
            mode = mode_file.read_text()
            if mode == "loss":
                # This is a successful native I/O call after programming and at
                # the read fault, not a substituted cleanup/reset callback.
                probe.assert_reset(False)
                append(dict(event="read-loss", address=address, count=count))
                raise OSError("independent native read lost with reset control still usable")
            if mode == "short":
                return data[:-1]
        return data

    target.read_memory_block8 = native_read
    dispatch, send = provider_worker_runtime._dispatch, provider_worker_runtime._send

    def observe(adapter, handle, opcode, arguments):
        before = len(target.reads)
        programs = sum(c[0] == "program" for c in target.calls)
        try:
            return dispatch(adapter, handle, opcode, arguments)
        finally:
            append(
                dict(
                    opcode=opcode,
                    arguments=arguments,
                    reads=target.reads[before:],
                    programs=sum(c[0] == "program" for c in target.calls) - programs,
                    memory_sha256=hashlib.sha256(target.memory).hexdigest(),
                    pid=os.getpid(),
                    birth=processes._start_token(os.getpid()),
                    imports=imports(),
                )
            )

    def observe_send(protocol, reply):
        append(dict(rpc_reply=reply))
        return send(protocol, reply)

    provider_worker_runtime.PyOCDSWDInterface = lambda: Adapter(target)
    provider_worker_runtime._dispatch = observe
    provider_worker_runtime._send = observe_send
    with patch.object(swd_pyocd, "FileProgrammer", side_effect=programmer):
        provider_worker.main()


@contextmanager
def batch_fixture(test):
    with isolated_project() as root, tiered_test_server(root) as server:
        f = SimpleNamespace(
            root=root, server=server, workers=[], observations=[], frames=[], wire=[]
        )
        inventory = ValidationInventory(
            probes=tuple(ValidationProbe(board, "Synthetic", "jlink", board) for board in BOARDS)
        )
        launch = root / "independent-readback-worker.py"
        project = Path(__file__).resolve().parents[1]
        launch.write_text(
            f"import sys\nsys.path[:0] = {[str(project / 'src'), str(project)]!r}\n"
            "from tests.test_m02_readback_batch_contract import worker_main\nworker_main()\n",
            encoding="utf-8",
        )
        image = root / "independent.bin"
        image.write_bytes(PAYLOAD)

        def call(tool, arguments):
            reply = asyncio.run(server.mcp.call_tool(tool, arguments))

            def document(value):
                if hasattr(value, "model_dump"):
                    return value.model_dump(mode="json")
                if isinstance(value, (tuple, list)):
                    return [document(v) for v in value]
                return value

            f.wire.append(dict(tool=tool, arguments=arguments, reply=document(reply)))
            return _content_text(reply)

        def connect(board):
            control = root / (board + "-" + str(len(f.workers)))
            control.mkdir()
            (control / "mode").write_text("match")
            argv = (sys.executable, str(launch), str(control))
            server.configure_tiered_test_seams(
                backend=swd_process.ProcessIsolatedSWDInterface(worker_argv=argv)
            )
            test.assertIn("Connected", call("connect", dict(board_id=board)))
            assigned = server.connection_manager.connection_for(board)
            test.assertEqual(assigned.handle.probe_uid, board)
            worker = assigned.handle.worker
            marker = worker._marker
            row = dict(
                board=board,
                pid=worker._process.pid,
                birth=processes._start_token(worker._process.pid),
                argv=worker._process.args,
                marker_path=str(marker),
                marker=json.loads(marker.read_text()),
                held_job=processes._WINDOWS_JOB_HANDLES.get(worker._process.pid),
                control=control.name,
                connection_id=assigned.connection_id,
            )
            f.workers.append((worker, row))
            return assigned, control

        def stamp(board, assigned):
            return server.gate_manager.stamp_validation(
                board_id=board,
                connection_id=assigned.connection_id,
                probe_identity=assigned.handle.probe_uid,
                observed_mcu="synthetic-LITE",
                validation_run="independent-readback-LITE-precondition",
                map_digest="synthetic-LITE-map",
            )

        original_write = swd_process._WorkerClient._write

        def observe_write(client, frame, deadline):
            active = current_operation()
            f.frames.append(
                dict(
                    request=json.loads(frame),
                    deadline=deadline,
                    timeout=active.timeout_seconds if active else None,
                )
            )
            return original_write(client, frame, deadline)

        f.call, f.connect, f.stamp, f.image = call, connect, stamp, image

        def probe_listing(argv):
            test.assertEqual(argv[-2:], ["list", "--probes"])
            return (
                0,
                "\n".join(f"{i}  J-Link Synthetic  {board}" for i, board in enumerate(BOARDS)),
                "",
            )

        with (
            patch.object(server, "_run_cmd", side_effect=probe_listing),
            patch.object(
                server,
                "_hardware_inventory",
                SimpleNamespace(
                    snapshot=lambda: snapshot_from_validation_inventory(inventory),
                    validation_inventory=lambda: inventory,
                ),
            ),
            patch.object(server, "_validation_inventory", return_value=inventory),
            patch.object(swd_process._WorkerClient, "_write", observe_write),
        ):
            for board in BOARDS:
                profile = fixture("legacy_states.json")["recognized_initial_remnant"]["profile"]
                profile = profile | dict(board_id=board, probe_hint_terms=[board])
                write_json(root / ".firm/boards" / (board + ".json"), profile)
                server._capability_policies.commit(
                    board,
                    Tier.SETUP_LITE,
                    profile_snapshot=profile,
                    map_snapshot=confirmed_lite_policy(board),
                )
            try:
                yield f
            finally:
                for board in tuple(server.connection_manager.assigned_board_ids()):
                    call("disconnect", dict(board_id=board))
                destination = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / test.id()
                destination.mkdir(parents=True, exist_ok=True)
                # Preserve actual child journals, launch bytes, inputs and .firm
                # while every temporary path still exists, even on assertion failure.
                shutil.copytree(root, destination / "project", dirs_exist_ok=True)
                (destination / "proof.json").write_text(
                    json.dumps(
                        dict(
                            wire=f.wire,
                            frames=f.frames,
                            observations=f.observations,
                            imports=imports(),
                            workers=[
                                row
                                | dict(
                                    exit=w._process.poll(),
                                    cleanup=w._cleanup_confirmed,
                                    marker_exists=Path(row["marker_path"]).exists(),
                                    job_now=processes._WINDOWS_JOB_HANDLES.get(row["pid"]),
                                )
                                for w, row in f.workers
                            ],
                        ),
                        indent=2,
                    ),
                    encoding="utf-8",
                )


class IndependentReadbackBatchContract(unittest.TestCase):
    def rows(self, control):
        return [json.loads(line) for line in (control / "worker.jsonl").read_text().splitlines()]

    def assert_live(self, f, board, assigned, stamp):
        worker = assigned.handle.worker
        self.assertIs(f.server.connection_manager.connection_for(board), assigned)
        self.assertIsNotNone(stamp, "readiness must exist before testing invalidation")
        self.assertEqual(f.server.gate_manager.snapshot(board), stamp)
        self.assertIsNotNone(assigned.runtime_session)
        self.assertIsNone(assigned.runtime_session.closed_at)
        self.assertIsNone(worker._process.poll(), "original worker must really be live")
        self.assertTrue(worker._marker.is_file())
        self.assertIsNotNone(processes._start_token(worker._process.pid))
        if os.name == "nt":
            self.assertIsNotNone(processes._WINDOWS_JOB_HANDLES.get(worker._process.pid))
        f.observations.append(
            dict(
                live_board=board,
                connection_id=assigned.connection_id,
                readiness=repr(stamp),
                runtime_closed=assigned.runtime_session.closed_at is not None,
                pid=worker._process.pid,
                birth=processes._start_token(worker._process.pid),
                worker_exit=worker._process.poll(),
                marker=str(worker._marker),
            )
        )

    def assert_idle(self):
        self.assertEqual(operation_manager.snapshots(), ())
        for board in BOARDS:
            self.assertFalse(cast(Any, operation_manager.worker_lock(board)).locked())

    def read(self, f, board):
        result = json.loads(f.call("read_memory_raw", dict(board_id=board, address=BASE)))
        self.assertEqual(result["status"], "ok")
        return result

    def flash(self, f):
        return dict(board_id=BOARDS[0], artifact=str(f.image), base_address=BASE)

    def assert_programmed(self, rows, *, corrupted=False):
        flash = [row for row in rows if row.get("opcode") == "flash"][-1]
        self.assertEqual(flash["programs"], 1)
        self.assertEqual(flash["reads"], READS)
        expected = bytearray(hardware.pattern(hardware.SIZE, 213))
        expected[BASE : BASE + len(PAYLOAD)] = PAYLOAD
        if corrupted:
            expected[BASE + len(PAYLOAD) - 1] ^= 1
        self.assertEqual(flash["memory_sha256"], hashlib.sha256(expected).hexdigest())
        programmed = [row for row in rows if row.get("event") == "program"][-1]
        self.assertEqual(programmed["sha256"], hashlib.sha256(PAYLOAD).hexdigest())
        self.assertEqual(programmed["bytes"], len(PAYLOAD))

    def test_readback_loss_stops_batch_evicts_original_and_preserves_neighbor_and_successor(self):
        with batch_fixture(self) as f:
            board, neighbor_board = BOARDS
            original, control = f.connect(board)
            neighbor, neighbor_control = f.connect(neighbor_board)
            stamp = f.stamp(board, original)
            neighbor_stamp = f.stamp(neighbor_board, neighbor)
            self.assert_live(f, board, original, stamp)
            self.assert_live(f, neighbor_board, neighbor, neighbor_stamp)
            marker = original.handle.worker._marker
            neighbor_before = (neighbor_control / "worker.jsonl").read_bytes()
            (control / "mode").write_text("loss")
            actions = [
                dict(tool_name="read_memory_raw", arguments=dict(board_id=board, address=BASE)),
                dict(tool_name="flash_raw", arguments=self.flash(f)),
                dict(tool_name="read_memory_raw", arguments=dict(board_id=board, address=BASE + 4)),
            ]
            result = json.loads(
                f.call("action_batch", dict(board_id=board, actions=actions)).split("\n", 1)[0]
            )
            self.assertEqual(result["status"], "batch_failed")
            self.assertEqual(result["failure"]["index"], 1)
            self.assertEqual(len(result["completed"]), 1)
            failed = result["failure"]["result"]
            failed = json.loads(failed) if isinstance(failed, str) else failed
            self.assertEqual((failed["schema_version"], failed["status"]), (1, "error"))
            self.assertEqual(failed["code"], "flash/verification-failed")
            self.assertIn("programming is not verified", failed["message"])
            assert_lite_raw_warning(self, failed, "flash_raw")
            rows = self.rows(control)
            self.assert_programmed(rows)
            events = [r for r in rows if "event" in r]
            programmed = next(i for i, r in enumerate(events) if r["event"] == "program")
            self.assertEqual(
                [r["event"] for r in events[programmed : programmed + 3]],
                ["program", "probe-reset", "read-loss"],
            )
            reset = events[programmed + 1]
            self.assertEqual((reset["asserted"], reset["success"]), (False, True))
            self.assertEqual(
                [r["arguments"]["address"] for r in rows if r.get("opcode") == "read_memory"],
                [BASE],
                "only the batch prefix may reach the worker; never the suffix",
            )
            state = dict(
                connected=f.server.connection_manager.maybe_connection(board) is not None,
                gate_cleared=f.server.gate_manager.snapshot(board) is None,
                runtime_closed=original.runtime_session.closed_at is not None,
                worker_exit=original.handle.worker._process.poll(),
                cleanup=original.handle.worker._cleanup_confirmed,
                marker_exists=marker.exists(),
            )
            f.observations.append(dict(after_fault=state))
            # Deliberately first: old-source sensitivity must fail on the actual
            # stale assignment, not an exception class, message or source match.
            self.assertFalse(state["connected"], state)
            self.assertTrue(state["gate_cleared"] and state["runtime_closed"], state)
            self.assertIsNotNone(state["worker_exit"], state)
            self.assertTrue(state["cleanup"], state)
            self.assertFalse(state["marker_exists"], state)
            self.assertIsNone(original.handle.worker._marker)
            self.assertEqual((neighbor_control / "worker.jsonl").read_bytes(), neighbor_before)
            self.assert_live(f, neighbor_board, neighbor, neighbor_stamp)
            self.read(f, neighbor_board)
            self.assert_idle()
            successor, _ = f.connect(board)
            self.assertIsNot(successor, original)
            self.assertIsNot(successor.handle.worker, original.handle.worker)
            self.assertIsNone(f.server.gate_manager.snapshot(board))
            successor_stamp = f.stamp(board, successor)  # synthetic LITE precondition again
            self.assert_live(f, board, successor, successor_stamp)
            self.read(f, board)
            self.assert_live(f, neighbor_board, neighbor, neighbor_stamp)
            self.assert_idle()

    def test_true_mismatch_and_short_reads_keep_original_usable_then_complete_match_succeeds(self):
        with batch_fixture(self) as f:
            board = BOARDS[0]
            original, control = f.connect(board)
            stamp = f.stamp(board, original)
            self.assert_live(f, board, original, stamp)
            for mode in ("mismatch", "short", "match"):
                with self.subTest(mode=mode):
                    (control / "mode").write_text(mode)
                    result = json.loads(f.call("flash_raw", self.flash(f)))
                    if mode == "match":
                        self.assertEqual(result["status"], "ok")
                    else:
                        self.assertEqual(result["code"], "flash/verification-failed")
                        self.assertIn(
                            "does not match" if mode == "mismatch" else "16 of 17",
                            result["message"],
                        )
                    assert_lite_raw_warning(self, result, "flash_raw")
                    self.assert_programmed(self.rows(control), corrupted=mode == "mismatch")
                    self.assert_live(f, board, original, stamp)
                    self.read(f, board)
                    self.assert_idle()
            self.assertEqual(sum(r.get("event") == "program" for r in self.rows(control)), 3)


if __name__ == "__main__":
    unittest.main()
