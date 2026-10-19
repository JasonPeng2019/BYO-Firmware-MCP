"""M08 implementation tests through actual public dispatch and installed pyOCD."""
import contextlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import psutil
from pyocd.core.target import Target
from pyocd.coresight.cortex_m import CortexM

from pyocd_debug_mcp.adapters.swd_process import ProcessIsolatedSWDInterface, _validate_result
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from tests.m07_breakpoint_boundary import BoundaryAdapter
from tests.m08_step_boundary import StepTarget
from tests.tiered_acceptance_support import (
    isolated_project, public_call, public_text, tiered_test_server,
)


def call(server, name, arguments):
    try:
        reply = public_text(server, name, arguments)
    except Exception as error:
        journal(name, arguments, {"error": str(error)})
        raise
    journal(name, arguments, {"text": reply})
    return reply


def journal(name, arguments, result):
    destination = os.environ.get("M08_JOURNAL")
    if destination:
        with Path(destination).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"tool": name, "arguments": arguments, **result}) + "\n")


class BreakpointSteppingTests(unittest.TestCase):
    @contextlib.contextmanager
    def connected(self, target, backend=None):
        with isolated_project() as root, tiered_test_server(root) as server:
            adapter = backend if backend is not None else BoundaryAdapter(target)
            server.configure_tiered_test_seams(backend=adapter)
            inventory = ValidationInventory(probes=(ValidationProbe(
                "m07-test", "M08 AP fixture", "jlink", "m07-test"),))
            with patch.object(server, "_validation_inventory", return_value=inventory):
                self.assertIn("Connected", call(server, "connect", {
                    "board_id": "m08", "probe_uid": "m07-test", "target": "cortex_m"}))
            try:
                yield server
            finally:
                journal("backend-boundary", {}, {
                    "location": "unused parent fixture" if backend is not None else "in-process target",
                    "registers": target.registers, "executed": target.executed,
                    "comparators": target.comparators, "dhcsr": target.dhcsr,
                    "dfsr": target.dfsr, "code_writes": target.code_writes,
                })
                call(server, "disconnect", {"board_id": "m08"})

    def install(self, server, target):
        reply = public_call(server, "set_breakpoint_raw", {
            "board_id": "m08", "address": target.registers["pc"]})
        self.assertEqual(reply["status"], "ok", reply)
        journal("set_breakpoint_raw", {"address": target.registers["pc"]}, reply)
        return target.find_breakpoint(target.registers["pc"])

    def test_public_installed_push_executes_once_and_restores_exact_comparators(self):
        target = StepTarget()
        with self.connected(target) as server:
            bp = self.install(server, target)
            saved = target.comparators.copy()
            reply = call(server, "step", {"board_id": "m08"})
            self.assertIn("Stepped. pc=0x08000002", reply)
            self.assertEqual(target.executed, 1)
            self.assertEqual(target.registers["sp"], 0x200007F8)
            self.assertEqual(target.get_state(), Target.State.HALTED)
            self.assertEqual(target.comparators, saved)
            self.assertIs(target.find_breakpoint(0x08000000), bp)
            self.assertEqual(target.code_writes, [])
            self.assertEqual(target.dhcsr & CortexM.C_MASKINTS, 0)

    def test_public_ordinary_no_breakpoint_step(self):
        target = StepTarget(instruction=0xBF00)
        with self.connected(target) as server:
            reply = call(server, "step", {"board_id": "m08"})
            self.assertIn("Stepped. pc=0x08000002", reply)
            self.assertEqual(target.executed, 1)
            self.assertFalse(any(target.comparators.values()))

    def test_public_step_timeout_reports_observed_running_and_cleans_up(self):
        target = StepTarget(mode="running")
        with self.connected(target) as server:
            self.install(server, target)
            saved = target.comparators.copy()
            with self.assertRaisesRegex(Exception, "Incomplete step: state=RUNNING pc=unavailable"):
                call(server, "step", {"board_id": "m08"})
            self.assertEqual(target.get_state(), Target.State.HALTED)
            self.assertEqual(target.comparators, saved)
            self.assertEqual(target.dhcsr & CortexM.C_MASKINTS, 0)

    def test_public_running_precondition_preserves_target_and_breakpoints(self):
        target = StepTarget()
        with self.connected(target) as server:
            self.install(server, target)
            target.dhcsr &= ~(CortexM.S_HALT | CortexM.C_HALT)
            saved = target.comparators.copy()
            writes = target.writes.copy()
            with self.assertRaisesRegex(Exception, "must be halted"):
                call(server, "step", {"board_id": "m08"})
            self.assertEqual(target.get_state(), Target.State.RUNNING)
            self.assertEqual(target.comparators, saved)
            self.assertEqual(target.writes, writes)

    def test_public_already_masked_interrupt_state_is_preserved(self):
        target = StepTarget()
        target.dhcsr |= CortexM.C_MASKINTS | CortexM.C_PMOV
        with self.connected(target) as server:
            self.install(server, target)
            self.assertIn("stop_reason=single_step", call(server, "step", {"board_id": "m08"}))
            self.assertEqual(target.dhcsr & (CortexM.C_MASKINTS | CortexM.C_PMOV),
                             CortexM.C_MASKINTS | CortexM.C_PMOV)

    def test_public_legitimate_self_branch_completes_at_same_pc_without_retire_status(self):
        target = StepTarget(instruction=0xE7FE)
        with self.connected(target) as server:
            self.install(server, target)
            saved = target.comparators.copy()
            reply = call(server, "step", {"board_id": "m08"})
            self.assertIn("Stepped. pc=0x08000000", reply)
            self.assertEqual(target.executed, 1)
            self.assertEqual(target.comparators, saved)

    def test_public_noop_is_incomplete_even_with_stale_retirement_or_changed_pc(self):
        for mode in ("noop", "pc-only"):
            with self.subTest(mode=mode):
                target = StepTarget(mode=mode)
                target.dhcsr |= CortexM.S_RETIRE_ST
                with self.connected(target) as server:
                    self.install(server, target)
                    saved = target.comparators.copy()
                    with self.assertRaisesRegex(Exception, "[Ii]ncomplete"):
                        call(server, "step", {"board_id": "m08"})
                    self.assertEqual(target.executed, 0)
                    self.assertEqual(target.comparators, saved)
                    self.assertIn("HALTED", call(server, "get_state", {"board_id": "m08"}))

    def test_public_step_error_restores_breakpoint_and_original_interrupt_mask(self):
        target = StepTarget(mode="error")
        with self.connected(target) as server:
            self.install(server, target)
            saved = target.comparators.copy()
            with self.assertRaisesRegex(Exception, "injected step transfer failure"):
                call(server, "step", {"board_id": "m08"})
            self.assertEqual(target.comparators, saved)
            self.assertEqual(target.dhcsr & CortexM.C_MASKINTS, 0)
            self.assertEqual(target.executed, 0)

    def test_public_foreign_breakpoint_is_preserved_and_refused(self):
        target = StepTarget()
        target.set_breakpoint(target.registers["pc"], Target.BreakpointType.HW)
        target.bp_manager.flush()
        saved = target.comparators.copy()
        writes = target.writes.copy()
        with self.connected(target) as server:
            with self.assertRaisesRegex(Exception, "does not own"):
                call(server, "step", {"board_id": "m08"})
            self.assertEqual(target.comparators, saved)
            self.assertEqual(target.writes, writes)
            self.assertEqual(target.executed, 0)

    def test_public_restoration_failure_never_reports_success(self):
        target = StepTarget()
        with self.connected(target) as server:
            self.install(server, target)
            target.drop_bp_restore = True
            with self.assertRaisesRegex(Exception, "restor.*(uncertain|failed|verified)"):
                call(server, "step", {"board_id": "m08"})
            self.assertEqual(target.executed, 1)

    def test_public_owned_software_ram_breakpoint_restores_patch_and_manager(self):
        target = StepTarget(instruction=0xBF00, pc=0x20000000)
        with self.connected(target) as server:
            bp = self.install(server, target)
            self.assertEqual(target.words[0x20000000], 0xBE00)
            reply = call(server, "step", {"board_id": "m08"})
            self.assertIn("Stepped. pc=0x20000002", reply)
            self.assertEqual(target.executed, 1)
            self.assertIs(target.find_breakpoint(0x20000000), bp)
            self.assertEqual(target.words[0x20000000], 0xBE00)

    def test_public_process_worker_preserves_confirmed_pc_and_typed_incomplete(self):
        for mode, instruction in (("execute", "0xb580"), ("execute", "0xe7fe"), ("noop", "0xbf00")):
            with self.subTest(mode=mode, instruction=instruction):
                imports_root = os.environ.get("M08_IMPORTS")
                case_imports = Path(imports_root) / f"case-{mode}-{instruction}" if imports_root else None
                observe_arguments = (str(case_imports),) if case_imports else ()
                backend = ProcessIsolatedSWDInterface(worker_argv=(
                    sys.executable, "-B", "-m", "tests.m08_step_boundary", mode, instruction,
                    *observe_arguments))
                with self.connected(StepTarget(), backend=backend) as server:
                    connection = server.connection_manager.connection_for("m08")
                    handle = connection.handle
                    if case_imports:
                        snapshots = list(case_imports.glob("worker-*.json"))
                        self.assertEqual(len(snapshots), 1)
                        snapshot = snapshots[0]
                        interpreter_pid = int(snapshot.stem.removeprefix("worker-"))
                        owned_pid = handle.worker._process.pid
                        self.assertTrue(interpreter_pid == owned_pid or owned_pid in {
                            parent.pid for parent in psutil.Process(interpreter_pid).parents()})
                        imported = json.loads(snapshot.read_text())
                        names = {row["module"] for row in imported}
                        self.assertTrue({"__main__", "pyocd_debug_mcp.adapters.provider_worker_runtime",
                                         "pyocd_debug_mcp.adapters.swd_pyocd",
                                         "pyocd.coresight.cortex_m"} <= names)
                        journal("worker-imports", {}, {"interpreter_pid": interpreter_pid,
                            "owned_root_pid": owned_pid, "snapshot": str(snapshot),
                            "owned_tree_verified": True})
                    before = handle.metadata
                    self.assertEqual(public_call(server, "set_breakpoint_raw", {
                        "board_id": "m08", "address": 0x08000000})["status"], "ok")
                    if mode == "noop":
                        with self.assertRaisesRegex(Exception, "Incomplete.*step_halt_confirmed=false"):
                            call(server, "step", {"board_id": "m08"})
                    else:
                        expected = "08000002" if instruction == "0xb580" else "08000000"
                        self.assertIn("Stepped. pc=0x" + expected,
                                      call(server, "step", {"board_id": "m08"}))
                        self.assertIn("0x" + expected, call(server, "read_execution_state", {
                            "board_id": "m08", "name": "pc"}))
                        if instruction == "0xb580":
                            self.assertIn("0x200007F8", call(server, "read_execution_state", {
                                "board_id": "m08", "name": "sp"}))
                    self.assertIn("HALTED", call(server, "get_state", {"board_id": "m08"}))
                    self.assertEqual(handle.metadata, before)
                    self.assertIs(server.connection_manager.connection_for("m08"), connection)
                    self.assertIn("Breakpoint removed", call(server, "remove_breakpoint", {
                        "board_id": "m08", "address": 0x08000000}))
                    worker = handle.worker
                # Production close uses confirmed process-tree termination as
                # authority, not a graceful zero child exit (Windows can be 1).
                journal("worker-close", {}, {"raw_child_exit": worker._process.returncode,
                    "cleanup_confirmed": worker._cleanup_confirmed,
                    "marker": str(worker._marker), "closed": worker._closed})
                self.assertIsNotNone(worker._process.returncode)
                self.assertTrue(worker._closed)
                self.assertTrue(worker._cleanup_confirmed)
                self.assertIsNone(worker._marker)

    def test_worker_contract_refuses_void_or_invalid_completion_results(self):
        self.assertEqual(_validate_result("step", 0), 0)
        self.assertEqual(_validate_result("step", 0x08000002), 0x08000002)
        for invalid in (None, True, -1, 1 << 32, "0x08000002"):
            with self.subTest(result=invalid), self.assertRaises(ValueError):
                _validate_result("step", invalid)


if __name__ == "__main__":
    unittest.main()
