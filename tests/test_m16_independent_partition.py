"""M16-T behavioral oracles; public authority, synthetic hardware only."""

from __future__ import annotations

import copy
import json
import unittest

from tests.b02_public_support import elf_fixture
from tests.m05_public_support import NULL_PLAN
from tests.m14_independent_support import disclose
from tests.m16_independent_support import NAME, RAM, RAM_VALUE, full_fixture


class IndependentPartitionTests(unittest.TestCase):
    def unchanged(self, f, saved):
        current = f.record()
        self.assertEqual(current.pointer_bytes, saved.pointer_bytes)
        self.assertEqual(current.data, saved.data)
        self.assertEqual(saved.path.read_bytes(), saved.data)

    def not_exact(self, f):
        self.assertIsNone(f.caps()["identity"]["capability"])
        self.assertFalse(f.flow.json("get_setup_status", dict(board_id=f.board))["ready_for_code"])

    def read(self, f):
        f.flow.call("read_memory_address-plan", NULL_PLAN)
        plan = f.flow.plan("read_memory_address", f.board, dict(address=RAM, width=32, length=None))
        self.assertEqual(plan["status"], "plan_accepted", plan)
        reply = f.flow.execute(plan["stable_client_fallback"])
        self.assertEqual(reply["status"], "batch_completed", reply)
        self.assertEqual(int(reply["completed"][0]["result"].splitlines()[0], 16), RAM_VALUE)

    def test_builtin_and_pack_admission_repeat_need_fresh_same_unit_validation(self):
        for family in ("builtin", "pack"):
            with self.subTest(family=family), full_fixture(self, family) as f:
                initial = f.record()
                self.assertEqual(f.caps()["identity"]["capability"], "compatible")
                self.assertIsNone(initial.document["map_snapshot"]["partitions"]["bootloader"])
                result = f.admit()
                self.assertEqual(result["status"], "safety_refresh_completed", result)
                admitted = f.record()
                snapshot = admitted.document["map_snapshot"]
                expected = {key: f.factory.request[key] for key in ("start", "end")}
                self.assertEqual(snapshot["partitions"]["bootloader"], expected)
                self.assertEqual(snapshot["identity"], initial.document["map_snapshot"]["identity"])
                self.assertEqual(
                    snapshot["authority_source"],
                    initial.document["map_snapshot"]["authority_source"],
                )
                self.assertEqual(
                    admitted.document["profile_snapshot"], initial.document["profile_snapshot"]
                )
                self.assertEqual(admitted.document["evidence"], initial.document["evidence"])
                proof = snapshot["bootloader_policy"]["factory_identity"]
                self.assertEqual(proof["observations"], list(f.factory.values.values()))
                self.assertEqual(proof["research"]["registers"], f.factory.request["registers"])
                captured = f.root / proof["source"]["reference"]
                self.assertEqual(captured.read_bytes(), f.factory.source.read_bytes())
                self.assertTrue(result["observed"]["validation_required"])
                self.not_exact(f)
                self.assertEqual(f.validate()["status"], "validation_passed")
                caps = f.caps()
                self.assertEqual(caps["identity"]["capability"], "exact", caps)
                self.assertIn("package suffix unproven", caps["identity"]["coverage"])
                self.read(f)
                repeated = f.admit()
                self.assertEqual(repeated["status"], "safety_refresh_completed", repeated)
                self.assertEqual(f.record().data, admitted.data)
                self.not_exact(f)
                self.assertEqual(f.validate()["status"], "validation_passed")
                self.assertEqual(f.caps()["identity"]["capability"], "exact")
                self.assertFalse(any(c[0] in {"program", "erase"} for c in f.backend.calls))

    def test_whole_authority_refresh_allocation_restart_and_erase_binding(self):
        for family in ("builtin", "pack"):
            with self.subTest(family=family), full_fixture(self, family) as f:
                f.allocate()
                before = f.record()
                old_binding = disclose(f)["manual_grant"]["binding_digest"]
                self.assertEqual(f.admit()["status"], "safety_refresh_completed")
                admitted = f.record()
                previous_map = before.document["map_snapshot"]
                current_map = admitted.document["map_snapshot"]
                for key in ("deployment_policy",):
                    self.assertEqual(current_map[key], previous_map[key])
                self.assertEqual(
                    current_map["partitions"]["application"],
                    previous_map["partitions"]["application"],
                )
                self.assertEqual(f.validate()["status"], "validation_passed")
                self.assertEqual(f.refresh()["status"], "safety_refresh_completed")
                self.assertEqual(f.record().data, admitted.data)
                f.allocate(4 * f.factory.sector)
                expanded = f.record()
                expanded_map = expanded.document["map_snapshot"]
                self.assertEqual(
                    expanded_map["bootloader_policy"], current_map["bootloader_policy"]
                )
                self.assertEqual(
                    expanded_map["partitions"]["bootloader"],
                    current_map["partitions"]["bootloader"],
                )
                self.assertEqual(
                    expanded_map["partitions"]["application"],
                    dict(start=f.factory.base, end=f.factory.base + 5 * f.factory.sector),
                )
                for key in set(expanded.document) - {"map_snapshot", "policy_digest"}:
                    self.assertEqual(expanded.document[key], admitted.document[key], key)
                self.assertEqual(f.refresh()["status"], "safety_refresh_completed")
                self.assertEqual(f.record().data, expanded.data)
                expanded = f.record()  # Refresh may publish a new pointer to identical bytes.
                disclosure = disclose(f)
                affected = [
                    r
                    for r in disclosure["disclosure"]["affected_regions"]
                    if r["kind"] == "bootloader_flash"
                ]
                self.assertEqual(
                    [(r["start"], r["end"]) for r in affected],
                    [(f.factory.request["start"], f.factory.request["end"])],
                )
                self.assertNotEqual(disclosure["manual_grant"]["binding_digest"], old_binding)
                run = f.server.server_run.run_id
                f.start()
                self.assertNotEqual(f.server.server_run.run_id, run)
                self.assertEqual(f.record().data, expanded.data)
                self.assertEqual(f.record().pointer_bytes, expanded.pointer_bytes)
                self.not_exact(f)
                f.reconnect()
                self.assertEqual(f.validate()["status"], "validation_passed")
                self.assertEqual(f.caps()["identity"]["capability"], "exact")
                self.read(f)
                self.unchanged(f, expanded)
                self.assertFalse(any(c[0] == "erase" for c in f.backend.calls))

    def test_bad_bounds_missing_evidence_and_application_overlap_preserve_all_bytes(self):
        for family in ("builtin", "pack"):
            with self.subTest(family=family), full_fixture(self, family) as f:
                f.allocate()
                saved, target = f.record(), dict(f.backend.programmed)
                request = f.factory.request
                invalid = [
                    dict(start=request["start"] + 1),
                    dict(end=request["end"] - 1),
                    dict(start=f.factory.base, end=f.factory.base + 0x100000),
                    dict(start=f.factory.base + 0x100000, end=f.factory.base + 0x101000),
                    dict(start=f.factory.base, end=f.factory.base + f.factory.sector),
                    dict(start=0x20000000, end=0x20001000),
                    dict(start=True),
                    dict(end=request["start"]),
                    dict(source_path="absent.pdf"),
                    dict(source_pages=[]),
                    dict(source_note=""),
                    dict(identity_coverage=""),
                    dict(registers=request["registers"][:2]),
                ]
                for change in invalid:
                    with self.subTest(change=change):
                        bad = copy.deepcopy(request)
                        bad.update(change)
                        result = f.admit(bad)
                        self.assertEqual(result["status"], "safety_refresh_blocked", result)
                        self.unchanged(f, saved)
                        self.assertEqual(f.backend.programmed, target)
                self.assertEqual(f.admit()["status"], "safety_refresh_completed")

    def test_candidate_identity_values_and_mutable_addresses_do_not_create_authority(self):
        with full_fixture(self) as f:
            f.allocate()
            saved = f.record()
            changes = [
                (0, "expected", 0x52832),
                (1, "expected", 1024),
                (2, "expected", 0x137C9A21),
                (2, "mask", 0xFFFF),
                (2, "read_only", False),
                (2, "factory", False),
                (2, "address", 0xE000ED00),
                (2, "address", RAM),
                (2, "address", f.factory.base),
                (2, "source_pages", []),
                (2, "address", f.factory.request["registers"][3]["address"]),
            ]
            for index, key, value in changes:
                with self.subTest(index=index, key=key, value=value):
                    bad = copy.deepcopy(f.factory.request)
                    bad["registers"][index][key] = value
                    reply = f.admit(bad)
                    self.assertEqual(reply["status"], "safety_refresh_blocked", reply)
                    self.unchanged(f, saved)
            original = dict(f.factory.values)
            for changes in (
                {next(iter(original)): 0x52832},
                {f.factory.request["registers"][1]["address"]: 512},
                {r["address"]: 0 for r in f.factory.request["registers"] if r["role"] == "unique"},
                {
                    r["address"]: 0xFFFFFFFF
                    for r in f.factory.request["registers"]
                    if r["role"] == "unique"
                },
            ):
                with self.subTest(observations=changes):
                    f.factory.values.update(changes)
                    reply = f.admit()
                    self.assertEqual(reply["status"], "safety_refresh_blocked", reply)
                    self.unchanged(f, saved)
                    f.factory.values.update(original)

    def test_disconnected_or_unvalidated_session_cannot_admit(self):
        with full_fixture(self) as f:
            f.allocate()
            saved = f.record()
            f.flow.call("disconnect", dict(board_id=f.board))
            f.reconnect()
            calls = list(f.backend.calls)
            result = f.admit()
            self.assertEqual(result["status"], "safety_refresh_blocked", result)
            self.assertEqual(f.backend.calls, calls)
            self.unchanged(f, saved)
            self.assertEqual(f.validate()["status"], "validation_passed")
            self.assertEqual(f.admit()["status"], "safety_refresh_completed")

    def test_unit_that_changes_between_server_capture_and_replay_cannot_be_admitted(self):
        with full_fixture(self) as f:
            f.allocate()
            saved = f.record()
            uid = f.factory.request["registers"][-1]["address"]
            previous = f.factory.values[uid]

            def swap_after_read(address):
                if address == uid:
                    f.factory.values[uid] ^= 1
                    f.backend.on_factory_read = None

            f.backend.on_factory_read = swap_after_read
            reply = f.admit()
            self.assertEqual(reply["status"], "safety_refresh_blocked", reply)
            self.unchanged(f, saved)
            f.factory.values[uid] = previous
            self.assertEqual(f.admit()["status"], "safety_refresh_completed")

    def test_missing_or_changed_official_pack_support_and_driver_bytes_close_readiness(self):
        with full_fixture(self, "pack") as f:
            f.allocate()
            self.assertEqual(f.admit()["status"], "safety_refresh_completed")
            self.assertEqual(f.validate()["status"], "validation_passed")
            saved = f.record()
            support = saved.document["map_snapshot"]["authority_source"]
            self.assertEqual(support["kind"], "resolved_pack")
            pack = f.root / ".firm/packs/files" / support["pack_filename"]
            payload = pack.read_bytes()
            import io
            import zipfile

            # Change actual programming algorithm bytes in the fixture's copy,
            # leaving the committed source/support/digest completely untouched.
            modified = io.BytesIO()
            with (
                zipfile.ZipFile(io.BytesIO(payload)) as original,
                zipfile.ZipFile(modified, "w") as changed,
            ):
                for entry in original.infolist():
                    data = original.read(entry)
                    if entry.filename == "CMSIS/Flash/STM32L4xx_1024.FLM":
                        data = data[:-1] + bytes([data[-1] ^ 1])
                    changed.writestr(entry, data)
            self.assertNotEqual(modified.getvalue(), payload)
            for replacement in (None, modified.getvalue()):
                with self.subTest(missing=replacement is None):
                    if replacement is None:
                        pack.unlink()
                    else:
                        pack.write_bytes(replacement)
                    reply = f.validate()
                    self.assertNotEqual(reply["status"], "validation_passed", reply)
                    self.not_exact(f)
                    self.unchanged(f, saved)
                    self.assertEqual(f.refresh()["status"], "safety_refresh_blocked")
                    self.unchanged(f, saved)
                    pack.write_bytes(payload)
                    self.assertEqual(f.validate()["status"], "validation_passed")
                    self.assertEqual(f.caps()["identity"]["capability"], "exact")

    def test_unit_model_size_and_captured_source_replay_revoke_exact_readiness(self):
        for family in ("builtin", "pack"):
            with self.subTest(family=family), full_fixture(self, family) as f:
                self.assertEqual(f.admit()["status"], "safety_refresh_completed")
                saved = f.record()
                for register in f.factory.request["registers"]:
                    address = register["address"]
                    old = f.factory.values[address]
                    f.factory.values[address] ^= 1
                    reply = f.validate()
                    self.assertNotEqual(reply["status"], "validation_passed", reply)
                    self.not_exact(f)
                    self.unchanged(f, saved)
                    f.factory.values[address] = old
                    self.assertEqual(f.validate()["status"], "validation_passed")
                reference = saved.document["map_snapshot"]["bootloader_policy"]["factory_identity"][
                    "source"
                ]["reference"]
                captured = f.root / reference
                payload = captured.read_bytes()
                for wrong in (None, payload + b"\nchanged source"):
                    with self.subTest(source=wrong is not None):
                        if wrong is None:
                            captured.unlink()
                        else:
                            captured.write_bytes(wrong)
                        reply = f.validate()
                        self.assertNotEqual(reply["status"], "validation_passed", reply)
                        self.not_exact(f)
                        self.unchanged(f, saved)
                        captured.write_bytes(payload)
                        self.assertEqual(f.validate()["status"], "validation_passed")

    def test_existing_partition_cannot_be_replaced_or_resized_or_rebound(self):
        with full_fixture(self) as f:
            f.allocate()
            self.assertEqual(f.admit()["status"], "safety_refresh_completed")
            self.assertEqual(f.validate()["status"], "validation_passed")
            saved = f.record()
            before = dict(f.backend.programmed)
            overlap = elf_fixture(f.root / "overlapping-app.elf", base=f.factory.request["start"])
            f.flow.call("flash_application-plan", NULL_PLAN)
            accepted = f.flow.plan("flash_application", f.board, dict(artifact=str(overlap)))
            self.assertEqual(accepted["status"], "plan_accepted", accepted)
            denied = f.flow.execute(accepted["stable_client_fallback"])
            self.assertEqual(denied["status"], "batch_failed", denied)
            self.unchanged(f, saved)
            self.assertEqual(f.backend.programmed, before)
            for changes in (
                dict(end=f.factory.request["end"] + f.factory.sector),
                dict(start=f.factory.request["start"] + f.factory.sector),
                dict(identity_coverage="replacement coverage"),
            ):
                with self.subTest(changes=changes):
                    request = copy.deepcopy(f.factory.request)
                    request.update(changes)
                    self.assertEqual(f.admit(request)["status"], "safety_refresh_blocked")
                    self.unchanged(f, saved)
                    self.assertEqual(f.validate()["status"], "validation_passed")
            uid = f.factory.request["registers"][-1]["address"]
            f.factory.values[uid] ^= 1
            reply = f.admit()
            self.assertEqual(reply["status"], "safety_refresh_blocked", reply)
            self.not_exact(f)
            self.unchanged(f, saved)

    def test_projection_only_partition_and_profile_flags_never_become_authority(self):
        with full_fixture(self) as f:
            f.allocate()
            saved = f.record()
            projected = copy.deepcopy(saved.document["map_snapshot"])
            projected["partitions"]["bootloader"] = {
                key: f.factory.request[key] for key in ("start", "end")
            }
            projection = f.root / ".firm/safety" / f.board / "memory_map.yaml"
            projection.write_text(json.dumps(projected), encoding="utf-8")
            profile = next((f.root / ".firm/boards").glob(f.board + ".*"))
            altered = copy.deepcopy(saved.document["profile_snapshot"])
            altered["exact_identity"] = True
            altered["factory_uid"] = "caller-asserted-unit"
            profile.write_text(json.dumps(altered), encoding="utf-8")
            self.assertEqual(f.refresh()["status"], "safety_refresh_completed")
            self.assertEqual(f.record().data, saved.data)
            f.start()
            self.not_exact(f)
            calls = list(f.backend.calls)
            overview = f.flow.overview(NAME)
            self.assertEqual(overview["status"], "setup_overview_blocked", overview)
            self.assertEqual(f.backend.calls, calls)
            # The unknown projection flags are a schema error, not live proof.
            # Repair projections using the existing exact-backup public route.
            backup = f.root / "projection-repair.json"
            backup.write_bytes(saved.data)
            f.flow.json("load_setup_tool", dict(board_id=f.board, tool_name="board_policy_restore"))
            restored = f.flow.json(
                "board_policy_restore", dict(board_id=f.board, generation_backup_path=str(backup))
            )
            self.assertEqual(restored["status"], "policy_restore_completed", restored)
            f.reconnect()
            self.assertEqual(f.validate()["status"], "validation_passed")
            self.assertEqual(f.caps()["identity"]["capability"], "compatible")
            self.assertIsNone(f.record().document["map_snapshot"]["partitions"]["bootloader"])

    def test_healthy_generic_setup_and_read_control(self):
        with full_fixture(self) as f:
            initial = f.record()
            self.read(f)
            self.assertEqual(f.refresh()["status"], "safety_refresh_completed")
            self.assertEqual(f.record().data, initial.data)
            self.assertEqual(f.caps()["identity"]["capability"], "compatible")


if __name__ == "__main__":
    unittest.main()
