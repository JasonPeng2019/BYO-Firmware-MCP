"""M02 independent public raw/guarded regressions with real production flashing.

These tests prove deterministic host behavior, not hardware programming or a
completed public setup. See m02_physical_flash for the separate opt-in workflow.
"""

from __future__ import annotations

import json
import unittest

from pyocd.core.memory_map import MemoryMap
from pyocd.coresight.coresight_target import CoreSightTarget
from pyocd_debug_mcp.safety.map_build import GenericSafetyMapDocument

from tests.m02_independent_support import (
    BASE,
    BOARD,
    SIZE,
    fixture_runs,
    pattern,
    plan,
    public_fixture,
    separate_loads,
    write_artifact,
)


class IndependentRawFlash(unittest.TestCase):
    def raw(self, f, artifact, **kwargs):
        return json.loads(
            f.call("flash_raw", {"board_id": BOARD, "artifact": str(artifact), **kwargs})
        )

    def assert_untouched(self, f, runs):
        expected = bytearray(f.target.initial)
        for address, data in runs:
            expected[address - BASE : address - BASE + len(data)] = data
        self.assertEqual(
            f.target.memory, expected, "all controlled holes and outside bytes preserved"
        )

    def test_programmed_bytes_control_and_healthy_followup(self):
        """Unaffected positive control for each deliberately defective mutant."""
        with public_fixture(self) as f:
            runs = fixture_runs()
            artifact = write_artifact(f.root / "control.elf", runs)
            reply = self.raw(f, artifact)
            self.assertEqual(reply["status"], "ok", reply)
            self.assertEqual(reply["result"]["programming"]["verified_bytes"], 9080)
            self.assert_untouched(f, runs)
            self.assertIn("running", f.call("get_state", {"board_id": BOARD}).lower())

    def test_supported_elf_axf_hex_and_nonzero_bin_use_independent_bytes_and_bounded_reads(self):
        for suffix in (".elf", ".axf", ".hex", ".bin"):
            with self.subTest(format=suffix), public_fixture(self) as f:
                runs = ((BASE + 0x2800, pattern(9001, 81)),) if suffix == ".bin" else fixture_runs()
                artifact = write_artifact(f.root / ("supported" + suffix), runs)
                self.assertEqual(separate_loads(artifact, runs[0][0]), runs)
                self.assertTrue(
                    any(bytes(f.target.memory[s - BASE : s - BASE + len(d)]) != d for s, d in runs)
                )
                kwargs = {"base_address": hex(runs[0][0])} if suffix == ".bin" else {}
                reply = self.raw(f, artifact, **kwargs)
                self.assertEqual(reply["status"], "ok", reply)
                self.assertEqual(
                    reply["result"]["programming"],
                    {
                        "status": "verified",
                        "verified_bytes": sum(len(d) for _, d in runs),
                        "ranges": [
                            {"start": f"0x{s:08X}", "end": f"0x{s + len(d):08X}"} for s, d in runs
                        ],
                    },
                )
                self.assertEqual(reply["result"]["target_state"], "running")
                expected_reads = [
                    (s + i, min(4096, len(d) - i)) for s, d in runs for i in range(0, len(d), 4096)
                ]
                self.assertEqual(f.target.reads, expected_reads)
                self.assertEqual(f.target.calls[0], ("reset_and_halt",))
                self.assertEqual(f.target.calls[-1], ("reset",))
                self.assert_untouched(f, runs)
                program = next(row for row in f.target.calls if row[0] == "program")
                self.assertEqual(
                    program[2], {"base_address": runs[0][0]} if suffix == ".bin" else {}
                )

    def test_normal_return_noop_on_different_bytes_cannot_succeed(self):
        with public_fixture(self) as f:
            runs = fixture_runs()
            artifact = write_artifact(f.root / "noop.elf", runs)
            self.assertNotEqual(bytes(f.target.memory[: len(runs[0][1])]), runs[0][1])
            f.target.program_mode = "noop"
            reply = self.raw(f, artifact)
            self.assertEqual(reply["status"], "error", reply)
            self.assertEqual(reply["code"], "flash/verification-failed", reply)
            self.assertIn("does not match", reply["message"])
            self.assertEqual(bytes(f.target.memory), f.target.initial)
            self.assertEqual(sum(c[0] == "program" for c in f.target.calls), 1)
            self.assertNotIn(("reset",), f.target.calls)

    def test_already_identical_noop_is_a_legitimate_idempotency_control(self):
        with public_fixture(self) as f:
            runs = fixture_runs()
            artifact = write_artifact(f.root / "already.elf", runs)
            for address, data in runs:
                f.target.memory[address - BASE : address - BASE + len(data)] = data
            before = bytes(f.target.memory)
            f.target.program_mode = "noop"
            reply = self.raw(f, artifact)
            self.assertEqual(reply["status"], "ok", reply)
            self.assertEqual(reply["result"]["programming"]["verified_bytes"], 9080)
            self.assertEqual(bytes(f.target.memory), before)
            self.assertEqual(sum(n for _, n in f.target.reads), 9080)

    def test_last_sparse_byte_corruption_short_and_failed_reads_never_prove_success(self):
        for fault in ("corrupt", "short", "read-error"):
            with self.subTest(fault=fault), public_fixture(self) as f:
                artifact = write_artifact(f.root / "fault.elf", fixture_runs())
                if fault == "corrupt":
                    f.target.program_mode = fault
                else:
                    f.target.read_fault = fault
                reply = self.raw(f, artifact)
                self.assertEqual(reply["status"], "error", reply)
                self.assertEqual(reply["code"], "flash/verification-failed", reply)
                self.assertIn(
                    {
                        "corrupt": "0x0800404E",
                        "short": "returned 78 of 79",
                        "read-error": "could not be read back",
                    }[fault],
                    reply["message"],
                )
                self.assertNotIn(("reset",), f.target.calls)
                self.assertEqual(f.target.reads[-1], (BASE + 0x4000, 79))
                self.assertEqual(f.target.state, "HALTED")

    def test_every_region_is_checked_before_any_reset_program_or_write(self):
        for fault in (
            "generic",
            "second-missing",
            "second-invalid",
            "second-sequences",
            "cross-region",
        ):
            with self.subTest(fault=fault), public_fixture(self) as f:
                first = f.target.region(BASE, 0x4000)
                second = f.target.region(
                    BASE + 0x4000,
                    SIZE - 0x4000,
                    valid=fault != "second-invalid",
                    sequences=fault != "second-sequences",
                )
                if fault == "generic":
                    f.target.memory_map = CoreSightTarget._create_default_cortex_m_memory_map(
                        object()
                    )
                    f.target.part_number = "cortex_m"
                else:
                    if fault in {"second-missing", "cross-region"}:
                        second.flash = None
                    f.target.memory_map = MemoryMap(first, second)
                runs = (
                    ((BASE + 0x3FF0, pattern(64, 14)),)
                    if fault == "cross-region"
                    else fixture_runs()
                )
                artifact = write_artifact(f.root / "unsupported.elf", runs)
                reply = self.raw(f, artifact)
                self.assertEqual(reply["status"], "refused", reply)
                self.assertEqual(reply["code"], "flash/unsupported-target", reply)
                self.assertIn("nothing was reset, erased or programmed", reply["message"])
                if fault != "generic":
                    self.assertIn("0x08004000", reply["message"])
                self.assertEqual(f.target.calls, [])
                self.assertEqual(bytes(f.target.memory), f.target.initial)
                self.assertEqual(f.target.state, "HALTED")

    def test_missing_invalid_or_overflowing_bin_base_refuses_before_effects(self):
        for base in (None, -1, 1 << 32, True, "nonsense", "0xFFFFFFFE"):
            with self.subTest(base=base), public_fixture(self) as f:
                artifact = write_artifact(f.root / "base.bin", ((BASE, b"\x10\x20\x30\x40"),))
                reply = self.raw(f, artifact, **({} if base is None else {"base_address": base}))
                self.assertIn(reply["status"], {"error", "refused"}, reply)
                self.assertNotIn("programming", reply)
                if base is None:
                    self.assertIn("explicit base_address", reply["message"])
                self.assertEqual(f.target.calls, [])
                self.assertEqual(bytes(f.target.memory), f.target.initial)

    def test_final_reset_failure_after_complete_equality_is_verified_execution_unconfirmed(self):
        with public_fixture(self) as f:
            artifact = write_artifact(f.root / "reset.elf", fixture_runs())
            f.target.reset_fault = True
            reply = self.raw(f, artifact)
            self.assertEqual(reply["status"], "ok", reply)
            self.assertEqual(reply["result"]["programming"]["status"], "verified")
            self.assertEqual(reply["result"]["target_state"], "reset_state_unconfirmed")
            self.assertEqual(f.target.calls[-2], ("read", BASE + 0x4000, 79))
            self.assertEqual(f.target.calls[-1], ("reset",))
            self.assert_untouched(f, fixture_runs())


class IndependentGuardedFlash(unittest.TestCase):
    def state(self, f):
        return GenericSafetyMapDocument.from_document(
            f.server._capability_policies.resolve(BOARD).map_snapshot
        )

    def artifact(self, f):
        return write_artifact(f.root / "guarded.elf", fixture_runs())

    def event_rows(self, f):
        return [
            json.loads(row)
            for path in f.root.glob(".firm/runs/*/logs/events.jsonl")
            for row in path.read_text(encoding="utf-8").splitlines()
        ]

    def test_unplanned_guarded_call_cannot_reach_production_programmer(self):
        with public_fixture(self, guarded=True) as f:
            artifact = self.artifact(f)
            before = self.state(f).canonical_digest
            with self.assertRaises(Exception):
                f.call("flash_application", {"board_id": BOARD, "artifact": str(artifact)})
            self.assertEqual(f.target.calls, [])
            self.assertEqual(self.state(f).canonical_digest, before)
            self.assertNotIn(BOARD, f.server._current_symbol_artifacts)

    def test_supported_public_guarded_flash_commits_real_bounded_allocation_and_symbol_binding(
        self,
    ):
        with public_fixture(self, guarded=True) as f:
            artifact = self.artifact(f)
            self.assertEqual(self.state(f).deployment_policy, {"kind": "none"})
            self.assertEqual(plan(f, "flash_application", artifact)["status"], "plan_accepted")
            f.target.before_program = lambda: self.assertIsNotNone(
                self.state(f).partitions.application
            )
            reply = f.call("flash_application", {"board_id": BOARD, "artifact": str(artifact)})
            self.assertIn("read back 9080 matching addressed bytes", reply)
            self.assertIn("target left running", reply)
            document = self.state(f)
            self.assertEqual(document.partitions.application.start, BASE)
            self.assertEqual(document.partitions.application.end, BASE + 0x4800)
            self.assertEqual(f.server._current_symbol_artifacts[BOARD][0], artifact)
            self.assertNotIn(BOARD, f.server._pending_generic_allocations)
            for address, data in fixture_runs():
                self.assertEqual(
                    bytes(f.target.memory[address - BASE : address - BASE + len(data)]), data
                )
            self.assertTrue(
                any(
                    "flash_application" in str(row) and "success" in str(row)
                    for row in self.event_rows(f)
                )
            )

    def test_guarded_unsupported_second_region_clears_pending_without_allocation(self):
        with public_fixture(self, guarded=True) as f:
            artifact = self.artifact(f)
            before = self.state(f).canonical_digest
            self.assertEqual(plan(f, "flash_application", artifact)["status"], "plan_accepted")
            second = f.target.region(BASE + 0x4000, SIZE - 0x4000, valid=False)
            f.target.memory_map = MemoryMap(f.target.region(BASE, 0x4000), second)
            reply = f.call("flash_application", {"board_id": BOARD, "artifact": str(artifact)})
            self.assertIn("flash/unsupported-target", reply)
            self.assertEqual(self.state(f).canonical_digest, before)
            self.assertEqual(self.state(f).deployment_policy, {"kind": "none"})
            self.assertEqual(f.target.calls, [])
            self.assertNotIn(BOARD, f.server._pending_generic_allocations)
            self.assertNotIn(BOARD, f.server._current_symbol_artifacts)

    def test_actual_guarded_backend_exception_or_noop_records_failure_without_successful_deployment(
        self,
    ):
        for mode in ("raise", "noop"):
            with self.subTest(mode=mode), public_fixture(self, guarded=True) as f:
                artifact = self.artifact(f)
                self.assertEqual(plan(f, "flash_application", artifact)["status"], "plan_accepted")
                f.target.program_mode = mode
                f.target.before_program = lambda: self.assertIsNotNone(
                    self.state(f).partitions.application
                )
                with self.assertRaisesRegex(
                    Exception, "controlled programmer exception|does not match"
                ):
                    f.call("flash_application", {"board_id": BOARD, "artifact": str(artifact)})
                self.assertEqual(bytes(f.target.memory), f.target.initial)
                # Allocation is durable ownership of the potential mutation scope;
                # it must not be mistaken for a successful deployment/event/binding.
                self.assertIsNotNone(self.state(f).partitions.application)
                self.assertEqual(
                    self.state(f).deployment_policy["kind"], "artifact_application_allocation"
                )
                self.assertNotIn(BOARD, f.server._pending_generic_allocations)
                self.assertNotIn(BOARD, f.server._current_symbol_artifacts)
                rows = [r for r in self.event_rows(f) if r.get("tool_name") == "flash_application"]
                self.assertTrue(rows, self.event_rows(f))
                self.assertTrue(any(r.get("outcome_kind") == "failed" for r in rows), rows)
                self.assertFalse(any(r.get("outcome_kind") == "success" for r in rows), rows)
                self.assertNotIn(("reset",), f.target.calls)

    def test_guarded_bin_and_partition_or_manual_authority_stay_refused(self):
        with public_fixture(self, guarded=True) as f:
            artifact = write_artifact(f.root / "no-authority.bin", ((BASE, pattern(64, 19)),))
            with self.assertRaisesRegex(Exception, "tier/wrong-route"):
                f.call(
                    "flash_raw",
                    {"board_id": BOARD, "artifact": str(artifact), "base_address": BASE},
                )
            with self.assertRaisesRegex(Exception, r"must use one of: \.elf, \.axf, \.hex"):
                plan(f, "flash_application", artifact)
            self.assertEqual(self.state(f).deployment_policy, {"kind": "none"})
            outside = write_artifact(
                f.root / "outside.elf", ((BASE + 0x100000, fixture_runs()[0][1]),)
            )
            plan(f, "flash_application", outside)
            with self.assertRaises(Exception):
                f.call("flash_application", {"board_id": BOARD, "artifact": str(outside)})
            boot = self.artifact(f)
            with self.assertRaisesRegex(
                Exception, "requires one-time or full-session user approval"
            ):
                plan(f, "flash_bootloader", boot)
            with self.assertRaises(Exception):
                f.call("flash_bootloader", {"board_id": BOARD, "artifact": str(boot)})
            self.assertEqual(f.target.calls, [])
            self.assertEqual(bytes(f.target.memory), f.target.initial)

    def test_guarded_artifact_drift_is_refused_before_allocation_or_programming(self):
        with public_fixture(self, guarded=True) as f:
            artifact = self.artifact(f)
            self.assertEqual(plan(f, "flash_application", artifact)["status"], "plan_accepted")
            source = bytearray(artifact.read_bytes())
            source[-1] ^= 0x40
            artifact.write_bytes(source)
            with self.assertRaisesRegex(Exception, "changed|replaced|artifact"):
                f.call("flash_application", {"board_id": BOARD, "artifact": str(artifact)})
            self.assertEqual(f.target.calls, [])
            self.assertEqual(self.state(f).deployment_policy, {"kind": "none"})
            self.assertNotIn(BOARD, f.server._current_symbol_artifacts)

    def test_guarded_matching_bytes_and_final_reset_failure_keep_execution_unconfirmed(self):
        with public_fixture(self, guarded=True) as f:
            artifact = self.artifact(f)
            self.assertEqual(plan(f, "flash_application", artifact)["status"], "plan_accepted")
            f.target.reset_fault = True
            reply = f.call("flash_application", {"board_id": BOARD, "artifact": str(artifact)})
            self.assertIn("read back 9080 matching addressed bytes", reply)
            self.assertIn("final reset state is unconfirmed", reply)
            self.assertNotIn("target left running", reply)
            for address, data in fixture_runs():
                self.assertEqual(
                    bytes(f.target.memory[address - BASE : address - BASE + len(data)]), data
                )
            events = [r for r in self.event_rows(f) if r.get("tool_name") == "flash_application"]
            self.assertEqual(events[-1]["details"]["programming"]["status"], "verified")
            self.assertEqual(events[-1]["details"]["target_state"], "reset_state_unconfirmed")


if __name__ == "__main__":
    unittest.main()
