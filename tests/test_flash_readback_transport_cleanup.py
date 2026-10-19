"""M02-I readback loss through public dispatch, real RPC and production flash.

Only native probe/session/programmer I/O is synthetic. Retained independent I/O
and FULL setup fixtures stay read-only. No clock/deadline or cleanup is replaced.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
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
from tests import m16_independent_support as setup
from tests.m02_independent_support import pattern, write_artifact
from tests.tiered_acceptance_support import (
    _content_text,
    assert_lite_raw_warning,
    confirmed_lite_policy,
    fixture,
    isolated_project,
    tiered_test_server,
    write_json,
)


def loaded():
    rows = {}
    for name, module in tuple(sys.modules.items()):
        source = getattr(module, "__file__", None)
        if source and name.startswith(("pyocd_debug_mcp", "tests.", "pyocd.")):
            path = Path(source).resolve()
            rows[name] = dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    return rows


def document(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (tuple, list)):
        return [document(item) for item in value]
    return value


def worker_main():
    from pyocd.core.exceptions import TransferError
    from pyocd_debug_mcp import probe_inventory
    from pyocd_debug_mcp.adapters import provider_worker, provider_worker_runtime, swd_pyocd
    from tests import m02_independent_support as io

    root, control = map(Path, sys.argv[1:3])
    factory = setup.research(control, "builtin")
    identity = setup.PartitionProbe(root, "builtin", factory)
    io.BASE = 0
    target = io.MemoryBoundary()
    target.part_number = "nrf52840"
    target.memory[:8] = struct.pack("<II", 0x20001000, 0x101)
    program, read = target.programmer, target.read_memory_block8

    def programmer(session, **kwargs):
        target.program_mode = "corrupt" if (control / "mode").read_text() == "mismatch" else "write"
        return program(session, **kwargs)

    def readback(address, count):
        data = read(address, count)
        if address == 0x4000:
            mode = (control / "mode").read_text()
            if mode == "transfer":
                raise TransferError(
                    "synthetic target read transport lost; probe reset remains live"
                )
            if mode == "oserror":
                raise OSError("synthetic native read transport lost; probe reset remains live")
            if mode == "short":
                return data[:-1]
            if mode == "refusal":
                # Transport-looking text must not turn a nontransport refusal into loss.
                raise ValueError("TransferError OSError: refused synthetic read request")
        return data

    target.read_memory_block8 = readback

    class Probe:
        def __init__(self, uid):
            self.unique_id, self.description = uid, "M02-I synthetic probe"

        def assert_reset(self, asserted):
            target.reset_line.append(asserted)  # Explicit success even after read loss.

    class Adapter(io.BoundaryAdapter):
        def open(self, **kwargs):
            return replace(
                identity.open(**kwargs),
                session=SimpleNamespace(target=target, probe=Probe(kwargs["unique_id"])),
            )

        def read_memory(self, handle, address, width_bits, **kwargs):
            if 0 <= address < io.SIZE:
                return target.read_memory(address, width_bits, **kwargs)
            return identity.read_memory(handle, address, width_bits, **kwargs)

    dispatch, send = provider_worker_runtime._dispatch, provider_worker_runtime._send

    def append(row):
        with (control / "worker.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    def observe(adapter, handle, opcode, arguments):
        before, resets = len(target.reads), len(target.reset_line)
        programs = sum(row[0] == "program" for row in target.calls)
        error = []
        reset_release_control = None
        try:
            return dispatch(adapter, handle, opcode, arguments)
        except Exception as exc:
            current = exc
            while current is not None:
                error.append(dict(type=type(current).__name__, message=str(current)))
                current = current.__cause__ or current.__context__
            if opcode == "flash":
                # A separate native-I/O control before sending the unchanged
                # failure: the real adapter can still release probe reset.
                # Production cleanup closes a lost worker before its final reset
                # callback; old code instead keeps it and releases reset normally.
                adapter.release_reset(handle)
                reset_release_control = "succeeded"
            raise
        finally:
            append(
                dict(
                    opcode=opcode,
                    arguments=arguments,
                    error=error,
                    programs=sum(row[0] == "program" for row in target.calls) - programs,
                    reads=target.reads[before:],
                    reset_line=target.reset_line[resets:],
                    reset_release_control=reset_release_control,
                    memory_sha256=hashlib.sha256(target.memory).hexdigest(),
                    pid=os.getpid(),
                    birth=processes._start_token(os.getpid()),
                    imports=loaded(),
                )
            )

    def observe_send(protocol, payload):
        append(dict(rpc_reply=payload))
        return send(protocol, payload)

    provider_worker_runtime._dispatch = observe
    provider_worker_runtime._send = observe_send
    provider_worker_runtime.PyOCDSWDInterface = lambda: Adapter(target)
    with (
        patch.object(swd_pyocd, "FileProgrammer", side_effect=programmer),
        patch.dict(probe_inventory.PROBE_CLASSES, {"jlink": Probe}, clear=True),
    ):
        provider_worker.main()


class Calls:
    def __init__(self, server):
        self.server, self.wire = server, []

    def __call__(self, name, arguments):
        row = dict(tool=name, arguments=arguments)
        self.wire.append(row)
        try:
            reply = asyncio.run(self.server.mcp.call_tool(name, arguments))
        except Exception as exc:
            row["exception"] = dict(type=type(exc).__name__, message=str(exc))
            raise
        row.update(
            sdk_type=type(reply).__name__,
            sdk_reply=document(reply),
            isError=getattr(reply, "isError", None),
        )
        row["primary"] = _content_text(reply)
        return row["primary"]


@contextmanager
def process_fixture(test, f: Any):
    control = f.root / "readback-io"
    control.mkdir()
    (control / "mode").write_text("match")
    script = f.root / "readback-worker.py"
    project = Path(__file__).resolve().parents[1]
    script.write_text(
        f"import sys\nsys.path[:0] = [{str(project / 'src')!r}, {str(project)!r}]\n"
        "from tests.test_flash_readback_transport_cleanup import worker_main\nworker_main()\n",
        encoding="utf-8",
    )
    workers, frames = [], []
    f.call = Calls(f.server)
    if hasattr(f, "flow"):
        f.flow.call = f.call
    backend = swd_process.ProcessIsolatedSWDInterface(
        worker_argv=(sys.executable, str(script), str(f.root), str(control))
    )
    if f.server.connection_manager.maybe_connection(f.board):
        f.call("disconnect", dict(board_id=f.board))
    f.server.configure_tiered_test_seams(backend=backend)
    original = swd_process._WorkerClient._write

    def write(client, frame, deadline):
        active = current_operation()
        frames.append(
            dict(
                request=json.loads(frame),
                deadline=deadline,
                budget=active.timeout_seconds if active else None,
                start=active.started_at if active else None,
            )
        )
        return original(client, frame, deadline)

    def connect():
        arguments = dict(board_id=f.board)
        if hasattr(f, "flow"):
            reply = f.reconnect()  # Public overview establishes this run's assignment.
        else:
            if f.server._tier_router.state_for(f.board).tier is Tier.NO_SETUP:
                arguments.update(probe_uid="m02-i-readback", target="nrf52840")
            reply = f.call("connect", arguments)
        test.assertIn("Connected", reply)
        connection = f.server.connection_manager.connection_for(f.board)
        worker = connection.handle.worker
        marker = worker._marker
        workers.append(
            (
                worker,
                dict(
                    pid=worker._process.pid,
                    birth=processes._start_token(worker._process.pid),
                    argv=worker._process.args,
                    marker_path=str(marker),
                    marker=json.loads(marker.read_text()),
                    held_job=processes._WINDOWS_JOB_HANDLES.get(worker._process.pid),
                ),
            )
        )
        return connection

    f.connect, f.control = connect, control
    f.rows = lambda: [
        json.loads(line) for line in (control / "worker.jsonl").read_text().splitlines()
    ]
    with patch.object(swd_process._WorkerClient, "_write", write):
        try:
            connect()
            yield f
        finally:
            if f.server.connection_manager.maybe_connection(f.board):
                f.call("disconnect", dict(board_id=f.board))
            destination = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / test.id() / "process"
            destination.mkdir(parents=True, exist_ok=True)
            # Copy raw child output and exact launch/input bytes BEFORE temp cleanup.
            shutil.copytree(control, destination / "io", dirs_exist_ok=True)
            shutil.copyfile(script, destination / script.name)
            shutil.copytree(f.root / ".firm", destination / ".firm", dirs_exist_ok=True)
            for path in f.root.glob("image.*"):
                shutil.copyfile(path, destination / path.name)
            (destination / "proof.json").write_text(
                json.dumps(
                    dict(
                        wire=f.call.wire,
                        frames=frames,
                        imports=loaded(),
                        observations=getattr(f, "observations", []),
                        workers=[
                            row
                            | dict(
                                exit=w._process.poll(),
                                cleanup=w._cleanup_confirmed,
                                marker_now=str(w._marker),
                                marker_exists=Path(row["marker_path"]).exists(),
                                job_now=processes._WINDOWS_JOB_HANDLES.get(row["pid"]),
                            )
                            for w, row in workers
                        ],
                    ),
                    indent=2,
                ),
                encoding="utf-8",
            )


class FlashReadbackTransportCleanupTests(unittest.TestCase):
    @contextmanager
    def raw(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            inventory = ValidationInventory(
                probes=(
                    ValidationProbe("m02-i-readback", "M02-I synthetic", "jlink", "m02-i-readback"),
                )
            )
            with (
                patch.object(
                    server,
                    "_hardware_inventory",
                    SimpleNamespace(
                        snapshot=lambda: snapshot_from_validation_inventory(inventory),
                        validation_inventory=lambda: inventory,
                    ),
                ),
                patch.object(server, "_validation_inventory", return_value=inventory),
            ):
                f = SimpleNamespace(root=root, server=server, board="m02_i_readback")
                # A synthetic saved profile permits the normal LITE reconnect
                # route. FULL tests below create authority through public setup.
                profile = fixture("legacy_states.json")["recognized_initial_remnant"]["profile"]
                profile = profile | dict(board_id=f.board, probe_hint_terms=["m02-i-readback"])
                write_json(
                    root / ".firm/boards" / (f.board + ".json"),
                    profile,
                )
                server._capability_policies.commit(
                    f.board,
                    Tier.SETUP_LITE,
                    profile_snapshot=profile,
                    map_snapshot=confirmed_lite_policy(f.board),
                )
                with process_fixture(self, f):
                    yield f

    def stamp(self, f):
        connection = f.server.connection_manager.connection_for(f.board)
        return f.server.gate_manager.stamp_validation(
            board_id=f.board,
            connection_id=connection.connection_id,
            probe_identity=connection.handle.probe_uid,
            observed_mcu="synthetic",
            validation_run="m02-i-synthetic-stamp",
            map_digest="synthetic-map",
        )

    def image(self, f):
        runs = (
            (0, struct.pack("<II", 0x20001000, 0x101) + pattern(4097, 67)),
            (0x4000, b"\xff" * 31),
        )
        return write_artifact(f.root / "image.elf", runs), runs

    def assert_idle(self, f):
        self.assertEqual(operation_manager.snapshots(), ())
        self.assertFalse(cast(Any, operation_manager.worker_lock(f.board)).locked())

    def assert_lost(self, f, original):
        w = original.handle.worker
        state = dict(
            connected=f.server.connection_manager.maybe_connection(f.board) is not None,
            gate_cleared=f.server.gate_manager.snapshot(f.board) is None,
            runtime_closed=original.runtime_session.closed_at is not None,
            worker_exit=w._process.poll(),
            cleanup=w._cleanup_confirmed,
            marker=str(w._marker),
        )
        f.observations = [state]
        # This is the baseline sensitivity assertion: old code keeps this assignment.
        self.assertFalse(state["connected"], state)
        self.assertTrue(state["gate_cleared"] and state["runtime_closed"], state)
        self.assertIsNotNone(state["worker_exit"], state)
        self.assertTrue(state["cleanup"], state)
        self.assertIsNone(w._marker)
        self.assert_idle(f)

    def assert_native(self, f, kind):
        rows = f.rows()
        flash = next(row for row in reversed(rows) if row.get("opcode") == "flash")
        self.assertEqual(flash["programs"], 1)
        self.assertEqual(flash["reads"], [[0, 4096], [4096, 9], [0x4000, 31]])
        self.assertEqual(flash["reset_release_control"], "succeeded")
        self.assertEqual(flash["reset_line"], [False])
        failed = [row["rpc_reply"] for row in rows if row.get("rpc_reply", {}).get("ok") is False]
        self.assertEqual(failed[-1]["error"]["kind"], kind)

    def test_raw_transport_loss_with_successful_reset_release_clears_original(self):
        with self.raw() as f:
            original = f.server.connection_manager.connection_for(f.board)
            marker = original.handle.worker._marker
            self.stamp(f)
            image, _ = self.image(f)
            (f.control / "mode").write_text("transfer")
            result = json.loads(f.call("flash_raw", dict(board_id=f.board, artifact=str(image))))
            self.assertEqual(
                (result["status"], result["code"]), ("error", "flash/verification-failed")
            )
            self.assertEqual(result["schema_version"], 1)
            self.assertIn("programming is not verified", result["message"])
            self.assertIn("0x00004000", result["message"])
            assert_lite_raw_warning(self, result, "flash_raw")
            self.assert_lost(f, original)
            self.assertFalse(marker.exists())
            self.assert_native(f, "flash_readback_connection")
            (f.control / "mode").write_text("match")
            successor = f.connect()
            self.assertIsNot(successor.handle.worker, original.handle.worker)
            self.stamp(f)
            self.assertEqual(
                json.loads(f.call("read_memory_raw", dict(board_id=f.board, address=0)))["status"],
                "ok",
            )

    def test_healthy_mismatch_short_refusal_and_complete_matching_readback(self):
        with self.raw() as f:
            original = f.server.connection_manager.connection_for(f.board)
            stamp = self.stamp(f)
            image, runs = self.image(f)
            for mode, context in (
                ("mismatch", "does not match"),
                ("short", "returned 30 of 31"),
                ("refusal", "ValueError"),
                ("match", None),
            ):
                with self.subTest(mode=mode):
                    (f.control / "mode").write_text(mode)
                    result = json.loads(
                        f.call("flash_raw", dict(board_id=f.board, artifact=str(image)))
                    )
                    if context:
                        self.assertEqual(result["code"], "flash/verification-failed")
                        self.assertIn(context, result["message"])
                        self.assert_native(f, "flash_verification")
                    else:
                        self.assertEqual(result["status"], "ok")
                        expected = bytearray(pattern(0x20000, 213))
                        for address, data in runs:
                            expected[address : address + len(data)] = data
                        row = next(r for r in reversed(f.rows()) if r.get("opcode") == "flash")
                        self.assertEqual(row["programs"], 1)
                        self.assertEqual(row["reads"], [[0, 4096], [4096, 9], [0x4000, 31]])
                        self.assertEqual(row["memory_sha256"], hashlib.sha256(expected).hexdigest())
                    assert_lite_raw_warning(self, result, "flash_raw")
                    self.assertIs(f.server.connection_manager.connection_for(f.board), original)
                    self.assertEqual(f.server.gate_manager.snapshot(f.board), stamp)
                    self.assertIsNone(original.runtime_session.closed_at)
                    self.assertIsNone(original.handle.worker._process.poll())
                    self.assertTrue(original.handle.worker._marker.exists())
                    self.assertEqual(
                        json.loads(f.call("read_memory_raw", dict(board_id=f.board, address=0)))[
                            "status"
                        ],
                        "ok",
                    )
                    self.assert_idle(f)

    def guarded(self, action, fallback):
        with setup.full_fixture(self, "builtin") as base, process_fixture(self, base) as f:
            self.assertEqual(f.validate()["status"], "validation_passed")
            if action == "flash_bootloader":
                f.factory.request.update(start=0, end=0x8000)
                self.assertEqual(f.admit()["status"], "safety_refresh_completed")
                self.assertEqual(f.validate()["status"], "validation_passed")
            original = f.server.connection_manager.connection_for(f.board)
            image, _ = self.image(f)
            f.flow.call(action + "-plan", setup.NULL_PLAN)
            plan = f.flow.plan(
                action,
                f.board,
                dict(artifact=str(image)),
                permission="one-time" if action == "flash_bootloader" else None,
            )
            self.assertEqual(plan["status"], "plan_accepted")
            self.assertEqual(plan["total_calls"], 1)
            self.assertEqual(plan["timeout_seconds"], 120)
            (f.control / "mode").write_text("oserror")
            call = plan["stable_client_fallback" if fallback else "preferred_call"]
            error = reply = None
            try:
                reply = f.call(call["tool_name"], call["arguments"])
            except Exception as exc:
                error = str(exc)
            if fallback:
                self.assertIn("batch_failed", str(reply))
                self.assertIn("programming is not verified", str(reply))
            else:
                self.assertIn("programming is not verified", str(error))
            self.assert_lost(f, original)
            self.assert_native(f, "flash_readback_connection")
            (f.control / "mode").write_text("match")
            successor = f.connect()
            self.assertIsNot(successor.handle.worker, original.handle.worker)
            self.assertIsNone(f.server.gate_manager.snapshot(f.board))
            self.assertEqual(f.validate()["status"], "validation_passed")
            self.assertIsNotNone(f.server.gate_manager.snapshot(f.board))
            self.assertIn("halted", f.call("get_state", dict(board_id=f.board)).lower())
            self.assert_idle(f)

    def test_application_direct_transport_loss_reconnects_and_revalidates(self):
        self.guarded("flash_application", False)

    def test_permission_approved_bootloader_fallback_transport_loss_revalidates(self):
        self.guarded("flash_bootloader", True)


if __name__ == "__main__":
    unittest.main()
