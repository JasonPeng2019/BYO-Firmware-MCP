"""M08-T independent public regressions for breakpoint-aware single stepping.

Every operation goes through the registered public MCP tools, the production
service/adapter (and, for the worker cases, the production process worker), and
installed pyOCD SoCTarget/CortexM/FPB/breakpoint manager. Only the MEM-AP/probe
transport is simulated (tests.m08_independent_core). It decodes and executes
the measured Thumb encoding at PC from the current firmware-v2 STM32 image, so a
reported step is checked against actual instruction effects, not PC movement.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import psutil
from pyocd.core.target import Target
from pyocd.coresight.cortex_m import CortexM

from pyocd_debug_mcp.adapters.swd_process import ProcessIsolatedSWDInterface
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from tests.m08_independent_core import (
    COUNTER, FP_COMP0, FPB_BASE, LOOP_CALL, PROBE_UID, REPAIR_STEP_PROBE, SimulatedCortexM4,
    SimulatedProbeAdapter,
)
from tests.tiered_acceptance_support import (
    isolated_project, public_call, public_text, tiered_test_server,
)

BOARD = "m08t"
PROBE = REPAIR_STEP_PROBE  # 0x08000058 B580 push {r7, lr}
CONFIRMED = " state=HALTED stop_reason=single_step"
CONTROL_MASK = CortexM.C_MASKINTS | CortexM.C_PMOV
FOREIGN = 0x080000F4  # 6808 ldr r0, [r1]
OTHER_OWNED = 0x080000F8  # 6008 str r0, [r1]
SELF_BRANCH = 0x08002000  # staged E7FE b .
RAM_CODE = 0x20004000  # staged B580 push {r7, lr}; E7FE b .


def stepped(pc: int) -> str:
    return f"Stepped. pc=0x{pc:08X}"


def rev1_comparator(address: int) -> int:
    """FPBv1 FP_COMPn value for an enabled code comparator (DDI0403E C1.11.5)."""
    return (2 if address & 2 else 1) << 30 | (address & 0x1FFFFFFC) | 1


def journal(entry: dict[str, Any]) -> None:
    destination = os.environ.get("M08T_JOURNAL")
    if destination:
        with Path(destination).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, default=str) + "\n")


class Bench:
    """Public tool calls for one connected board plus the simulated silicon, if local."""

    def __init__(self, test: unittest.TestCase, server: Any, model: SimulatedCortexM4 | None):
        self.test = test
        self.server = server
        self.model = model

    def attempt(self, tool: str, **arguments: Any) -> tuple[bool, str]:
        arguments = {"board_id": BOARD, **arguments}
        try:
            reply = public_text(self.server, tool, arguments)
            ok = True
        except Exception as error:  # FastMCP raises the tool error text
            reply, ok = str(error), False
        journal({"test": self.test.id(), "tool": tool, "arguments": arguments,
                 "ok": ok, "reply": reply})
        return ok, reply

    def call(self, tool: str, **arguments: Any) -> str:
        ok, reply = self.attempt(tool, **arguments)
        self.test.assertTrue(ok and not reply.startswith("Refused"), f"{tool}: {reply}")
        return reply

    def raw(self, tool: str, **arguments: Any) -> dict[str, Any]:
        arguments = {"board_id": BOARD, **arguments}
        reply = public_call(self.server, tool, arguments)
        journal({"test": self.test.id(), "tool": tool, "arguments": arguments, "reply": reply})
        return reply

    def step(self) -> tuple[bool, str]:
        return self.attempt("step")

    def register(self, name: str) -> int:
        tool = "read_cpu_register" if re.fullmatch(r"r\d+", name) else "read_execution_state"
        reply = self.call(tool, name=name)
        return int(re.search(r"0x[0-9A-Fa-f]+", reply)[0], 16)

    def read(self, address: int, width: int = 32) -> int:
        reply = self.raw("read_memory_raw", address=address, width=width)
        self.test.assertEqual(reply.get("status"), "ok", reply)
        return int(reply["result"], 16)

    def write(self, address: int, value: int, width: int = 32) -> None:
        reply = self.raw("write_memory_raw", address=address, value=value, width=width)
        self.test.assertEqual(reply.get("status"), "ok", reply)

    def set_pc(self, value: int) -> None:
        reply = self.raw("write_cpu_register_raw", name="pc", value=value)
        self.test.assertEqual(reply.get("status"), "ok", reply)

    def set_bp(self, address: int) -> None:
        reply = self.raw("set_breakpoint_raw", address=address)
        self.test.assertEqual(reply.get("status"), "ok", reply)

    def remove_bp(self, address: int) -> None:
        self.test.assertIn("Breakpoint removed", self.call(
            "remove_breakpoint", address=f"0x{address:08X}"))

    def state(self) -> str:
        return self.call("get_state").strip().splitlines()[0]

    def comparator_address(self, address: int) -> int:
        """FP_COMPn register currently matching ``address``, found by public reads."""
        for index in range(6):
            register = FP_COMP0 + 4 * index
            if self.read(register) == rev1_comparator(address):
                return register
        self.test.fail(f"no enabled comparator matches 0x{address:08X}")

    @property
    def pyocd_target(self) -> Any:
        return self.server.connection_manager.connection_for(BOARD).handle.session.target

    def resume_to(self, address: int) -> None:
        """Resume the firmware loop until a breakpoint halt at ``address``."""
        self.test.assertEqual(self.call("resume").strip().splitlines()[0], "Resumed.")
        self.test.assertEqual(self.state(), "HALTED")
        self.test.assertEqual(self.register("pc"), address)
        if self.model is not None:
            self.test.assertTrue(self.model.dfsr & CortexM.DFSR_BKPT, "halt was not a breakpoint")


class _Connected(unittest.TestCase):
    @contextlib.contextmanager
    def bench(self, model: SimulatedCortexM4 | None = None, *, writable_flash: bool = False,
              backend: Any = None) -> Iterator[Bench]:
        if backend is None:
            model = model if model is not None else SimulatedCortexM4()
            backend = SimulatedProbeAdapter(model, writable_flash=writable_flash)
        with isolated_project() as root, tiered_test_server(root) as server:
            server.configure_tiered_test_seams(backend=backend)
            inventory = ValidationInventory(probes=(ValidationProbe(
                PROBE_UID, "M08-T simulated MEM-AP", "jlink", PROBE_UID),))
            with patch.object(server, "_validation_inventory", return_value=inventory):
                reply = public_text(server, "connect", {
                    "board_id": BOARD, "probe_uid": PROBE_UID, "target": "cortex_m"})
            self.assertIn("Connected", reply)
            bench = Bench(self, server, model)
            bench.backend = backend
            try:
                yield bench
            finally:
                if model is not None:
                    model.disarm()
                bench.attempt("disconnect")
                if model is not None:
                    journal({"test": self.id(), "model": model.state()})

    def comparator_writes(self, model: SimulatedCortexM4, since: int) -> list[tuple[int, int]]:
        return [(row[1], row[2]) for row in model.journal[since:]
                if row[0] in {"w", "w-dropped"} and FP_COMP0 <= row[1] < FP_COMP0 + 32]

    def executed(self, model: SimulatedCortexM4, since: int) -> list[tuple[int, str, str, int]]:
        return [(row["pc"], row["encoding"], row["mnemonic"], row["next_pc"])
                for row in model.executed[since:]]


class OwnedBreakpointStepTests(_Connected):
    def test_owned_flash_breakpoint_executes_decoded_push_and_restores_exact_comparator(self):
        with self.bench() as bench:
            model = bench.model
            bench.set_bp(PROBE)
            comparator = bench.comparator_address(PROBE)
            installed = list(model.comparators)
            bench.resume_to(PROBE)
            sp, r7, lr = (bench.register(name) for name in ("sp", "r7", "lr"))
            self.assertEqual(lr, 0x080000ED)  # bl at 0x080000E8 returns to 0x080000EC
            mark, journal_mark = len(model.executed), len(model.journal)
            requests = model.step_requests

            ok, reply = bench.step()

            self.assertTrue(ok, reply)
            self.assertEqual(reply.splitlines()[0], stepped(PROBE + 2) + CONFIRMED)
            self.assertEqual(self.executed(model, mark), [(PROBE, "B580", "push", PROBE + 2)])
            self.assertEqual(model.step_requests - requests, 1)
            # Instruction effects, read back through public tools.
            self.assertEqual(bench.register("pc"), PROBE + 2)
            self.assertEqual(bench.register("sp"), sp - 8)
            self.assertEqual(bench.read(sp - 8), r7)
            self.assertEqual(bench.read(sp - 4), lr)
            self.assertEqual(bench.register("r7"), r7)
            # Exact restoration: the owned comparator was suspended only for the step.
            self.assertEqual(self.comparator_writes(model, journal_mark), [(comparator, 0),
                             (comparator, rev1_comparator(PROBE))])
            self.assertEqual(bench.read(comparator), rev1_comparator(PROBE))
            self.assertEqual(model.comparators, installed)
            self.assertEqual(bench.read(FPB_BASE) & 1, 1)
            self.assertEqual(model.control & CONTROL_MASK, 0)
            self.assertEqual(model.dfsr, CortexM.DFSR_HALTED)
            self.assertEqual(bench.state(), "HALTED")
            self.assertEqual(model.code_writes(), [])
            # Ownership and the physical breakpoint survive: the next loop pass
            # increments the counter and halts at, then steps over, the same BP.
            counter = bench.read(COUNTER)
            bench.resume_to(PROBE)
            self.assertEqual(bench.read(COUNTER), counter + 1)
            ok, reply = bench.step()
            self.assertTrue(ok, reply)
            self.assertEqual(reply.splitlines()[0], stepped(PROBE + 2) + CONFIRMED)
            bench.remove_bp(PROBE)
            self.assertEqual(model.comparators, [0] * 8)

    def test_ordinary_steps_without_breakpoint_execute_decoded_instructions(self):
        # Unaffected control: also expected to pass before M08 (prefix match only).
        with self.bench() as bench:
            model = bench.model
            self.assertEqual(bench.register("pc"), LOOP_CALL)
            sp = bench.register("sp")
            expected = [
                ((LOOP_CALL, "F7FF FFB6", "bl", PROBE), PROBE),
                ((PROBE, "B580", "push", PROBE + 2), PROBE + 2),
                ((PROBE + 2, "466F", "mov", PROBE + 4), PROBE + 4),
            ]
            for instruction, pc_after in expected:
                with self.subTest(instruction=instruction[2]):
                    mark, journal_mark = len(model.executed), len(model.journal)
                    ok, reply = bench.step()
                    self.assertTrue(ok, reply)
                    self.assertTrue(reply.startswith(stepped(pc_after)), reply)
                    self.assertEqual(self.executed(model, mark), [instruction])
                    self.assertEqual(bench.register("pc"), pc_after)
                    self.assertEqual(self.comparator_writes(model, journal_mark), [])
            self.assertEqual(bench.register("lr"), 0x080000ED)
            self.assertEqual(bench.register("sp"), sp - 8)
            self.assertEqual(bench.register("r7"), sp - 8)
            self.assertEqual(bench.read(sp - 4), 0x080000ED)
            self.assertEqual(model.code_writes(), [])

    def test_legitimate_same_pc_self_branch_completes_with_and_without_owned_breakpoint(self):
        for owned in (False, True):
            with self.subTest(owned_breakpoint=owned), self.bench(
                SimulatedCortexM4(staged={SELF_BRANCH: 0xE7FE})
            ) as bench:
                model = bench.model
                bench.set_pc(SELF_BRANCH)  # explicit pre-measurement starting PC
                if owned:
                    bench.set_bp(SELF_BRANCH)
                installed = list(model.comparators)
                mark = len(model.executed)
                ok, reply = bench.step()
                self.assertTrue(ok, reply)
                self.assertTrue(reply.startswith(stepped(SELF_BRANCH)), reply)
                self.assertEqual(self.executed(model, mark),
                                 [(SELF_BRANCH, "E7FE", "b", SELF_BRANCH)])
                self.assertEqual(bench.register("pc"), SELF_BRANCH)
                self.assertEqual(model.comparators, installed)
                self.assertEqual(model.dfsr, CortexM.DFSR_HALTED)
                if owned:
                    self.assertEqual(reply.splitlines()[0], stepped(SELF_BRANCH) + CONFIRMED)
                    bench.remove_bp(SELF_BRANCH)
                self.assertEqual(model.code_writes(), [])

    def test_owned_ram_software_breakpoints_restore_patch_for_push_and_self_branch(self):
        with self.bench() as bench:
            model = bench.model
            bench.write(RAM_CODE, 0xB580, 16)
            bench.write(RAM_CODE + 2, 0xE7FE, 16)
            bench.set_pc(RAM_CODE)
            sp, r7, lr = (bench.register(name) for name in ("sp", "r7", "lr"))
            for address, instruction, pc_after in (
                (RAM_CODE, (RAM_CODE, "B580", "push", RAM_CODE + 2), RAM_CODE + 2),
                (RAM_CODE + 2, (RAM_CODE + 2, "E7FE", "b", RAM_CODE + 2), RAM_CODE + 2),
            ):
                with self.subTest(instruction=instruction[2]):
                    bench.set_bp(address)
                    self.assertEqual(model.halfword(address), 0xBE00)  # FPBv1 cannot match SRAM
                    self.assertEqual(model.comparators, [0] * 8)
                    mark = len(model.executed)
                    ok, reply = bench.step()
                    self.assertTrue(ok, reply)
                    self.assertEqual(reply.splitlines()[0], stepped(pc_after) + CONFIRMED)
                    self.assertEqual(self.executed(model, mark), [instruction])
                    self.assertEqual(model.halfword(address), 0xBE00)  # physical patch restored
                    # The public read hides the patch and returns the original encoding.
                    self.assertEqual(bench.read(address, 16), int(instruction[1], 16))
            self.assertEqual(bench.register("sp"), sp - 8)
            self.assertEqual(bench.read(sp - 8), r7)
            self.assertEqual(bench.read(sp - 4), lr)
            bench.remove_bp(RAM_CODE)
            bench.remove_bp(RAM_CODE + 2)
            self.assertEqual((model.halfword(RAM_CODE), model.halfword(RAM_CODE + 2)),
                             (0xB580, 0xE7FE))
            self.assertEqual(model.flash_writes(), [])

    def test_foreign_breakpoint_at_pc_is_refused_without_step_or_patch(self):
        with self.bench() as bench:
            model = bench.model
            target = bench.pyocd_target  # another owner installs through pyOCD directly
            self.assertTrue(target.set_breakpoint(PROBE, Target.BreakpointType.HW))
            target.selected_core_or_raise.bp_manager.flush()
            foreign = target.find_breakpoint(PROBE)
            bench.resume_to(PROBE)
            installed = list(model.comparators)
            mark, journal_mark = len(model.executed), len(model.journal)
            requests = model.step_requests
            ok, reply = bench.step()
            self.assertFalse(ok, reply)
            self.assertNotIn("Stepped", reply)
            self.assertIn("does not own the installed breakpoint", reply)
            self.assertEqual(model.step_requests, requests)
            self.assertEqual(self.executed(model, mark), [])
            self.assertEqual(self.comparator_writes(model, journal_mark), [])
            self.assertEqual(model.comparators, installed)
            self.assertIs(target.find_breakpoint(PROBE), foreign)
            self.assertEqual(bench.register("pc"), PROBE)
            self.assertEqual(model.code_writes(), [])


class IncompleteStepTests(_Connected):
    CASES = (
        # name, step fault knobs, stop at owned BP, stale S_RETIRE_ST, expected reply, PC moves
        ("no-op at owned breakpoint", {"step_mode": "noop"}, True, False,
         "Incomplete step: state=HALTED pc=0x08000058 step_halt_confirmed=false dfsr=0x0", 0),
        ("no-op with stale retirement status", {"step_mode": "noop"}, False, True,
         "Incomplete step: state=HALTED pc=0x080000E8 step_halt_confirmed=false dfsr=0x0", 0),
        ("changed PC without a step", {"step_mode": "pc_only"}, True, False,
         "Incomplete step: state=HALTED pc=0x0800005A step_halt_confirmed=false dfsr=0x0", 2),
        ("stale halt cause that cannot be cleared",
         {"step_mode": "noop", "sticky_dfsr": CortexM.DFSR_HALTED}, True, False,
         "Incomplete step: stale debug causes could not be cleared.", 0),
    )

    def test_no_op_stale_cause_or_changed_pc_never_fabricates_success(self):
        for name, knobs, at_breakpoint, stale_retire, expected, moved in self.CASES:
            with self.subTest(case=name), self.bench() as bench:
                model = bench.model
                if at_breakpoint:
                    bench.set_bp(PROBE)
                    bench.resume_to(PROBE)
                start, sp = bench.register("pc"), bench.register("sp")
                installed = list(model.comparators)
                mark, journal_mark = len(model.executed), len(model.journal)
                model.arm(**knobs)
                model.retired = stale_retire
                ok, reply = bench.step()
                model.disarm()
                self.assertFalse(ok, reply)
                self.assertNotIn("Stepped", reply)
                self.assertIn(expected, reply)
                self.assertEqual(self.executed(model, mark), [])
                self.assertEqual(bench.state(), "HALTED")
                self.assertEqual(bench.register("pc"), start + moved)
                self.assertEqual(bench.register("sp"), sp)
                self.assertEqual(model.comparators, installed)
                self.assertEqual(model.control & CONTROL_MASK, 0)
                if at_breakpoint:
                    comparator = bench.comparator_address(PROBE)
                    self.assertEqual(self.comparator_writes(model, journal_mark)[-1],
                                     (comparator, rev1_comparator(PROBE)))
                # The same session then completes a real step from the observed PC.
                pc = bench.register("pc")
                mark = len(model.executed)
                ok, reply = bench.step()
                self.assertTrue(ok, reply)
                self.assertEqual(len(self.executed(model, mark)), 1)
                self.assertEqual(self.executed(model, mark)[0][0], pc)
                if at_breakpoint:
                    bench.remove_bp(PROBE)
                self.assertEqual(model.code_writes(), [])


class FaultedStepCleanupTests(_Connected):
    def install_owners(self, bench: Bench) -> tuple[Any, Any, dict[int, int]]:
        """A foreign BP set through pyOCD directly, another owned BP, and the owned BP at PC."""
        target = bench.pyocd_target
        self.assertTrue(target.set_breakpoint(FOREIGN, Target.BreakpointType.HW))
        target.selected_core_or_raise.bp_manager.flush()
        foreign = target.find_breakpoint(FOREIGN)
        bench.set_bp(OTHER_OWNED)
        bench.set_bp(PROBE)
        comparators = {address: bench.comparator_address(address)
                       for address in (FOREIGN, OTHER_OWNED, PROBE)}
        bench.resume_to(PROBE)
        return target, foreign, comparators

    def faulted_step(self, bench: Bench, case: str, **knobs: Any) -> tuple[str, int, int]:
        model = bench.model
        mark, journal_mark = len(model.executed), len(model.journal)
        model.arm(**knobs)
        started = time.monotonic()
        ok, reply = bench.step()
        journal({"test": self.id(), "case": case, "step_seconds": time.monotonic() - started})
        model.disarm()
        self.assertFalse(ok, reply)
        self.assertNotIn("Stepped", reply)
        return reply, mark, journal_mark

    def test_timeout_and_lost_restoration_keep_other_owners_and_halt(self):
        cases = (
            ("core never re-halts within the step timeout", {"step_mode": "stall"},
             "Incomplete step: state=RUNNING pc=unavailable step_halt_confirmed=false dfsr=0x0",
             [], PROBE),
            ("owned comparator restoration lost", None, "Step breakpoint restoration is uncertain",
             [(PROBE, "B580", "push", PROBE + 2)], PROBE + 2),
        )
        for case, knobs, expected, ran, pc_after in cases:
            with self.subTest(case=case), self.bench() as bench:
                model = bench.model
                target, foreign, comparators = self.install_owners(bench)
                installed = list(model.comparators)
                restored = knobs is not None
                reply, mark, journal_mark = self.faulted_step(
                    bench, case, **(knobs or {"drop_after_execute": {comparators[PROBE]}}))
                self.assertIn(expected, reply)
                self.assertEqual(self.executed(model, mark), ran)
                self.assertEqual(bench.state(), "HALTED")  # the stall is halted by cleanup
                self.assertEqual(bench.register("pc"), pc_after)
                self.assertEqual(model.control & CONTROL_MASK, 0)
                # Only the owned slot at PC was touched; other owners stay installed.
                touched = {address for address, _ in self.comparator_writes(model, journal_mark)}
                self.assertEqual(touched, {comparators[PROBE]})
                self.assertIs(target.find_breakpoint(FOREIGN), foreign)
                self.assertEqual(bench.read(comparators[FOREIGN]), rev1_comparator(FOREIGN))
                self.assertEqual(bench.read(comparators[OTHER_OWNED]), rev1_comparator(OTHER_OWNED))
                self.assertEqual(model.comparators == installed, restored)
                if not restored:
                    self.assertEqual(bench.read(comparators[PROBE]), 0)
                # Owned cleanup succeeds through the public route.
                bench.remove_bp(PROBE)
                bench.remove_bp(OTHER_OWNED)
                self.assertEqual(bench.read(comparators[PROBE]), 0)
                self.assertEqual(bench.read(comparators[OTHER_OWNED]), 0)
                self.assertEqual(bench.read(comparators[FOREIGN]), rev1_comparator(FOREIGN))
                self.assertEqual(model.code_writes(), [])

    def test_transport_faults_restore_owned_slot_before_connection_eviction(self):
        # A transport failure evicts and closes the connection (accepted lifecycle
        # behavior). The step must still report the original fault and finish its
        # own restoration before that teardown begins.
        cases = (
            ("step request transfer fault", {"step_mode": "transfer_error"},
             "TransferError: simulated SWD transfer fault on the DHCSR step request", []),
            ("transfer fault reported after execution", {"fail_flush_after_execute": True},
             "TransferError: simulated deferred SWD transfer fault",
             [(PROBE, "B580", "push", PROBE + 2)]),
        )
        for case, knobs, expected, ran in cases:
            with self.subTest(case=case), self.bench() as bench:
                model = bench.model
                _, _, comparators = self.install_owners(bench)
                reply, mark, journal_mark = self.faulted_step(bench, case, **knobs)
                self.assertIn(expected, reply)
                rows = model.executed[mark:]
                self.assertEqual([(row["pc"], row["encoding"], row["mnemonic"], row["next_pc"])
                                  for row in rows if row["via"] == "step"], ran)
                writes = [(index, row[1], row[2]) for index, row in
                          enumerate(model.journal[journal_mark:], journal_mark) if row[0] == "w"]
                request = next(index for index, address, value in writes
                               if address == CortexM.DHCSR and value & CortexM.C_STEP)
                slot = [(index, address, value) for index, address, value in writes
                        if FP_COMP0 <= address < FP_COMP0 + 32]
                self.assertEqual([(address, value) for _, address, value in slot[:2]],
                                 [(comparators[PROBE], 0),
                                  (comparators[PROBE], rev1_comparator(PROBE))])
                restored_at = slot[1][0]
                controls = [value & 0xFFFF for index, address, value in writes
                            if address == CortexM.DHCSR and request < index < restored_at]
                self.assertEqual(controls, [CortexM.C_DEBUGEN | CortexM.C_HALT])
                self.assertTrue(all(row["journal_index"] > restored_at
                                    for row in rows if row["via"] == "run"))
                ok, after = bench.attempt("get_state")
                self.assertFalse(ok, after)
                self.assertIn("is not connected", after)
                self.assertTrue(bench.backend.sessions[-1].closed)
                self.assertEqual(model.code_writes(), [])

    def test_lost_ram_patch_restoration_is_reported_and_owner_can_clean_up(self):
        with self.bench() as bench:
            model = bench.model
            bench.write(RAM_CODE, 0xB580, 16)
            bench.set_pc(RAM_CODE)
            bench.set_bp(RAM_CODE)
            mark = len(model.executed)
            model.arm(drop_after_execute={RAM_CODE})
            ok, reply = bench.step()
            model.disarm()
            self.assertFalse(ok, reply)
            self.assertNotIn("Stepped", reply)
            self.assertIn("Step breakpoint restoration is uncertain", reply)
            self.assertEqual(self.executed(model, mark), [(RAM_CODE, "B580", "push", RAM_CODE + 2)])
            self.assertEqual(model.halfword(RAM_CODE), 0xB580)  # the 0xBE00 patch was not restored
            self.assertEqual(bench.state(), "HALTED")
            self.assertEqual(model.control & CONTROL_MASK, 0)
            bench.remove_bp(RAM_CODE)
            self.assertEqual(model.halfword(RAM_CODE), 0xB580)
            self.assertEqual(model.flash_writes(), [])


class OwnedBreakpointTeardownTests(_Connected):
    """C01: inspect silicon after the public connection has actually closed.

    Step restoration alone is insufficient: installed pyOCD may re-install a
    pending breakpoint during disconnect's resume/PRE_RUN. These assertions
    deliberately remain ordinary failures until that product defect is fixed.
    """

    def prepare(self, bench: Bench, ram: bool) -> tuple[int, list[int]]:
        address = RAM_CODE if ram else PROBE
        before = list(bench.model.comparators)
        if ram:
            bench.write(address, 0xB580, 16)
            bench.write(address + 2, 0xE7FE, 16)
            bench.set_pc(address)
        bench.set_bp(address)
        if ram:
            self.assertEqual(bench.model.halfword(address), 0xBE00)
        else:
            bench.resume_to(address)
            self.assertEqual(bench.read(bench.comparator_address(address)), rev1_comparator(address))
        return address, before

    def assert_closed_and_uninstrumented(self, bench: Bench, address: int,
                                        before: list[int], ram: bool) -> None:
        model = bench.model
        observation = {"test": self.id(), "ram": ram, "address": address,
                       "closed": bench.backend.sessions[-1].closed, "model": model.state(),
                       "ram_halfword": model.halfword(RAM_CODE)}
        journal({"teardown": observation})
        self.assertTrue(observation["closed"])
        self.assertEqual(model.control & CortexM.C_DEBUGEN, 0)
        self.assertFalse(model.halted)
        if ram:
            self.assertEqual(model.halfword(address), 0xB580,
                             "C01: owned RAM BKPT remains after the public session closed")
        else:
            self.assertEqual(model.comparators, before,
                             "C01: owned comparator remains after the public session closed")
        self.assertEqual(model.flash_writes(), [])

    def disconnect_case(self, ram: bool, *, remove_first: bool = False) -> None:
        with self.bench() as bench:
            address, before = self.prepare(bench, ram)
            if remove_first:
                bench.remove_bp(address)
            self.assertIn("Disconnected", bench.call("disconnect"))
            self.assert_closed_and_uninstrumented(bench, address, before, ram)

    def eviction_case(self, ram: bool) -> None:
        with self.bench() as bench:
            address, before = self.prepare(bench, ram)
            mark = len(bench.model.executed)
            journal_mark = len(bench.model.journal)
            bench.model.arm(step_mode="transfer_error")
            ok, reply = bench.step()
            bench.model.disarm()
            self.assertFalse(ok, reply)
            self.assertNotIn("Stepped", reply)
            self.assertIn("TransferError: simulated SWD transfer fault", reply)
            # The failed step executes nothing. Native disconnect subsequently
            # resumes normally after removing instrumentation (via="run").
            rows = bench.model.executed[mark:]
            self.assertEqual([row for row in rows if row["via"] == "step"], [])
            journal({"test": self.id(), "eviction_execution_phases": {
                "step_count": sum(row["via"] == "step" for row in rows),
                "run_count": sum(row["via"] == "run" for row in rows),
                "first_run": next((row for row in rows if row["via"] == "run"), None),
                "journal_start": journal_mark,
                "transfers": bench.model.journal[journal_mark:],
            }})
            ok, after = bench.attempt("get_state")
            self.assertFalse(ok, after)
            self.assertIn("is not connected", after)
            self.assert_closed_and_uninstrumented(bench, address, before, ram)
            self.assertTrue(rows, "ordinary native disconnect must resume after cleanup")
            self.assertTrue(all(row["via"] == "run" for row in rows))

    def test_disconnect_removes_owned_hardware_comparator(self):
        self.disconnect_case(False)

    def test_disconnect_restores_owned_ram_instruction(self):
        self.disconnect_case(True)

    def test_step_fault_eviction_removes_owned_hardware_comparator(self):
        self.eviction_case(False)

    def test_step_fault_eviction_restores_owned_ram_instruction(self):
        self.eviction_case(True)

    def test_explicit_public_remove_before_disconnect_clears_hardware_and_ram(self):
        for ram in (False, True):
            with self.subTest(ram=ram):
                self.disconnect_case(ram, remove_first=True)


class M07HardwareOnlyTests(_Connected):
    def test_exhaustion_never_patches_writable_flash_and_owned_steps_use_every_comparator(self):
        with self.bench(writable_flash=True) as bench:
            model = bench.model
            owned = (LOOP_CALL, PROBE, PROBE + 2, PROBE + 4, PROBE + 6, PROBE + 8)
            for address in owned:
                bench.set_bp(address)
            installed = list(model.comparators)
            self.assertEqual(sorted(installed[:6]), sorted(rev1_comparator(a) for a in owned))
            refused = bench.raw("set_breakpoint_raw", address=0x080000EC)
            self.assertEqual(refused.get("status"), "error", refused)
            self.assertIn("Hardware breakpoint capacity exhausted at 0x080000EC",
                          refused["message"])
            self.assertIn("No code-memory write was attempted", refused["message"])
            self.assertEqual(model.code_writes(), [])
            # Owned stepping with all six comparators in use: BL, PUSH, MOV, NOP, NOP.
            for pc_before, pc_after, mnemonic in (
                (LOOP_CALL, PROBE, "bl"), (PROBE, PROBE + 2, "push"), (PROBE + 2, PROBE + 4, "mov"),
                (PROBE + 4, PROBE + 6, "nop"), (PROBE + 6, PROBE + 8, "nop"),
            ):
                with self.subTest(pc=hex(pc_before)):
                    self.assertEqual(bench.register("pc"), pc_before)
                    mark = len(model.executed)
                    ok, reply = bench.step()
                    self.assertTrue(ok, reply)
                    self.assertEqual(reply.splitlines()[0], stepped(pc_after) + CONFIRMED)
                    self.assertEqual([row[2] for row in self.executed(model, mark)], [mnemonic])
                    self.assertEqual(model.comparators, installed)
            self.assertEqual(model.code_writes(), [])


def _pid_absent(pid: int, created: float) -> bool:
    try:
        return psutil.Process(pid).create_time() != created
    except psutil.NoSuchProcess:
        return True


class ProcessWorkerStepTests(_Connected):
    """The production process worker, its RPC/serialization and its deadline."""

    def observation_root(self) -> Path:
        configured = os.environ.get("M08T_WORKER_OBSERVATIONS")
        if configured:
            return Path(configured)
        temporary = tempfile.TemporaryDirectory(prefix="m08t-worker-")
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    @contextlib.contextmanager
    def worker(self, mode: str) -> Iterator[tuple[Bench, dict[str, Any]]]:
        root = self.observation_root()
        root.mkdir(parents=True, exist_ok=True)
        # A fresh directory per worker, so only this worker's snapshots are read.
        observation = Path(tempfile.mkdtemp(prefix=f"{self._testMethodName}-{mode}-", dir=root))
        backend = ProcessIsolatedSWDInterface(worker_argv=(
            sys.executable, "-B", "-m", "tests.m08_independent_core", mode, str(observation)))
        record: dict[str, Any] = {"mode": mode, "observation": str(observation)}
        with self.bench(backend=backend) as bench:
            worker = bench.server.connection_manager.connection_for(BOARD).handle.worker
            owned = psutil.Process(worker._process.pid)
            imports = list(observation.glob("worker-*-imports.json"))
            self.assertEqual(len(imports), 1, imports)
            interpreter = psutil.Process(int(imports[0].name.split("-")[1]))
            self.assertTrue(interpreter.pid == owned.pid
                            or owned.pid in {parent.pid for parent in interpreter.parents()})
            names = {row["module"] for row in json.loads(imports[0].read_text(encoding="utf-8"))}
            self.assertTrue({"__main__", "pyocd_debug_mcp.adapters.provider_worker_runtime",
                             "pyocd_debug_mcp.adapters.swd_pyocd", "pyocd.coresight.cortex_m",
                             "pyocd.coresight.fpb"} <= names)
            record.update(owned_pid=owned.pid, owned_created=owned.create_time(),
                          interpreter_pid=interpreter.pid,
                          interpreter_created=interpreter.create_time())
            record["state_file"] = str(observation / f"worker-{interpreter.pid}-state.json")
            yield bench, record
        record.update(raw_child_exit=worker._process.returncode, closed=worker._closed,
                      cleanup_confirmed=worker._cleanup_confirmed)
        record["owned_absent"] = _pid_absent(record["owned_pid"], record["owned_created"])
        record["interpreter_absent"] = _pid_absent(record["interpreter_pid"],
                                                   record["interpreter_created"])
        journal({"test": self.id(), "worker": record})
        self.assertIsNotNone(worker._process.returncode)
        self.assertTrue(worker._closed)
        self.assertTrue(record["owned_absent"])
        self.assertTrue(record["interpreter_absent"])

    @staticmethod
    def worker_state(record: dict[str, Any]) -> dict[str, Any]:
        return json.loads(Path(record["state_file"]).read_text(encoding="utf-8"))

    def test_worker_reports_executed_owned_step_and_typed_incomplete_step(self):
        for mode in ("execute", "noop"):
            with self.subTest(mode=mode), self.worker(mode) as (bench, record):
                bench.set_bp(PROBE)
                comparator = bench.comparator_address(PROBE)
                bench.resume_to(PROBE)
                sp, r7, lr = (bench.register(name) for name in ("sp", "r7", "lr"))
                mark = len(self.worker_state(record)["executed"])
                ok, reply = bench.step()
                ran = [(row["pc"], row["encoding"], row["mnemonic"])
                       for row in self.worker_state(record)["executed"][mark:]]
                if mode == "execute":
                    self.assertTrue(ok, reply)
                    self.assertEqual(reply.splitlines()[0], stepped(PROBE + 2) + CONFIRMED)
                    self.assertEqual(ran, [(PROBE, "B580", "push")])
                    self.assertEqual(bench.register("sp"), sp - 8)
                    self.assertEqual(bench.read(sp - 8), r7)
                    self.assertEqual(bench.read(sp - 4), lr)
                else:
                    self.assertFalse(ok, reply)
                    self.assertNotIn("Stepped", reply)
                    self.assertIn("Incomplete step: state=HALTED pc=0x08000058 "
                                  "step_halt_confirmed=false dfsr=0x0", reply)
                    self.assertEqual(ran, [])
                    self.assertEqual(bench.register("sp"), sp)
                self.assertEqual(bench.state(), "HALTED")
                self.assertEqual(bench.read(comparator), rev1_comparator(PROBE))
                bench.remove_bp(PROBE)
                self.assertEqual(bench.read(comparator), 0)
                self.assertEqual(self.worker_state(record)["flash_writes"], [])

    def test_worker_hung_in_step_request_is_terminated_and_never_reports_success(self):
        with self.worker("hang") as (bench, record):
            bench.set_bp(PROBE)
            bench.resume_to(PROBE)
            started = time.monotonic()
            ok, reply = bench.step()
            record["step_seconds"] = time.monotonic() - started
            record["step_reply"] = reply
            self.assertFalse(ok, reply)
            self.assertNotIn("Stepped", reply)
            hung = self.worker_state(record)
            self.assertEqual(hung["note"], "transport hung in DHCSR step request")
            self.assertEqual(hung["step_requests"], 1)
            record["owned_absent_after_step"] = _pid_absent(record["owned_pid"],
                                                            record["owned_created"])
            self.assertTrue(record["owned_absent_after_step"])
            ok, after = bench.attempt("get_state")
            record["get_state_after"] = (ok, after)
            self.assertNotIn("HALTED", after if ok else "")


if __name__ == "__main__":
    unittest.main()
