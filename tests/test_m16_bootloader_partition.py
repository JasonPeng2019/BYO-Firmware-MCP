"""Public M16 implementation regressions; all physical I/O is controlled at the probe."""

from __future__ import annotations

import copy
import json
import unittest

from tests.b02_public_support import elf_fixture
from tests.m05_public_support import NULL_PLAN
from tests.m14_independent_support import connect, disclose, ordinary_full
from tests.m15_independent_support import (
    assert_closed,
    assert_committed,
    assert_read,
    corpus,
    lose,
    restart,
    restore_route,
    selected,
)
from tests.m16_public_support import admit, factory_registers
from tests.test_m05_lite_validation import advertised_validation


class PublicBootloaderPartitionTests(unittest.TestCase):
    def test_public_admission_creates_bounded_partition(self):
        with ordinary_full(self) as f, factory_registers(f) as factory:
            original = selected(f.root, f.board)
            response = admit(f, factory.request)
            self.assertEqual(response["status"], "safety_refresh_completed", response)
            current = selected(f.root, f.board)
            snapshot = current.document["map_snapshot"]
            self.assertEqual(snapshot["partitions"]["bootloader"], dict(start=0x80000, end=0x82000))
            self.assertEqual(
                current.document["profile_snapshot"], original.document["profile_snapshot"]
            )
            self.assertEqual(
                snapshot["deployment_policy"],
                original.document["map_snapshot"]["deployment_policy"],
            )
            self.assertTrue(response["observed"]["validation_required"])
            caps = f.flow.json("get_capabilities", dict(board_id=f.board))
            self.assertIsNone(caps["identity"]["capability"])
            self.assertEqual(advertised_validation(f.flow, f.board)["status"], "validation_passed")
            caps = f.flow.json("get_capabilities", dict(board_id=f.board))
            self.assertEqual(caps["identity"]["capability"], "exact", caps)
            self.assertIn("package suffix is not proven", caps["identity"]["coverage"])
            row = next(row for row in caps["capabilities"] if row["family"] == "flash-bootloader")
            self.assertTrue(row["safe"], row)
            artifact = elf_fixture(f.root / "bootloader.elf", base=0x80000)
            f.flow.call("flash_bootloader-plan", NULL_PLAN)
            plan = f.flow.plan(
                "flash_bootloader", f.board, dict(artifact=str(artifact)), permission="one-time"
            )
            self.assertEqual(plan["status"], "plan_accepted", plan)
            proof = snapshot["bootloader_policy"]["factory_identity"]
            self.assertEqual(proof["observations"][-2:], [0x62A73C51, 0x8279BE14])
            repeat = admit(f, copy.deepcopy(factory.request))
            self.assertEqual(repeat["status"], "safety_refresh_completed", repeat)
            self.assertEqual(selected(f.root, f.board).document["map_snapshot"], snapshot)
            self.assertFalse(any(row[0] in {"program", "erase"} for row in f.backend.calls))

    def test_invalid_bounds_evidence_and_identity_cannot_create_authority(self):
        with ordinary_full(self) as f, factory_registers(f) as factory:
            original = selected(f.root, f.board)
            wrong_source = f.root / "wrong-source.pdf"
            wrong_source.write_bytes(b"This is not the documented official PDF.")
            cases = []
            for changes in (
                dict(start=0x80001),
                dict(end=0x82001),
                dict(start=0xFFFFF, end=0x101000),
                dict(start=0, end=0x100000),
                dict(start=True),
                dict(end=0),
                dict(source_path="missing.pdf"),
                dict(source_path=str(wrong_source)),
                dict(source_pages=[]),
                dict(source_note=""),
            ):
                request = copy.deepcopy(factory.request)
                request.update(changes)
                cases.append(request)
            no_uid = copy.deepcopy(factory.request)
            no_uid["registers"] = no_uid["registers"][:2]
            cases.append(no_uid)
            for field, value in (
                ("expected", 0x62A73C51),
                ("read_only", False),
                ("address", 0xE000ED00),
                ("address", 0x20000000),
            ):
                request = copy.deepcopy(factory.request)
                request["registers"][2][field] = value
                cases.append(request)
            wrong_model = copy.deepcopy(factory.request)
            wrong_model["registers"][0]["expected"] = 0x52832
            cases.append(wrong_model)
            for request in cases:
                with self.subTest(request=request):
                    response = admit(f, request)
                    self.assertEqual(response["status"], "safety_refresh_blocked", response)
                    self.assertEqual(selected(f.root, f.board).data, original.data)
                    self.assertEqual(original.pointer.read_bytes(), original.pointer_bytes)
            for address, value in ((0x10000110, 512), (0x10000100, 0x52832)):
                old = factory.values[address]
                factory.values[address] = value
                response = admit(f, factory.request)
                self.assertEqual(response["status"], "safety_refresh_blocked", response)
                self.assertEqual(selected(f.root, f.board).data, original.data)
                factory.values[address] = old
            for blank in (0, 0xFFFFFFFF):
                prior = (factory.values[0x10000060], factory.values[0x10000064])
                factory.values[0x10000060] = factory.values[0x10000064] = blank
                response = admit(f, factory.request)
                self.assertEqual(response["status"], "safety_refresh_blocked", response)
                self.assertEqual(selected(f.root, f.board).data, original.data)
                factory.values[0x10000060], factory.values[0x10000064] = prior

    def test_reconnect_cannot_admit_without_fresh_validation(self):
        with ordinary_full(self) as f, factory_registers(f) as factory:
            original = selected(f.root, f.board)
            f.flow.call("disconnect", dict(board_id=f.board))
            connect(f)
            before = list(f.backend.calls)
            response = admit(f, factory.request)
            self.assertEqual(response["status"], "safety_refresh_blocked", response)
            self.assertEqual(f.backend.calls, before)
            self.assertEqual(selected(f.root, f.board).data, original.data)

    def test_application_allocation_refresh_and_erase_disclosure_preserve_bootloader(self):
        with corpus(self) as f, factory_registers(f) as factory:
            original = selected(f.root, f.board).document
            before_disclosure = disclose(f)
            overlap = copy.deepcopy(factory.request)
            overlap.update(start=0, end=0x1000)
            self.assertEqual(admit(f, overlap)["status"], "safety_refresh_blocked")
            self.assertEqual(admit(f, factory.request)["status"], "safety_refresh_completed")
            admitted = selected(f.root, f.board).document
            self.assertEqual(
                admitted["map_snapshot"]["deployment_policy"],
                original["map_snapshot"]["deployment_policy"],
            )
            self.assertEqual(
                admitted["map_snapshot"]["partitions"]["application"],
                original["map_snapshot"]["partitions"]["application"],
            )
            self.assertEqual(advertised_validation(f.flow, f.board)["status"], "validation_passed")
            bootloader = admitted["map_snapshot"]["bootloader_policy"]
            expanded = elf_fixture(f.root / "expanded.elf", base=0x2000)
            f.flow.call("flash_application-plan", NULL_PLAN)
            plan = f.flow.plan("flash_application", f.board, dict(artifact=str(expanded)))
            self.assertEqual(plan["status"], "plan_accepted", plan)
            result = f.flow.call(
                "flash_application", dict(board_id=f.board, artifact=str(expanded))
            )
            self.assertIn("matching addressed bytes", result)
            after = selected(f.root, f.board).document["map_snapshot"]
            self.assertEqual(after["bootloader_policy"], bootloader)
            self.assertEqual(after["partitions"]["application"], dict(start=0, end=0x3000))
            refresh = f.flow.json("board_safety_refresh", dict(board_id=f.board))
            self.assertEqual(refresh["status"], "safety_refresh_completed", refresh)
            self.assertEqual(selected(f.root, f.board).document["map_snapshot"], after)
            disclosure = disclose(f)
            affected = [
                row
                for row in disclosure["disclosure"]["affected_regions"]
                if row["kind"] == "bootloader_flash"
            ]
            self.assertEqual([(row["start"], row["end"]) for row in affected], [(0x80000, 0x82000)])
            self.assertNotEqual(
                disclosure["manual_grant"]["binding_digest"],
                before_disclosure["manual_grant"]["binding_digest"],
            )
            self.assertFalse(any(row[0] == "erase" for row in f.backend.calls))

    def test_partition_bearing_m15_recovery_preserves_whole_record_and_requires_validation(self):
        with corpus(self) as f, factory_registers(f) as factory:
            self.assertEqual(admit(f, factory.request)["status"], "safety_refresh_completed")
            self.assertEqual(advertised_validation(f.flow, f.board)["status"], "validation_passed")
            f.saved = selected(f.root, f.board)
            f.backup.write_bytes(f.saved.data)
            lose(f)
            with restart(f) as broken:
                assert_closed(self, broken)
                result = restore_route(self, broken, str(f.backup))
                self.assertEqual(result["status"], "policy_restore_completed", result)
                assert_committed(self, broken)
                self.assertIsNone(broken.server.gate_manager.snapshot(f.board))
            with restart(f) as fresh:
                assert_committed(self, fresh)
                self.assertIsNone(fresh.server.gate_manager.snapshot(f.board))
                fresh.flow.overview("Independent M14 generic erase")
                self.assertEqual(
                    advertised_validation(fresh.flow, f.board)["status"], "validation_passed"
                )
                assert_read(self, fresh)
                caps = fresh.flow.json("get_capabilities", dict(board_id=f.board))
                self.assertEqual(caps["identity"]["capability"], "exact", caps)
                assert_committed(self, fresh)

    def test_changed_unit_or_captured_source_cannot_revalidate_exact(self):
        with ordinary_full(self) as f, factory_registers(f) as factory:
            self.assertEqual(admit(f, factory.request)["status"], "safety_refresh_completed")
            snapshot = selected(f.root, f.board).document["map_snapshot"]
            factory.values[0x10000060] ^= 1
            reply = advertised_validation(f.flow, f.board)
            self.assertNotEqual(reply["status"], "validation_passed", reply)
            self.assertEqual(reply["code"], "validation/factory-proof-unavailable")
            self.assertIsNone(f.server.gate_manager.snapshot(f.board))
            factory.values[0x10000060] ^= 1
            self.assertEqual(advertised_validation(f.flow, f.board)["status"], "validation_passed")
            source = (
                f.root / snapshot["bootloader_policy"]["factory_identity"]["source"]["reference"]
            )
            source.write_bytes(b"%PDF-1.4\n% Wrong source bytes\n")
            reply = advertised_validation(f.flow, f.board)
            self.assertNotEqual(reply["status"], "validation_passed", reply)
            caps = f.flow.json("get_capabilities", dict(board_id=f.board))
            self.assertIsNone(caps["identity"]["capability"], caps)
            self.assertIsNone(f.server.gate_manager.snapshot(f.board))

    def test_failed_repeat_closes_exact_readiness_without_replacing_ownership(self):
        with ordinary_full(self) as f, factory_registers(f) as factory:
            self.assertEqual(admit(f, factory.request)["status"], "safety_refresh_completed")
            self.assertEqual(advertised_validation(f.flow, f.board)["status"], "validation_passed")
            original = selected(f.root, f.board)
            factory.values[0x10000064] ^= 1
            reply = admit(f, factory.request)
            self.assertEqual(reply["status"], "safety_refresh_blocked", reply)
            self.assertIsNone(f.server.gate_manager.snapshot(f.board))
            self.assertEqual(selected(f.root, f.board).data, original.data)

    def test_mutable_projection_does_not_admit_partition(self):
        with ordinary_full(self) as f:
            original = selected(f.root, f.board).document["map_snapshot"]
            projected = copy.deepcopy(original)
            projected["partitions"]["bootloader"] = dict(start=0x80000, end=0x82000)
            path = f.root / ".firm/safety" / f.board / "memory_map.yaml"
            path.write_text(json.dumps(projected), encoding="utf-8")
            f.flow.json("load_setup_tool", dict(board_id=f.board, tool_name="board_safety_refresh"))
            reply = f.flow.json("board_safety_refresh", dict(board_id=f.board))
            self.assertEqual(reply["status"], "safety_refresh_completed", reply)
            self.assertEqual(selected(f.root, f.board).document["map_snapshot"], original)


if __name__ == "__main__":
    unittest.main()
