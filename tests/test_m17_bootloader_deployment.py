"""Implementation proof of public bootloader deployment; host-only synthetic I/O."""

from __future__ import annotations

import struct
import unittest

from mcp.server.fastmcp.exceptions import ToolError

from tests.m02_independent_support import BASE, pattern
from tests.m05_public_support import NULL_PLAN
from tests.m17_public_support import (
    BOOT_SIZE,
    SECTOR,
    admit_and_validate,
    boot_artifact,
    boot_plan,
    deployment_fixture,
    execute_boot,
)


class PublicBootloaderDeploymentTests(unittest.TestCase):
    def assert_bytes(self, f, runs):
        for address, data in runs:
            self.assertEqual(
                bytes(f.target.memory[address - BASE : address - BASE + len(data)]), data
            )

    def test_full_setup_partition_plan_verified_write_preservation_and_public_restoration(self):
        with deployment_fixture(self) as f:
            f.allocate(0x8000)
            application = f.record().document["map_snapshot"]["deployment_policy"]
            admit_and_validate(self, f)
            saved = f.record()
            before = bytes(f.target.memory)
            artifact, runs = boot_artifact(f)
            f.target.calls.clear()
            f.target.reads.clear()
            accepted = boot_plan(f, artifact)
            self.assertEqual(accepted["status"], "plan_accepted", accepted)
            # The generated public fallback must use the same guarded action.
            result = f.flow.execute(accepted["stable_client_fallback"])
            self.assertEqual(result["status"], "batch_completed", result)
            text = str(result)
            self.assertIn("680 matching addressed bytes", text)
            self.assertIn("target left running", text)
            self.assert_bytes(f, runs)
            self.assertEqual(f.target.reads, [(address, len(data)) for address, data in runs])
            affected = {0, 0x2000}
            for offset in range(0, len(before), SECTOR):
                if offset not in affected:
                    self.assertEqual(
                        f.target.memory[offset : offset + SECTOR], before[offset : offset + SECTOR]
                    )
            self.assertEqual(f.record().data, saved.data)
            self.assertEqual(f.record().document["map_snapshot"]["deployment_policy"], application)
            names = [row[0] for row in f.target.calls]
            self.assertLess(names.index("program"), names.index("read"))
            self.assertLess(
                max(i for i, name in enumerate(names) if name == "read"), names.index("reset")
            )
            calls = list(f.target.calls)
            with self.assertRaisesRegex(ToolError, "locked"):
                execute_boot(f, artifact)
            self.assertEqual(f.target.calls, calls, "one-use plan cannot program again")

            # Publicly program the complete saved user partition, then revalidate.
            restore, original = boot_artifact(
                f, name="restore.elf", runs=((BASE, before[:BOOT_SIZE]),)
            )
            self.assertEqual(boot_plan(f, restore)["status"], "plan_accepted")
            self.assertIn(f"{BOOT_SIZE} matching addressed bytes", execute_boot(f, restore))
            self.assert_bytes(f, original)
            self.assertEqual(bytes(f.target.memory), before)
            self.assertEqual(f.refresh()["status"], "safety_refresh_completed")
            f.start()
            self.assertIsNone(f.server.gate_manager.snapshot(f.board))
            f.reconnect()
            self.assertEqual(f.validate()["status"], "validation_passed")
            self.assertEqual(f.caps()["identity"]["capability"], "exact")
            self.assertEqual(f.record().data, saved.data)

    def test_verified_programming_does_not_promise_startup_when_final_reset_fails(self):
        with deployment_fixture(self) as f:
            admit_and_validate(self, f)
            artifact, runs = boot_artifact(f)
            self.assertEqual(boot_plan(f, artifact)["status"], "plan_accepted")
            f.target.reset_fault = True
            reply = execute_boot(f, artifact)
            self.assertIn("680 matching addressed bytes", reply)
            self.assertIn("final reset state is unconfirmed", reply)
            self.assertNotIn("target left running", reply)
            self.assert_bytes(f, runs)
            # Public plan instructions must agree with this successful result.
            instructions = f.flow.call("flash_bootloader-plan", NULL_PLAN)
            self.assertNotIn("The target runs after flashing", instructions)
            self.assertIn("reset_state_unconfirmed", instructions)

    def test_verified_programming_reports_observed_halted_state(self):
        with deployment_fixture(self) as f:
            admit_and_validate(self, f)
            artifact, runs = boot_artifact(f)
            self.assertEqual(boot_plan(f, artifact)["status"], "plan_accepted")
            f.target.final_state = "HALTED"
            reply = execute_boot(f, artifact)
            self.assertIn("680 matching addressed bytes", reply)
            self.assertIn("target left halted", reply)
            self.assertNotIn("target left running", reply)
            self.assert_bytes(f, runs)

    def test_noop_programmer_fails_readback_and_spends_plan_without_widening_ownership(self):
        with deployment_fixture(self) as f:
            admit_and_validate(self, f)
            artifact, _ = boot_artifact(f)
            self.assertEqual(boot_plan(f, artifact)["status"], "plan_accepted")
            saved, before = f.record(), bytes(f.target.memory)
            f.target.calls.clear()
            f.target.program_mode = "noop"
            with self.assertRaisesRegex(ToolError, "does not match"):
                execute_boot(f, artifact)
            self.assertEqual(bytes(f.target.memory), before)
            self.assertEqual(f.record().data, saved.data)
            self.assertNotIn(("reset",), f.target.calls)
            calls = list(f.target.calls)
            with self.assertRaisesRegex(ToolError, "locked"):
                execute_boot(f, artifact)
            self.assertEqual(f.target.calls, calls)
            f.target.program_mode = "write"
            self.assertEqual(f.validate()["status"], "validation_passed")
            self.assertEqual(boot_plan(f, artifact)["status"], "plan_accepted")
            self.assertIn("680 matching addressed bytes", execute_boot(f, artifact))

    def test_absent_partition_permission_and_outside_images_never_program(self):
        with deployment_fixture(self) as f:
            artifact, _ = boot_artifact(f)
            self.assertEqual(boot_plan(f, artifact)["status"], "plan_accepted")
            calls = list(f.target.calls)
            with self.assertRaisesRegex(ToolError, "exact live silicon proof"):
                execute_boot(f, artifact)
            self.assertEqual(f.target.calls, calls)
            admit_and_validate(self, f)
            with self.assertRaisesRegex(
                ToolError, "requires one-time or full-session user approval"
            ):
                boot_plan(f, artifact, permission=None)
            saved, before = f.record(), bytes(f.target.memory)
            calls = list(f.target.calls)
            for name, runs in (
                (
                    "oversize.elf",
                    (
                        (
                            BASE,
                            struct.pack("<II", 0x20001000, BASE + 0x101) + pattern(BOOT_SIZE, 41),
                        ),
                    ),
                ),
                (
                    "application.elf",
                    (
                        (
                            BASE + 0x8000,
                            struct.pack("<II", 0x20001000, BASE + 0x8101) + pattern(512, 51),
                        ),
                    ),
                ),
                (
                    "rom.elf",
                    ((0x1FFF0000, struct.pack("<II", 0x20001000, 0x1FFF0101) + pattern(512, 61)),),
                ),
            ):
                with self.subTest(image=name):
                    wrong, _ = boot_artifact(f, name=name, runs=runs)
                    self.assertEqual(boot_plan(f, wrong)["status"], "plan_accepted")
                    with self.assertRaisesRegex(ToolError, "outside|partition|region"):
                        execute_boot(f, wrong)
                    self.assertEqual(f.target.calls, calls)
                    self.assertEqual(bytes(f.target.memory), before)
                    self.assertEqual(f.record().data, saved.data)
            self.assertEqual(boot_plan(f, artifact)["status"], "plan_accepted")
            raw = bytearray(artifact.read_bytes())
            raw[-1] ^= 1
            artifact.write_bytes(raw)
            with self.assertRaisesRegex(ToolError, "changed"):
                execute_boot(f, artifact)
            self.assertEqual(f.target.calls, calls)
            self.assertEqual(bytes(f.target.memory), before)

    def test_reconnected_or_changed_unit_cannot_spend_previous_approval(self):
        with deployment_fixture(self) as f:
            admit_and_validate(self, f)
            artifact, _ = boot_artifact(f)
            self.assertEqual(boot_plan(f, artifact)["status"], "plan_accepted")
            before = bytes(f.target.memory)
            f.call("disconnect", dict(board_id=f.board))
            f.reconnect()
            calls = list(f.target.calls)
            with self.assertRaisesRegex(ToolError, "locked"):
                execute_boot(f, artifact)
            self.assertEqual(f.target.calls, calls)
            self.assertEqual(f.validate()["status"], "validation_passed")
            with self.assertRaisesRegex(ToolError, "locked"):
                execute_boot(f, artifact)
            self.assertEqual(f.target.calls, calls)
            unique = next(
                row["address"] for row in f.factory.request["registers"] if row["role"] == "unique"
            )
            f.backend.factory.values[unique] ^= 1
            self.assertNotEqual(f.validate()["status"], "validation_passed")
            self.assertEqual(boot_plan(f, artifact)["status"], "plan_accepted")
            with self.assertRaisesRegex(ToolError, "gate|validat"):
                execute_boot(f, artifact)
            self.assertEqual(bytes(f.target.memory), before)


if __name__ == "__main__":
    unittest.main()
