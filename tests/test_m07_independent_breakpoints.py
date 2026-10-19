"""F11/R06 independent regressions using the installed pyOCD manager/providers."""

import unittest
import struct
from unittest.mock import patch

from pyocd.core.memory_map import MemoryMap
from pyocd.core.target import Target

from pyocd_debug_mcp.adapters.swd_pyocd import PyOCDSWDInterface
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from pyocd_debug_mcp.target_errors import TargetControlError, TargetStateError
from tests.m07_breakpoint_boundary import BoundaryAdapter, MemoryTarget, handle_for
from tests.tiered_acceptance_support import (
    isolated_project,
    public_call,
    public_text,
    tiered_test_server,
)

PROBE_INVENTORY = ValidationInventory(
    probes=(ValidationProbe("m07-test", "M07 fixture", "jlink", "m07-test"),)
)


class IndependentBreakpointTests(unittest.TestCase):
    def test_already_full_flash_has_zero_attempted_code_writes(self):
        target = MemoryTarget(capacity=0)
        error = None
        try:
            PyOCDSWDInterface().set_breakpoint(handle_for(target), target.flash_start)
        except TargetControlError as caught:
            error = caught
        self.assertEqual(target.code_writes, [], "even a restored write attempt violates F11")
        self.assertIsNotNone(error, "exhaustion must refuse")
        self.assertIn("capacity exhausted", str(error))

    def test_boundary_control_auto_really_patches_writable_flash_when_full(self):
        target = MemoryTarget(capacity=0)
        address = target.flash_start
        before = target.read16(address)
        self.assertTrue(target.set_breakpoint(address))
        target.bp_manager.flush()
        self.assertEqual(target.find_breakpoint(address).type, Target.BreakpointType.SW)
        self.assertEqual(target.words[address], 0xBE00)
        self.assertEqual(target.code_writes, [(address, 0xBE00, 16)])
        target.remove_breakpoint(address)
        target.bp_manager.flush()
        self.assertEqual(target.words[address], before)
        self.assertEqual(target.code_writes[-1], (address, before, 16))

    def test_flash_exhaustion_zero_attempts_and_repeated_valid_successors(self):
        for capacity, flash_start in ((1, 0x1000), (3, 0x08000000), (5, 0x10000)):
            with self.subTest(capacity=capacity, flash_start=flash_start):
                target = MemoryTarget(capacity, flash_start)
                adapter, handle = PyOCDSWDInterface(), handle_for(target)
                discovered = target.fpb.available_breakpoints
                owned = [flash_start + 2 * index for index in range(discovered)]
                extra = flash_start + 2 * discovered
                before = target.read16(extra)
                for address in owned:
                    adapter.set_breakpoint(handle, address | 1)
                self.assertTrue(all(kind == Target.BreakpointType.HW
                                    for _, kind in target.requests))
                self.assertEqual(target.fpb.available_breakpoints, 0)
                installed = target.comparators.copy()
                attempts = target.writes.copy()
                for _ in range(2):
                    with self.assertRaisesRegex(TargetControlError, "Hardware.*capacity exhausted"):
                        adapter.set_breakpoint(handle, extra)
                    self.assertEqual(target.writes, attempts)
                    self.assertEqual(target.comparators, installed)
                    self.assertEqual(adapter.read_memory(handle, extra, 16), before)
                self.assertEqual(target.code_writes, [])
                self.assertEqual(target.software._breakpoints, {})
                for address in owned:
                    adapter.remove_breakpoint(handle, address)
                self.assertTrue(all(value == 0 for value in target.comparators.values()))
                self.assertEqual(target.fpb.available_breakpoints, discovered)

    def test_duplicate_and_foreign_queued_auto_are_not_owned_or_flushed(self):
        target = MemoryTarget(capacity=1)
        adapter, handle = PyOCDSWDInterface(), handle_for(target)
        address = target.flash_start
        adapter.set_breakpoint(handle, address)
        installed = target.find_breakpoint(address)
        before = target.writes.copy()
        adapter.set_breakpoint(handle, address | 1)
        self.assertIs(target.find_breakpoint(address), installed)
        self.assertEqual(target.writes, before)
        foreign = address + 2
        self.assertTrue(target.set_breakpoint(foreign, Target.BreakpointType.AUTO))
        queued = target.find_breakpoint(foreign)
        with self.assertRaisesRegex(TargetStateError, "does not own"):
            adapter.set_breakpoint(handle, foreign)
        self.assertIs(target.find_breakpoint(foreign), queued)
        self.assertIs(target.find_breakpoint(address), installed)
        self.assertEqual(target.writes, before)
        self.assertEqual(target.code_writes, [])
        # The foreign owner cancels its own request before our normal cleanup.
        target.remove_breakpoint(foreign)
        adapter.remove_breakpoint(handle, address)

    def test_unknown_map_and_fpb_unsupported_code_never_fall_back(self):
        for unknown in (False, True):
            target = MemoryTarget(flash_start=0x60000000)
            if unknown:
                target.memory_map = MemoryMap()
            with self.assertRaisesRegex(TargetControlError, "unsupported"):
                PyOCDSWDInterface().set_breakpoint(handle_for(target), target.flash_start)
            self.assertEqual(target.requests, [])
            self.assertEqual(target.writes, [])

    def test_supported_ram_uses_real_software_provider_and_restores_saved_bytes(self):
        target = MemoryTarget()
        address, saved = 0x20000000, 0x465A
        target.words[address] = saved
        adapter, handle = PyOCDSWDInterface(), handle_for(target)
        adapter.set_breakpoint(handle, address)
        bp = target.find_breakpoint(address)
        self.assertEqual(target.requests, [(address, Target.BreakpointType.AUTO)])
        self.assertEqual((bp.original_instr, target.words[address]), (saved, 0xBE00))
        adapter.remove_breakpoint(handle, address)
        self.assertEqual(target.words[address], saved)
        self.assertEqual(target.code_writes, [(address, 0xBE00, 16), (address, saved, 16)])
        self.assertEqual(target.software._breakpoints, {})
        self.assertIsNone(target.find_breakpoint(address))
        self.assertTrue(all(value == 0 for value in target.comparators.values()))

    def test_failed_ram_transaction_proven_restoration_has_truthful_advice(self):
        target = MemoryTarget()
        address, saved = 0x20000000, 0x4629
        target.words[address] = saved
        target.fail_flushes = {1}
        with self.assertRaisesRegex(TargetControlError, "Rollback verified") as raised:
            PyOCDSWDInterface().set_breakpoint(handle_for(target), address)
        self.assertIn("does not require reflashing", str(raised.exception))
        self.assertNotIn("Disconnect", str(raised.exception))
        self.assertEqual(target.code_writes, [(address, 0xBE00, 16), (address, saved, 16)])
        self.assertEqual(target.words[address], saved)
        self.assertIsNone(target.find_breakpoint(address))
        self.assertEqual(target.software._breakpoints, {})

    def test_ram_manager_absence_does_not_prove_restoration(self):
        target = MemoryTarget()
        address, saved = 0x20000000, 0x4637
        target.words[address] = saved
        target.fail_flushes = {1}
        target.drop_restore = True
        with self.assertRaisesRegex(TargetStateError, "uncertain") as raised:
            PyOCDSWDInterface().set_breakpoint(handle_for(target), address)
        self.assertIn("restore/reflash", str(raised.exception))
        self.assertNotIn("Rollback verified", str(raised.exception))
        self.assertEqual(target.code_writes, [(address, 0xBE00, 16), (address, saved, 16)])
        self.assertEqual(target.words[address], 0xBE00)
        self.assertIsNone(target.find_breakpoint(address))
        self.assertEqual(target.software._breakpoints, {})

    def test_unreadable_restored_ram_stays_uncertain(self):
        target = MemoryTarget()
        address, saved = 0x20000000, 0x4638
        target.words[address] = saved
        target.fail_raw_reads = True
        # The software provider's own original read must succeed before we lose
        # physical readback. Inject the loss at the transfer-flush boundary.
        original_flush = target.flush

        def flush():
            target.fail_raw_reads = True
            original_flush()

        target.fail_raw_reads = False
        target.flush = flush
        with self.assertRaisesRegex(TargetStateError, "uncertain") as raised:
            PyOCDSWDInterface().set_breakpoint(handle_for(target), address)
        self.assertIn("raw instruction read failed", str(raised.exception))
        self.assertNotIn("Rollback verified", str(raised.exception))
        self.assertEqual(target.words[address], saved)
        self.assertEqual(target.code_writes, [(address, 0xBE00, 16), (address, saved, 16)])

    def test_removal_cannot_claim_success_from_manager_absence_with_physical_residue(self):
        for ram in (True, False):
            with self.subTest(ram=ram):
                target = MemoryTarget()
                adapter, handle = PyOCDSWDInterface(), handle_for(target)
                address = 0x20000000 if ram else target.flash_start
                adapter.set_breakpoint(handle, address)
                target.drop_restore = ram
                target.drop_comparator_clear = not ram
                with self.assertRaisesRegex(TargetStateError, "uncertain"):
                    adapter.remove_breakpoint(handle, address)
                self.assertIsNone(target.find_breakpoint(address))
                if ram:
                    self.assertEqual(target.words[address], 0xBE00)
                else:
                    self.assertTrue(any(target.comparators.values()))

    def test_hardware_manager_absence_and_provider_flush_failure_stay_uncertain(self):
        for fault in ("provider", "clear", "flush"):
            with self.subTest(fault=fault):
                target = MemoryTarget()
                target.fail_after_comparator_write = fault == "provider"
                target.drop_comparator_clear = fault == "clear"
                target.fail_flushes = {1, 2} if fault == "flush" else {1}
                with self.assertRaisesRegex(TargetStateError, "uncertain") as raised:
                    PyOCDSWDInterface().set_breakpoint(handle_for(target), target.flash_start)
                self.assertNotIn("Rollback verified", str(raised.exception))
                self.assertIn("power-cycle", str(raised.exception))
                self.assertNotIn("reflash", str(raised.exception))
                self.assertEqual(target.code_writes, [])
                self.assertIsNone(target.find_breakpoint(target.flash_start))
                if fault != "flush":
                    self.assertTrue(any(target.comparators.values()))

    def test_verified_hardware_rollback_observes_physical_comparator_clear(self):
        target = MemoryTarget()
        target.fail_flushes = {1}
        with self.assertRaisesRegex(TargetControlError, "hardware comparator was read back clear"):
            PyOCDSWDInterface().set_breakpoint(handle_for(target), target.flash_start)
        self.assertEqual(target.code_writes, [])
        self.assertTrue(all(value == 0 for value in target.comparators.values()))
        self.assertTrue(any(width == 32 and addr in target.comparators
                            for addr, width, _ in target.reads))

    def test_public_raw_dispatch_retains_session_after_real_manager_exhaustion(self):
        with (
            isolated_project() as root,
            tiered_test_server(root) as server,
            patch.object(server, "_validation_inventory", return_value=PROBE_INVENTORY),
        ):
            target = MemoryTarget(capacity=2)
            server.configure_tiered_test_seams(backend=BoundaryAdapter(target))
            self.assertIn("Connected", public_text(server, "connect", {
                "board_id": "m07", "probe_uid": "m07-test", "target": "cortex_m"}))
            connection = server.connection_manager.connection_for("m07")
            try:
                count = target.fpb.available_breakpoints
                for index in range(count):
                    result = public_call(server, "set_breakpoint_raw", {
                        "board_id": "m07", "address": target.flash_start + index * 2})
                    self.assertEqual(result["status"], "ok", result)
                for _ in range(2):
                    refused = public_call(server, "set_breakpoint_raw", {
                        "board_id": "m07", "address": target.flash_start + count * 2})
                    self.assertEqual(refused["status"], "error", refused)
                    self.assertIn("capacity exhausted", refused["message"])
                    read = public_call(server, "read_memory_raw", {
                        "board_id": "m07", "address": target.flash_start, "width": 16})
                    self.assertEqual(read["status"], "ok", read)
                    self.assertIs(server.connection_manager.connection_for("m07"), connection)
                    self.assertIsNone(connection.runtime_session.closed_at)
                self.assertEqual(target.code_writes, [])
                for index in range(count):
                    public_text(server, "remove_breakpoint", {
                        "board_id": "m07", "address": target.flash_start + index * 2})
                self.assertTrue(all(value == 0 for value in target.comparators.values()))
            finally:
                public_text(server, "disconnect", {"board_id": "m07"})

    def test_public_guarded_dispatch_keeps_real_plan_and_map_guards(self):
        with (
            isolated_project() as root,
            tiered_test_server(root) as server,
            patch.object(server, "_validation_inventory", return_value=PROBE_INVENTORY),
        ):
            target = MemoryTarget(capacity=1)
            repository = CapabilityPolicyRepository(root)
            server.configure_tiered_test_seams(
                backend=BoundaryAdapter(target), policy_repository=repository)
            public_text(server, "connect", {
                "board_id": "m07", "probe_uid": "m07-test", "target": "cortex_m"})
            repository.commit("m07", Tier.SETUP_LITE, map_snapshot={
                "board_id": "m07", "regions": [{
                    "name": "verified executable allocation", "kind": "application_flash",
                    "start": target.flash_start, "end": target.flash_start + 0x1000,
                    "readable": True, "writable": False, "executable": True,
                    "source_pages": [1], "source_note": "Independent controlled fixture map",
                }], "flash": {"backend_target": None, "erase_sectors": []},
            })
            # Real valid ELF header; lite dispatch uses the confirmed executable map.
            artifact = root / "controlled.elf"
            artifact.write_bytes(struct.pack(
                "<16sHHIIIIIHHHHHH", b"\x7fELF\x01\x01\x01" + bytes(9),
                2, 40, 1, target.flash_start | 1, 0, 0, 0, 52, 32, 0, 40, 0, 0))
            parameters = {"symbol_or_address": target.flash_start, "elf_artifact": str(artifact)}
            null = dict.fromkeys(("board_id", "hypothesis", "strategy", "hypothesis_made",
                                  "strategy_evaluated", "expected_fail_return",
                                  "expected_success_return", "max_calls", "max_calls_buffer",
                                  "action_parameters", "user_permission"))

            def plan(address):
                parameters["symbol_or_address"] = address
                public_text(server, "set_breakpoint-plan", null)
                return public_text(server, "set_breakpoint-plan", {
                    "board_id": "m07", "hypothesis": "Hardware capacity permits one breakpoint.",
                    "strategy": "Use the controlled executable address then verify physical state.",
                    "hypothesis_made": True, "strategy_evaluated": True,
                    "expected_fail_return": "Clear capacity or map refusal before a code write.",
                    "expected_success_return": "A hardware comparator is installed.",
                    "max_calls": 1, "max_calls_buffer": 0, "action_parameters": dict(parameters),
                })

            try:
                with self.assertRaises(Exception):
                    public_text(server, "set_breakpoint", {"board_id": "m07", **parameters})
                self.assertEqual(target.requests, [], "unplanned call cannot reach the adapter")
                plan(target.flash_start)
                reply = public_text(server, "set_breakpoint", {"board_id": "m07", **parameters})
                self.assertIn("Breakpoint set", reply)
                plan(target.flash_start + 2)
                with self.assertRaisesRegex(Exception, "capacity exhausted"):
                    public_text(server, "set_breakpoint", {"board_id": "m07", **parameters})
                self.assertEqual(target.code_writes, [])
                self.assertEqual(target.requests, [(target.flash_start, Target.BreakpointType.HW)])
                public_text(server, "remove_breakpoint", {
                    "board_id": "m07", "address": target.flash_start})
                self.assertTrue(all(value == 0 for value in target.comparators.values()))
            finally:
                public_text(server, "disconnect", {"board_id": "m07"})


if __name__ == "__main__":
    unittest.main()
