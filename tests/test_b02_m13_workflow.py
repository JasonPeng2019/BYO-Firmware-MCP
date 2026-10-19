"""Independent M13 public no-allocation -> real verified flash -> breakpoint proof."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
from unittest.mock import patch
import unittest

from tests.b02_public_support import RAM, RAM_BYTES, elf_fixture, independently_resolve
from tests import m02_independent_support as boundary
from tests.m05_public_support import NULL_PLAN, PublicSetup


class DebugBoundary(boundary.BoundaryAdapter):
    """Controlled breakpoint/register I/O; inherited flash implementation stays real."""

    def __init__(self, target):
        super().__init__(target)
        self.breakpoints = set()
        self.pc = 0

    def set_breakpoint(self, handle, address):
        self.target.calls.append(("set_breakpoint", address))
        self.breakpoints.add(address)

    def remove_breakpoint(self, handle, address):
        self.target.calls.append(("remove_breakpoint", address))
        self.breakpoints.discard(address)

    def resume(self, handle):
        self.target.calls.append(("resume",))
        self.target.state = "RUNNING"
        if self.breakpoints:
            self.pc = min(self.breakpoints)
            self.target.state = "HALTED"

    def halt(self, handle):
        self.target.calls.append(("halt",))
        self.target.state = "HALTED"

    def read_core_register(self, handle, name):
        self.target.calls.append(("register", name))
        return self.pc

    def supported_core_registers(self, handle):
        return ("pc", "sp", "lr", "xpsr")


@contextmanager
def workflow(test):
    class RamBoundary(boundary.MemoryBoundary):
        def read_memory(self, address, width=32, **kwargs):
            if RAM <= address < RAM + len(RAM_BYTES):
                self.calls.append(("read-ram", address, width))
                return int.from_bytes(RAM_BYTES[address-RAM:address-RAM+width//8], "little")
            return super().read_memory(address, width, **kwargs)

    with (patch.object(boundary, "BoundaryAdapter", DebugBoundary),
          patch.object(boundary, "MemoryBoundary", RamBoundary)):
        with boundary.public_fixture(test, guarded=True) as f:
            f.flow = PublicSetup(f.server, call=f.call)
            f.elf = elf_fixture(f.root / "b02.elf")
            try:
                yield f
            finally:
                evidence = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / test.id()
                evidence.mkdir(parents=True, exist_ok=True)
                shutil.copytree(f.root / ".firm", evidence / ".firm", dirs_exist_ok=True)
                shutil.copyfile(f.elf, evidence / f.elf.name)
                (evidence / "wire.json").write_text(json.dumps(f.wire, indent=2), encoding="utf-8")
                (evidence / "target.json").write_text(json.dumps(f.target.calls, default=str), encoding="utf-8")


class IndependentAllocation(unittest.TestCase):
    @staticmethod
    def policy(f):
        return f.server._capability_policies.resolve(boundary.BOARD).map_snapshot

    def bp_plan(self, f, *, location="entry", elf=None):
        f.flow.call("set_breakpoint-plan", NULL_PLAN)
        return f.flow.plan("set_breakpoint", boundary.BOARD,
                           {"symbol_or_address": location, "elf_artifact": str(elf or f.elf)})

    def refuse_breakpoint(self, f, *, elf=None, message="has no application deployment allocation yet"):
        accepted = self.bp_plan(f, elf=elf)
        self.assertEqual(accepted["status"], "plan_accepted", accepted)
        rejected = f.flow.execute(accepted["stable_client_fallback"])
        self.assertEqual(rejected["status"], "batch_failed", rejected)
        self.assertEqual(rejected["completed"], [])
        self.assertIn(message, str(rejected))
        return rejected

    def deploy(self, f):
        f.flow.call("flash_application-plan", NULL_PLAN)
        accepted = f.flow.plan("flash_application", boundary.BOARD, {"artifact": str(f.elf)})
        self.assertEqual(accepted["status"], "plan_accepted", accepted)
        reply = f.flow.call("flash_application", {"board_id": boundary.BOARD, "artifact": str(f.elf)})
        self.assertIn("read back 256 matching addressed bytes", reply)
        self.assertIn("target left running", reply)
        # Source-independent ELF PT_LOAD bytes, not the FlashResult's assertion.
        loads = boundary.separate_loads(f.elf)
        for address, data in loads:
            self.assertEqual(bytes(f.target.memory[address-boundary.BASE:address-boundary.BASE+len(data)]), data)
        policy = self.policy(f)
        self.assertIsNotNone(policy["partitions"]["application"])
        self.assertNotEqual(policy["deployment_policy"]["kind"], "none")
        self.assertEqual(f.server._current_symbol_artifacts[boundary.BOARD][0], f.elf)
        return reply

    def test_public_refusal_refresh_then_advertised_verified_deployment_and_same_breakpoint(self):
        with workflow(self) as f:
            policy = self.policy(f)
            self.assertEqual(policy["deployment_policy"], {"kind": "none"})
            self.assertIsNone(policy["partitions"]["application"])
            memory = bytes(f.target.memory)
            calls = list(f.target.calls)
            rejected = self.refuse_breakpoint(f)
            self.assertIn("flash_application-plan", str(rejected))
            self.assertIn("flash_application", str(rejected))
            self.assertEqual(f.target.calls, calls, "initial refusal has zero target effect")
            self.assertEqual(bytes(f.target.memory), memory)
            self.assertEqual(self.policy(f), policy)
            f.flow.json("load_setup_tool", {"board_id": boundary.BOARD, "tool_name": "board_safety_refresh"})
            refresh = f.flow.json("board_safety_refresh", {"board_id": boundary.BOARD})
            self.assertEqual(refresh["status"], "safety_refresh_completed", refresh)
            self.assertEqual(self.policy(f)["deployment_policy"], {"kind": "none"})
            self.assertEqual(bytes(f.target.memory), memory)
            self.refuse_breakpoint(f)
            self.assertFalse(any(r[0] in {"program", "set_breakpoint"} for r in f.target.calls))
            self.deploy(f)
            accepted = self.bp_plan(f)
            self.assertEqual(accepted["status"], "plan_accepted", accepted)
            address, _ = independently_resolve(f.elf, "entry")
            address &= ~1
            reply = f.flow.call("set_breakpoint", {"board_id": boundary.BOARD,
                                "symbol_or_address": "entry", "elf_artifact": str(f.elf)})
            self.assertIn(f"0x{address:08X}", reply)
            f.flow.call("resume", {"board_id": boundary.BOARD})
            self.assertIn("halted", f.flow.call("get_state", {"board_id": boundary.BOARD}).lower())
            self.assertIn(f"0x{address:08X}", f.flow.call("read_execution_state", {"board_id": boundary.BOARD, "name": "pc"}))
            f.flow.call("remove_breakpoint", {"board_id": boundary.BOARD, "address": address})
            f.flow.call("resume", {"board_id": boundary.BOARD})
            self.assertIn("running", f.flow.call("get_state", {"board_id": boundary.BOARD}).lower())

    def test_outside_allocation_and_changed_plan_elf_remain_refused(self):
        with workflow(self) as f:
            self.deploy(f)
            outside = elf_fixture(f.root / "outside.elf", base=boundary.BASE + 0x9000)
            before = list(f.target.calls)
            self.refuse_breakpoint(f, elf=outside, message="outside every reviewed executable deployment partition")
            self.assertEqual(f.target.calls, before)
            accepted = self.bp_plan(f)
            self.assertEqual(accepted["status"], "plan_accepted", accepted)
            original = f.elf.read_bytes()
            try:
                f.elf.write_bytes(original + b"changed after acceptance")
                before = list(f.target.calls)
                with self.assertRaisesRegex(Exception, "artifact.changed|artifact changed"):
                    f.flow.call("set_breakpoint", {"board_id": boundary.BOARD,
                                "symbol_or_address": "entry", "elf_artifact": str(f.elf)})
                self.assertEqual(f.target.calls, before)
            finally:
                f.elf.write_bytes(original)

    def test_genuinely_damaged_projection_is_restored_by_refresh_without_new_ownership(self):
        with workflow(self) as f:
            self.deploy(f)
            projection = f.root / ".firm/safety" / boundary.BOARD / "memory_map.yaml"
            before = self.policy(f)
            projection.write_text("damaged compatibility projection", encoding="utf-8")
            self.assertNotEqual(projection.read_text(), json.dumps(before))
            # Immutable committed authority remains intact. The public loader and
            # refresh repair the damaged projection without a new deployment.
            calls = list(f.target.calls)
            f.flow.json("load_setup_tool", {"board_id": boundary.BOARD, "tool_name": "board_safety_refresh"})
            reply = f.flow.json("board_safety_refresh", {"board_id": boundary.BOARD})
            self.assertEqual(reply["status"], "safety_refresh_completed", reply)
            import yaml
            self.assertEqual(yaml.safe_load(projection.read_text()), self.policy(f))
            self.assertEqual(self.policy(f)["deployment_policy"], before["deployment_policy"])
            self.assertEqual(f.target.calls, calls)

    def test_wrong_elf_executable_location_is_refused(self):
        with workflow(self) as f:
            self.deploy(f)
            # Same valid allocated address, wrong ELF executable coverage.
            wrong = elf_fixture(f.root / "wrong.elf", base=boundary.BASE + 0x100)
            before = list(f.target.calls)
            accepted = self.bp_plan(f, location=boundary.BASE + 0x20, elf=wrong)
            reply = f.flow.execute(accepted["stable_client_fallback"])
            self.assertEqual(reply["status"], "batch_failed", reply)
            self.assertIn("executable", str(reply))
            self.assertEqual(f.target.calls, before)


if __name__ == "__main__":
    unittest.main()
