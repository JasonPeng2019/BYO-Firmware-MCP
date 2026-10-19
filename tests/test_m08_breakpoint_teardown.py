"""Implementation coverage for C01's target uncertainty and owned host release."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile

import psutil

from pyocd_debug_mcp.adapters.swd_process import ProcessIsolatedSWDInterface
from tests import test_m08_independent_stepping as independent
from tests.m08_independent_core import REPAIR_STEP_PROBE, SimulatedCortexM4
from tests.m08_teardown_boundary import RAM_CODE, NativeCloseAdapter, TeardownFaultModel


class BreakpointTeardownTests(independent._Connected):
    def prepare(self, bench, ram):
        address = RAM_CODE if ram else REPAIR_STEP_PROBE
        if ram:
            bench.write(address, 0xB580, 16)
            bench.write(address + 2, 0xE7FE, 16)
            bench.set_pc(address)
        bench.set_bp(address)
        if not ram:
            bench.resume_to(address)
        return address

    def assert_evicted(self, bench):
        ok, reply = bench.attempt("get_state")
        self.assertFalse(ok, reply)
        self.assertIn("is not connected", reply)
        self.assertEqual(bench.call("disconnect").splitlines()[0], "Not connected.")

    def test_installed_session_close_removes_owned_hw_and_exact_ram_with_controls(self):
        for ram in (False, True):
            for explicit_remove in (False, True):
                with self.subTest(ram=ram, explicit_remove=explicit_remove):
                    model = SimulatedCortexM4()
                    adapter = NativeCloseAdapter(model)
                    with self.bench(model, backend=adapter) as bench:
                        address = self.prepare(bench, ram)
                        if explicit_remove:
                            bench.remove_bp(address)
                        self.assertIn("Disconnected", bench.call("disconnect"))
                        self.assertEqual(model.comparators, [0] * len(model.comparators))
                        if ram:
                            self.assertEqual(model.halfword(address), 0xB580)
                        self.assertFalse(model.halted)
                        self.assertEqual(model.control, 0)
                        self.assertFalse(adapter.sessions[-1].probe.is_open)
                        self.assertEqual(adapter._owned_breakpoints, {})
                        self.assertEqual(model.flash_writes(), [])

    def test_disconnect_reports_target_uncertainty_even_when_native_session_suppresses_io_error(self):
        for ram in (False, True):
            with self.subTest(ram=ram):
                model = TeardownFaultModel(ram=ram)
                adapter = NativeCloseAdapter(model)
                with self.bench(model, backend=adapter) as bench:
                    self.prepare(bench, ram)
                    ok, reply = bench.attempt("disconnect")
                    self.assertFalse(ok, reply)
                    self.assertIn("Session-owned breakpoint cleanup is uncertain", reply)
                    self.assertIn("target restoration was not proven", reply)
                    self.assertTrue(model.transport_lost)
                    self.assertTrue(adapter.sessions[-1].closed)
                    self.assertFalse(adapter.sessions[-1].probe.is_open)
                    self.assertEqual(adapter._owned_breakpoints, {})
                    self.assert_evicted(bench)
                    self.assertEqual(model.flash_writes(), [])

    def test_step_fault_retains_original_error_and_cleanup_uncertainty_after_eviction(self):
        for ram in (False, True):
            with self.subTest(ram=ram):
                model = TeardownFaultModel(ram=ram, after_step=True)
                adapter = NativeCloseAdapter(model)
                with self.bench(model, backend=adapter) as bench:
                    self.prepare(bench, ram)
                    ok, reply = bench.step()
                    self.assertFalse(ok, reply)
                    self.assertNotIn("Stepped", reply)
                    self.assertIn("TransferError: simulated SWD transfer fault", reply)
                    self.assertIn("Session-owned breakpoint cleanup is uncertain", reply)
                    self.assertTrue(model.transport_lost)
                    self.assertFalse(adapter.sessions[-1].probe.is_open)
                    self.assertEqual(adapter._owned_breakpoints, {})
                    self.assert_evicted(bench)
                    self.assertEqual(model.flash_writes(), [])

    @contextlib.contextmanager
    def worker(self, mode):
        parent = os.environ.get("M08T_WORKER_OBSERVATIONS")
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="m08-r01-", dir=parent) as observation:
            backend = ProcessIsolatedSWDInterface(worker_argv=(
                sys.executable, "-B", "-m", "tests.m08_teardown_boundary", mode, observation))
            with self.bench(backend=backend) as bench:
                worker = bench.server.connection_manager.connection_for(independent.BOARD).handle.worker
                owned = psutil.Process(worker._process.pid)
                imports = list(Path(observation).glob("worker-*-imports.json"))
                self.assertEqual(len(imports), 1)
                interpreter = psutil.Process(int(imports[0].name.split("-")[1]))
                identities = [(p.pid, p.create_time()) for p in (owned, interpreter)]
                state_file = Path(observation) / f"worker-{interpreter.pid}-state.json"
                yield bench, state_file
                state = json.loads(state_file.read_text())
            absent = [independent._pid_absent(pid, created) for pid, created in identities]
            independent.journal({"test": self.id(), "mode": mode, "identities": identities,
                                 "absent": absent, "raw_worker_close": worker._process.returncode,
                                 "cleanup_confirmed": worker._cleanup_confirmed, "state": state,
                                 "imports": json.loads(imports[0].read_text())})
            self.assertTrue(all(absent))
            self.assertTrue(worker._cleanup_confirmed)
            self.assertIsNotNone(worker._process.returncode)

    def test_real_worker_disconnect_restores_hw_and_ram_before_process_termination(self):
        for ram in (False, True):
            with self.subTest(ram=ram), self.worker("ordinary-ram" if ram else "ordinary-hw") as (bench, state_file):
                self.prepare(bench, ram)
                self.assertIn("Disconnected", bench.call("disconnect"))
                state = json.loads(state_file.read_text())
                self.assertEqual(state["comparators"], [0] * len(state["comparators"]))
                self.assertFalse(state["transport_lost"])
                if ram:
                    self.assertEqual(state["ram_instruction"], 0xB580)

    def test_real_worker_preserves_teardown_uncertainty_and_original_step_fault(self):
        for ram in (False, True):
            for step in (False, True):
                mode = ("step-" if step else "fault-") + ("ram" if ram else "hw")
                with self.subTest(mode=mode), self.worker(mode) as (bench, state_file):
                    self.prepare(bench, ram)
                    ok, reply = bench.step() if step else bench.attempt("disconnect")
                    self.assertFalse(ok, reply)
                    self.assertIn("Session-owned breakpoint cleanup is uncertain", reply)
                    self.assertIn("target restoration was not proven", reply)
                    if step:
                        self.assertIn("TransferError: simulated SWD transfer fault", reply)
                    self.assertTrue(json.loads(state_file.read_text())["transport_lost"])
                    self.assert_evicted(bench)
