"""Fresh B02 M06 public direct/batch oracles; no implementer tests are imported."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import unittest

from tests.b02_public_support import (
    RAM, RAM_BYTES, batch, independently_resolve, symbol_args, symbol_project,
)
from tests.m05_public_support import NULL_PLAN
from tests.test_m05_lite_validation import advertised_validation
from tests.tiered_acceptance_support import isolated_project, public_text, tiered_test_server


def scalar(reply):
    match = re.search(r"value=(0x[0-9a-fA-F]+)", reply)
    if match is None:
        raise AssertionError(f"No scalar in public symbol response: {reply}")
    return int(match[1], 16)


class IndependentSymbols(unittest.TestCase):
    def test_current_elf_association_from_real_verified_public_deployment_and_changed_file(self):
        from tests.test_b02_m13_workflow import IndependentAllocation, workflow
        with workflow(self) as f:
            IndependentAllocation().deploy(f)
            args = {"board_id": "m02_host", "symbol": "word"}
            self.assertEqual(scalar(f.flow.call("read_memory_symbol", args)), int.from_bytes(RAM_BYTES[:4], "little"))
            original = f.elf.read_bytes()
            try:
                f.elf.write_bytes(original + b"changed bound ELF")
                before = list(f.target.calls)
                self.assertIn("memory/symbol-artifact-unavailable", f.flow.call("read_memory_symbol", args))
                self.assertEqual(f.target.calls, before)
            finally:
                f.elf.write_bytes(original)

    def test_registered_direct_batch_and_planned_address_match_independent_elf_bytes(self):
        with symbol_project(self) as f:
            names = {t.name for t in asyncio.run(f.server.mcp.list_tools())}
            self.assertIn("read_memory_symbol", names)
            self.assertNotIn("read_memory_symbol-plan", names)
            f.flow.call("read_memory_address-plan", NULL_PLAN)
            for name, width in (("word", 32), ("half", 16), ("byte", 8)):
                with self.subTest(symbol=name):
                    address, size = independently_resolve(f.elf, name)
                    self.assertEqual(size, width // 8)
                    expected = int.from_bytes(RAM_BYTES[address-RAM:address-RAM+size], "little")
                    args = symbol_args(f, symbol=name, width=width)
                    try:
                        direct = f.flow.call("read_memory_symbol", args)
                    except Exception as exc:
                        self.fail(f"Required public no-plan symbol read failed: {exc}")
                    self.assertEqual(scalar(direct), expected)
                    response = batch(f, "read_memory_symbol", args)
                    self.assertEqual(response["status"], "batch_completed", response)
                    result = response["completed"][0]["result"]
                    if isinstance(result, list):
                        result = result[0]["text"]
                    self.assertEqual(scalar(result), expected)
                    params = {"address": address, "width": width, "length": None}
                    self.assertEqual(f.flow.plan("read_memory_address", f.board, params)["status"], "plan_accepted")
                    reply = f.flow.call("read_memory_address", {"board_id": f.board, **params})
                    self.assertIn(f"0x{expected:0{width//4}X}".lower(), reply.lower())

    def test_missing_invalid_objects_alignment_and_range_refuse_without_target_reads(self):
        with symbol_project(self) as f:
            cases = (({"elf_artifact": None}, "memory/symbol-artifact-unavailable"),
                     ({"elf_artifact": str(f.root / "absent.elf")}, "memory/symbol-artifact-unavailable"),
                     ({"symbol": "absent"}, "memory/symbol-not-found"),
                     ({"width": 24}, "memory/invalid-width"),
                     ({"symbol": "half", "width": 32}, "memory/symbol-width-exceeds-object"),
                     ({"symbol": "unaligned"}, "memory/symbol-address-unaligned"),
                     ({"symbol": "entry"}, "memory/symbol-is-function"))
            for extra, code in cases:
                with self.subTest(extra=extra):
                    before = [r for r in f.backend.calls if r[0] == "read_memory"]
                    reply = f.flow.call("read_memory_symbol", symbol_args(f, **extra))
                    self.assertIn(code, reply)
                    response = batch(f, "read_memory_symbol", symbol_args(f, **extra))
                    self.assertEqual(response["status"], "batch_failed", response)
                    self.assertEqual([r for r in f.backend.calls if r[0] == "read_memory"], before)
            for name in ("unmapped", "edge"):
                with self.subTest(symbol=name):
                    before = [r for r in f.backend.calls if r[0] == "read_memory"]
                    with self.assertRaises(Exception):
                        f.flow.call("read_memory_symbol", symbol_args(f, symbol=name, width=8))
                    self.assertEqual(batch(f, "read_memory_symbol", symbol_args(f, symbol=name, width=8))["status"], "batch_failed")
                    self.assertEqual([r for r in f.backend.calls if r[0] == "read_memory"], before)

    def test_real_disconnect_changed_session_and_revalidation(self):
        with symbol_project(self) as f:
            expected = int.from_bytes(RAM_BYTES[:4], "little")
            self.assertEqual(scalar(f.flow.call("read_memory_symbol", symbol_args(f))), expected)
            old = f.server._active_session_id(f.board)
            f.flow.call("disconnect", {"board_id": f.board})
            for connected in (False, True):
                if connected:
                    f.flow.call("connect", {"board_id": f.board})
                    self.assertNotEqual(old, f.server._active_session_id(f.board))
                before = list(f.backend.calls)
                with self.assertRaises(Exception):
                    f.flow.call("read_memory_symbol", symbol_args(f))
                self.assertEqual(batch(f, "read_memory_symbol", symbol_args(f))["status"], "batch_failed")
                self.assertEqual(f.backend.calls, before)
            f.flow.overview("B02 independent symbols")
            self.assertEqual(advertised_validation(f.flow, f.board)["status"], "validation_passed")
            self.assertEqual(scalar(f.flow.call("read_memory_symbol", symbol_args(f))), expected)

    def test_stale_setup_and_monitor_stop_are_real_refusals(self):
        with symbol_project(self) as f:
            pointer = f.server._capability_policies.pointer_path(f.board)
            original = pointer.read_bytes()
            try:
                pointer.write_bytes(b"incomplete committed policy")
                before = list(f.backend.calls)
                with self.assertRaisesRegex(Exception, "corrupt committed safety policy"):
                    f.flow.call("read_memory_symbol", symbol_args(f))
                self.assertEqual(batch(f, "read_memory_symbol", symbol_args(f))["status"], "batch_failed")
                self.assertEqual(f.backend.calls, before)
            finally:
                pointer.write_bytes(original)
            f.server._monitor._block.refresh("B02", "controlled clock", datetime.now(timezone.utc) - timedelta(days=100))
            try:
                before = list(f.backend.calls)
                with self.assertRaises(Exception):
                    f.flow.call("read_memory_symbol", symbol_args(f))
                self.assertEqual(batch(f, "read_memory_symbol", symbol_args(f))["status"], "batch_failed")
                self.assertEqual(f.backend.calls, before)
            finally:
                f.server._monitor._block.refresh("B02", "controlled clock", datetime.now(timezone.utc))

    def test_failed_public_revalidation_revokes_previously_valid_symbol_reads(self):
        with symbol_project(self) as f:
            self.assertEqual(scalar(f.flow.call("read_memory_symbol", symbol_args(f))), int.from_bytes(RAM_BYTES[:4], "little"))
            # A changed chip reading is a hardware fault boundary. The actual
            # public validator, rather than a manufactured stamp, revokes trust.
            f.backend.identity_value = 0
            validation = advertised_validation(f.flow, f.board)
            self.assertNotEqual(validation["status"], "validation_passed", validation)
            before = list(f.backend.calls)
            with self.assertRaises(Exception):
                f.flow.call("read_memory_symbol", symbol_args(f))
            self.assertEqual(batch(f, "read_memory_symbol", symbol_args(f))["status"], "batch_failed")
            self.assertEqual(f.backend.calls, before)
            f.backend.identity_value = None
            f.flow.call("disconnect", {"board_id": f.board})
            f.flow.call("connect", {"board_id": f.board})
            f.flow.overview("B02 independent symbols")
            self.assertEqual(advertised_validation(f.flow, f.board)["status"], "validation_passed")
            self.assertEqual(scalar(f.flow.call("read_memory_symbol", symbol_args(f))), int.from_bytes(RAM_BYTES[:4], "little"))

    def test_raw_and_lite_never_receive_full_symbol_access(self):
        with isolated_project() as root, tiered_test_server(root) as server:
            with self.assertRaisesRegex(Exception, "no safety map"):
                public_text(server, "read_memory_symbol", {"board_id": "raw", "symbol": "word"})
        with symbol_project(self, tier="setup-lite") as f:
            before = list(f.backend.calls)
            with self.assertRaisesRegex(Exception, "FULL|setup-full"):
                f.flow.call("read_memory_symbol", symbol_args(f))
            self.assertEqual(batch(f, "read_memory_symbol", symbol_args(f))["status"], "batch_failed")
            self.assertEqual(f.backend.calls, before)

    def test_simultaneous_direct_and_batch_obey_board_lock(self):
        with symbol_project(self) as f, ThreadPoolExecutor(max_workers=2) as pool:
            ready = threading.Barrier(3)

            def read(batched):
                ready.wait(timeout=5)
                if batched:
                    return batch(f, "read_memory_symbol", symbol_args(f))
                return f.flow.call("read_memory_symbol", symbol_args(f))

            with f.server.connection_manager.lock_for(f.board):
                before = list(f.backend.calls)
                direct = pool.submit(read, False)
                batched = pool.submit(read, True)
                ready.wait(timeout=5)
                self.assertFalse(threading.Event().wait(0.1))
                self.assertFalse(direct.done())
                self.assertFalse(batched.done())
                self.assertEqual(f.backend.calls, before)
            self.assertEqual(scalar(direct.result(timeout=10)), int.from_bytes(RAM_BYTES[:4], "little"))
            self.assertEqual(batched.result(timeout=10)["status"], "batch_completed")

    def test_fresh_process_explicit_elf_after_restart(self):
        with symbol_project(self) as f:
            f.flow.call("disconnect", {"board_id": f.board})
            command = [sys.executable, "-B", "-m", "tests.b02_restart", str(f.root), f.board, str(f.elf)]
            started = datetime.now(timezone.utc).isoformat()
            result = subprocess.run(command, env={**os.environ, "BYO_MCP_ARTIFACT_ROOT": str(f.root),
                                    "BYO_MCP_MONITOR_ROOT": str(f.root / "restart-monitor")},
                                    capture_output=True, text=True, timeout=45, check=False)
            record = dict(argv=command, started_at=started,
                          finished_at=datetime.now(timezone.utc).isoformat(), exit_code=result.returncode,
                          stdout=result.stdout, stderr=result.stderr)
            (Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / "restart-command.json").write_text(json.dumps(record, indent=2))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
