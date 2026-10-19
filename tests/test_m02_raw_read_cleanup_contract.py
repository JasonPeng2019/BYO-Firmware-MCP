"""Independent real-worker caller control for the shared raw failure handoff.

The programming regression is retained separately. This control loses SWD on a
raw read inside a batch: a healthy refusal and another live worker must survive,
while the failed worker's reservation must be reusable without disconnecting it.
Only native session I/O is substituted; dispatch, RPC and cleanup remain real.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.adapters.swd_process import ProcessIsolatedSWDInterface
from pyocd_debug_mcp.hardware_inventory import snapshot_from_validation_inventory
from pyocd_debug_mcp.kernel.operations import operation_manager
from pyocd_debug_mcp.kernel.processes import _start_token
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from tests.tiered_acceptance_support import isolated_project, public_text, tiered_test_server


BASE = 0x08000000
FAILED = "read-loss"
HEALTHY = "retained-board"
WORKER = r"""
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from tests import m02_independent_support as hardware
from pyocd_debug_mcp.adapters import provider_worker, provider_worker_runtime as runtime, swd_pyocd
target = hardware.MemoryBoundary()
read = target.read_memory
def read_memory(address, *args, **kwargs):
    if sys.argv[2] == "fault" and address == hardware.BASE + 32:
        raise OSError("independent raw-read SWD cable loss")
    return read(address, *args, **kwargs)
target.read_memory = read_memory
dispatch = runtime._dispatch
def observed(adapter, handle, opcode, arguments):
    error = []
    try:
        return dispatch(adapter, handle, opcode, arguments)
    except BaseException as exc:
        current = exc
        while current is not None:
            error.append({"type": type(current).__name__, "message": str(current)})
            current = current.__cause__ or current.__context__
        raise
    finally:
        with Path(sys.argv[3]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"opcode": opcode, "arguments": arguments, "error": error,
                "imports": {m.__name__: {"path": m.__file__,
                    "sha256": hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()}
                    for m in (hardware, runtime, swd_pyocd)}}) + "\n")
runtime._dispatch = observed
runtime.PyOCDSWDInterface = lambda: hardware.BoundaryAdapter(target)
provider_worker.main()
"""


class RawReadCleanupContract(unittest.TestCase):
    def test_batched_read_loss_releases_only_its_worker_after_healthy_input_refusal(self):
        with contextlib.ExitStack() as stack:
            root = stack.enter_context(isolated_project())
            server = stack.enter_context(tiered_test_server(root))
            inventory = ValidationInventory(
                probes=tuple(
                    ValidationProbe(uid, "Controlled", "stlink", uid)
                    for uid in ("lost-probe", "retained-probe")
                )
            )
            stack.enter_context(
                patch.object(
                    server,
                    "_hardware_inventory",
                    SimpleNamespace(
                        snapshot=lambda: snapshot_from_validation_inventory(inventory),
                        validation_inventory=lambda: inventory,
                    ),
                )
            )
            stack.enter_context(
                patch.object(server, "_validation_inventory", return_value=inventory)
            )
            script = root / "read_worker.py"
            script.write_text(WORKER, encoding="utf-8")
            workers, responses, logs = [], [], {}

            def call(tool, board, **arguments):
                response = public_text(server, tool, {"board_id": board, **arguments})
                responses.append({"tool": tool, "board": board, "response": response})
                return response

            def connect(board, uid, mode):
                log = root / f"{board}-{len(workers)}.jsonl"
                logs[str(log)] = log
                server.configure_tiered_test_seams(
                    backend=ProcessIsolatedSWDInterface(
                        worker_argv=(
                            sys.executable,
                            str(script),
                            str(Path(__file__).resolve().parents[1]),
                            mode,
                            str(log),
                        )
                    )
                )
                self.assertIn(
                    "Connected", call("connect", board, probe_uid=uid, target="stm32l476rgtx")
                )
                assigned = server.connection_manager.connection_for(board)
                worker = assigned.handle.worker
                workers.append((worker, worker._process.pid, _start_token(worker._process.pid)))
                return assigned, log

            state = {}
            try:
                failed, failed_log = connect(FAILED, "lost-probe", "fault")
                healthy, healthy_log = connect(HEALTHY, "retained-probe", "healthy")
                original_marker = failed.handle.worker._marker
                self.assertTrue(original_marker.is_file())
                stamps = {}
                for board, connection in ((FAILED, failed), (HEALTHY, healthy)):
                    stamps[board] = server.gate_manager.stamp_validation(
                        board_id=board,
                        connection_id=connection.connection_id,
                        probe_identity=connection.handle.probe_uid,
                        observed_mcu="fixture",
                        validation_run="read-cleanup-control",
                        map_digest="fixture-map",
                    )
                # A user argument error must leave a usable connection and stamp.
                refused = json.loads(call("read_memory_raw", FAILED, address=BASE, width=7))
                self.assertEqual(refused["status"], "error")
                self.assertIs(server.connection_manager.connection_for(FAILED), failed)
                self.assertEqual(server.gate_manager.snapshot(FAILED), stamps[FAILED])
                before = sum(
                    json.loads(s)["opcode"] == "read_memory"
                    for s in healthy_log.read_text().splitlines()
                )
                batch = json.loads(
                    call(
                        "action_batch",
                        FAILED,
                        actions=[
                            {
                                "tool_name": "read_memory_raw",
                                "arguments": {"board_id": FAILED, "address": BASE},
                            },
                            {
                                "tool_name": "read_memory_raw",
                                "arguments": {"board_id": FAILED, "address": BASE + 32},
                            },
                            {
                                "tool_name": "read_memory_raw",
                                "arguments": {"board_id": FAILED, "address": BASE + 64},
                            },
                        ],
                    ).split("\n", 1)[0]
                )
                worker = failed.handle.worker
                marker = worker._marker
                state.update(
                    connected=server.connection_manager.maybe_connection(FAILED) is not None,
                    runtime_closed=failed.runtime_session.closed_at is not None,
                    readiness_cleared=server.gate_manager.snapshot(FAILED) is None,
                    worker_exit=worker._process.poll(),
                    cleanup=worker._cleanup_confirmed,
                    marker=str(marker) if marker else None,
                    operations=len(operation_manager.snapshots()),
                )
                self.assertEqual(batch["status"], "batch_failed", batch)
                self.assertEqual(batch["failure"]["index"], 1)
                self.assertEqual(len(batch["completed"]), 1)
                failure = batch["failure"]["result"]
                failure = json.loads(failure) if isinstance(failure, str) else failure
                self.assertEqual(
                    (failure["status"], failure["code"]), ("error", "backend/read-failed")
                )
                self.assertIn("independent raw-read SWD cable loss", failure["message"])
                rows = [json.loads(s) for s in failed_log.read_text().splitlines()]
                errors = [r for r in rows if r["error"]]
                self.assertEqual(len(errors), 1)
                self.assertEqual(
                    [e["type"] for e in errors[0]["error"]], ["TargetConnectionError", "OSError"]
                )
                self.assertEqual(
                    [r["arguments"]["address"] for r in rows if r["opcode"] == "read_memory"],
                    [BASE, BASE + 32],
                    "the successful prefix runs and the suffix is never sent",
                )
                after = sum(
                    json.loads(s)["opcode"] == "read_memory"
                    for s in healthy_log.read_text().splitlines()
                )
                self.assertEqual(after, before, "the batch must not touch the unrelated worker")
                self.assertIs(server.connection_manager.connection_for(HEALTHY), healthy)
                self.assertEqual(server.gate_manager.snapshot(HEALTHY), stamps[HEALTHY])
                self.assertIsNone(healthy.runtime_session.closed_at)
                self.assertIsNone(healthy.handle.worker._process.poll())
                self.assertEqual(
                    json.loads(call("read_memory_raw", HEALTHY, address=BASE))["status"], "ok"
                )
                self.assertFalse(state["connected"], state)
                self.assertTrue(state["runtime_closed"] and state["readiness_cleared"], state)
                self.assertTrue(state["cleanup"], state)
                self.assertIsNotNone(state["worker_exit"], state)
                self.assertIsNone(state["marker"], state)
                self.assertFalse(original_marker.exists(), "remove only the captured worker marker")
                self.assertEqual(state["operations"], 0)
                successor, _ = connect(FAILED, "lost-probe", "healthy")
                self.assertIsNot(successor.handle.worker, worker)
                self.assertEqual(
                    json.loads(call("read_memory_raw", FAILED, address=BASE + 32))["status"], "ok"
                )
            finally:
                for board in tuple(server.connection_manager.assigned_board_ids()):
                    call("disconnect", board)
                server._monitor.closeout("independent raw read control finished")
                if journal := os.environ.get("RAW_FLASH_BUDGET_JOURNAL"):
                    with Path(journal).open("a", encoding="utf-8") as stream:
                        stream.write(
                            json.dumps(
                                {
                                    "test": self.id(),
                                    "state_at_return": state,
                                    "responses": responses,
                                    "workers": [
                                        {
                                            "pid": pid,
                                            "birth": birth,
                                            "exit": w._process.poll(),
                                            "cleanup": w._cleanup_confirmed,
                                        }
                                        for w, pid, birth in workers
                                    ],
                                    "worker_journals": {
                                        name: p.read_text()
                                        for name, p in logs.items()
                                        if p.exists()
                                    },
                                }
                            )
                            + "\n"
                        )


if __name__ == "__main__":
    unittest.main()
