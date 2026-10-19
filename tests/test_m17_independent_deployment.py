"""Fresh M17-T public behavioral oracles; all target I/O here is synthetic."""

from __future__ import annotations

import struct
import unittest

from mcp.server.fastmcp.exceptions import ToolError

from tests.m02_independent_support import pattern
from tests.m05_public_support import NULL_PLAN
from tests.m17_independent_support import (
    BASE,
    BOOT_END,
    admit,
    affected,
    assert_image,
    assert_outside,
    execute,
    image,
    plan,
    workflow,
)


class IndependentDeploymentTests(unittest.TestCase):
    def test_sparse_fallback_preserves_all_outside_sectors_and_restores_after_restart(self):
        with workflow(self) as f:
            f.allocate(0x10000)
            application = f.record().document["map_snapshot"]["deployment_policy"]
            admit(self, f)
            policy = f.record().data
            before = bytes(f.bus.memory)
            path, runs = image(f)
            f.bus.calls.clear()
            f.bus.reads.clear()
            accepted = plan(f, path)
            self.assertEqual(accepted["status"], "plan_accepted")
            result = f.flow.execute(accepted["stable_client_fallback"])
            self.assertEqual(result["status"], "batch_completed", result)
            self.assertIn("5144 matching addressed bytes", str(result))
            self.assertIn("target left running", str(result))
            assert_image(self, f, runs)
            assert_outside(self, f, before, runs)
            self.assertEqual({row[1] for row in f.bus.calls if row[0] == "erase"}, affected(runs))
            self.assertEqual(
                [(a, n) for a, n in f.bus.reads],
                [(BASE, 4096), (BASE + 4096, 915), (BASE + 0x4000, 133)],
            )
            events = [row[0] for row in f.bus.calls]
            self.assertLess(events.index("attempt"), events.index("readback"))
            self.assertLess(
                len(events) - 1 - events[::-1].index("readback"), events.index("final-reset")
            )
            self.assertEqual(f.record().data, policy)
            self.assertEqual(f.record().document["map_snapshot"]["deployment_policy"], application)
            count = len(f.bus.calls)
            with self.assertRaisesRegex(ToolError, "locked"):
                execute(f, path)
            self.assertEqual(len(f.bus.calls), count)

            # Persisted policy is replayed, while the old session/plan is not.
            f.start()
            self.assertIsNone(f.server.gate_manager.snapshot(f.board))
            f.reconnect()
            self.assertEqual(f.validate()["status"], "validation_passed")
            self.assertEqual(f.caps()["identity"]["capability"], "exact")
            self.assertEqual(f.record().data, policy)
            restore, saved = image(f, "restore.elf", ((BASE, before[:BOOT_END]),))
            self.assertEqual(plan(f, restore)["status"], "plan_accepted")
            self.assertIn(f"{BOOT_END} matching addressed bytes", execute(f, restore))
            assert_image(self, f, saved)
            self.assertEqual(bytes(f.bus.memory), before)
            self.assertEqual(f.refresh()["status"], "safety_refresh_completed")
            self.assertEqual(f.validate()["status"], "validation_passed")
            self.assertEqual(f.record().data, policy)

    def test_noop_cannot_claim_verified_programming_and_consumes_only_one_call(self):
        with workflow(self) as f:
            admit(self, f)
            path, runs = image(f)
            policy, before = f.record().data, bytes(f.bus.memory)
            self.assertEqual(plan(f, path)["status"], "plan_accepted")
            f.bus.mode = "noop"
            f.bus.calls.clear()
            with self.assertRaisesRegex(ToolError, "does not match"):
                execute(f, path)
            self.assertEqual(bytes(f.bus.memory), before)
            self.assertEqual(f.record().data, policy)
            self.assertEqual([r[0] for r in f.bus.calls].count("attempt"), 1)
            self.assertNotIn("final-reset", [r[0] for r in f.bus.calls])
            count = len(f.bus.calls)
            with self.assertRaisesRegex(ToolError, "locked"):
                execute(f, path)
            self.assertEqual(len(f.bus.calls), count)
            f.bus.mode = "write"
            self.assertEqual(f.validate()["status"], "validation_passed")
            self.assertEqual(plan(f, path)["status"], "plan_accepted")
            self.assertIn("5144 matching addressed bytes", execute(f, path))
            assert_image(self, f, runs)
            assert_outside(self, f, before, runs)

    def test_post_attempt_fault_retains_partition_without_false_success_or_retry(self):
        with workflow(self) as f:
            admit(self, f)
            path, runs = image(f)
            before, policy = bytes(f.bus.memory), f.record().data
            self.assertEqual(plan(f, path)["status"], "plan_accepted")
            f.bus.mode = "post-write-fault"
            f.bus.calls.clear()
            with self.assertRaisesRegex(ToolError, "after first addressed write"):
                execute(f, path)
            self.assertNotEqual(bytes(f.bus.memory), before)
            assert_outside(self, f, before, runs)
            self.assertEqual(f.record().data, policy)
            self.assertNotIn("final-reset", [r[0] for r in f.bus.calls])
            count = len(f.bus.calls)
            with self.assertRaisesRegex(ToolError, "locked|session|connect|validation"):
                execute(f, path)
            self.assertEqual(len(f.bus.calls), count)

    def test_missing_partition_permission_and_wrong_ranges_refuse_without_programmer(self):
        with workflow(self) as f:
            path, _ = image(f)
            self.assertEqual(plan(f, path)["status"], "plan_accepted")
            with self.assertRaisesRegex(ToolError, "exact live silicon proof"):
                execute(f, path)
            self.assertFalse(any(row[0] == "attempt" for row in f.bus.calls))
            admit(self, f)
            with self.assertRaisesRegex(
                ToolError, "requires one-time or full-session user approval"
            ):
                plan(f, path, None)
            policy, before = f.record().data, bytes(f.bus.memory)
            vector = struct.pack("<II", 0x20001000, BASE + 0x181)
            for name, runs, refusal in (
                # Valid in-partition stack/reset/entry; only the addressed
                # content exceeds the bootloader partition by one byte.
                (
                    "oversized.elf",
                    ((BASE, vector + pattern(BOOT_END + 1 - 8, 31)),),
                    "Flash artifact range .*not fully inside.*bootloader partition",
                ),
                (
                    "application.hex",
                    (
                        (
                            BASE + 0x10000,
                            struct.pack("<II", 0x20001000, BASE + 0x10081) + pattern(256, 42),
                        ),
                    ),
                    "reset handler is outside the reviewed deployment partition",
                ),
                # Valid boot vectors plus a separate prohibited addressed load.
                (
                    "rom.hex",
                    ((BASE, vector + pattern(512, 27)), (0x1FFF0000, pattern(256, 51))),
                    "Flash artifact range .*not fully inside.*bootloader partition",
                ),
                (
                    "option.hex",
                    ((BASE, vector + pattern(512, 27)), (0x1FFF7800, pattern(16, 71))),
                    "Flash artifact range .*not fully inside.*bootloader partition",
                ),
            ):
                with self.subTest(image=name):
                    invalid, _ = image(f, name, runs)
                    self.assertEqual(plan(f, invalid)["status"], "plan_accepted")
                    count = len(f.bus.calls)
                    with self.assertRaisesRegex(ToolError, refusal):
                        execute(f, invalid)
                    self.assertEqual(len(f.bus.calls), count)
                    self.assertEqual(bytes(f.bus.memory), before)
                    self.assertEqual(f.record().data, policy)

    def test_rebuild_reconnect_and_changed_unit_invalidate_approved_binding(self):
        with workflow(self) as f:
            admit(self, f)
            path, runs = image(f)
            before, policy = bytes(f.bus.memory), f.record().data
            self.assertEqual(plan(f, path)["status"], "plan_accepted")
            image(f, path.name, ((BASE, pattern(4096, 123)),))
            count = len(f.bus.calls)
            with self.assertRaisesRegex(ToolError, "artifact|changed|sha256"):
                execute(f, path)
            self.assertEqual(len(f.bus.calls), count)
            image(f, path.name, runs)
            self.assertEqual(plan(f, path)["status"], "plan_accepted")
            f.call("disconnect", dict(board_id=f.board))
            f.reconnect()
            with self.assertRaises(ToolError):
                execute(f, path)
            self.assertEqual(f.validate()["status"], "validation_passed")
            self.assertEqual(plan(f, path)["status"], "plan_accepted")
            f.call("disconnect", dict(board_id=f.board))
            key = next(k for k in f.factory.values if k == 0x1FFF7594)
            f.factory.values[key] ^= 1
            f.reconnect()
            result = f.validate()
            self.assertNotEqual(result["status"], "validation_passed", result)
            with self.assertRaises(ToolError):
                execute(f, path)
            self.assertEqual(bytes(f.bus.memory), before)
            self.assertEqual(f.record().data, policy)
            self.assertFalse(any(row[0] == "attempt" for row in f.bus.calls))

    def test_programming_readback_precedes_reset_and_does_not_establish_startup(self):
        with workflow(self) as f:
            admit(self, f)
            path, runs = image(f, "reset.elf")
            self.assertEqual(plan(f, path)["status"], "plan_accepted")
            f.bus.reset_fault = True
            result = execute(f, path)
            assert_image(self, f, runs)
            self.assertIn("5144 matching addressed bytes", result)
            self.assertIn("final reset state is unconfirmed", result)
            self.assertNotIn("target left running", result)
            instructions = f.flow.call("flash_bootloader-plan", NULL_PLAN)
            self.assertIn("reset_state_unconfirmed", instructions)
            self.assertNotIn("The target runs after flashing", instructions)


if __name__ == "__main__":
    unittest.main()
