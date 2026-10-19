"""Partition-bearing M15 regressions plus independent whole-authority oracles."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from tests import test_m15_independent_recovery as m15
from tests.m15_independent_support import (
    assert_closed,
    assert_committed,
    lose,
    restart,
    restore_route,
)
from tests.m16_independent_support import full_fixture, partitioned_recovery_corpus


class PartitionRecoveryTests(unittest.TestCase):
    def test_m15_loss_restart_exact_backup_and_read_on_partition_bearing_record(self):
        with patch.object(m15, "corpus", partitioned_recovery_corpus):
            m15.IndependentRecoveryTests.test_loss_public_route_two_restarts_fresh_validation_and_exact_ownership(
                self
            )

    def test_m15_projection_replay_on_partition_bearing_record(self):
        with patch.object(m15, "corpus", partitioned_recovery_corpus):
            m15.IndependentRecoveryTests.test_altered_projections_replay_only_complete_original_snapshots(
                self
            )

    def test_m15_bad_backups_on_partition_bearing_record(self):
        with patch.object(m15, "corpus", partitioned_recovery_corpus):
            m15.IndependentRecoveryTests.test_absent_blank_wrong_stale_and_changed_byte_evidence_never_writes(
                self
            )

    def test_m15_live_plan_and_gate_invalidation_on_partition_bearing_record(self):
        with patch.object(m15, "corpus", partitioned_recovery_corpus):
            m15.IndependentRecoveryTests.test_live_plan_gate_invalidation_and_repeat_require_validation(
                self
            )

    def test_m15_abrupt_child_restore_boundaries_on_partition_bearing_record(self):
        with patch.object(m15, "corpus", partitioned_recovery_corpus):
            m15.IndependentRecoveryTests.test_abrupt_generation_restore_boundaries_restart_and_public_retry(
                self
            )

    def test_modified_partition_or_unit_proof_cannot_be_recovered(self):
        with partitioned_recovery_corpus(self) as f:
            lose(f)
            variants = []
            for field in ("bounds", "unit", "source"):
                changed = copy.deepcopy(f.saved.document)
                snapshot = changed["map_snapshot"]
                if field == "bounds":
                    snapshot["partitions"]["bootloader"]["end"] += 0x1000
                elif field == "unit":
                    snapshot["bootloader_policy"]["factory_identity"]["observations"][-1] ^= 1
                else:
                    snapshot["bootloader_policy"]["factory_identity"]["source"]["sha256"] = "0" * 64
                # Even recomputing the public policy digest cannot match the
                # surviving trusted pointer's exact whole-file authority.
                changed.pop("policy_digest")
                canonical = json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()
                changed["policy_digest"] = hashlib.sha256(canonical).hexdigest()
                backup = f.root / ("changed-" + field + ".json")
                backup.write_text(json.dumps(changed), encoding="utf-8")
                variants.append(backup)
            with restart(f) as g:
                for backup in variants:
                    with self.subTest(backup=backup.name):
                        reply = restore_route(self, g, str(backup))
                        self.assertEqual(reply["status"], "policy_restore_blocked", reply)
                        self.assertFalse(f.saved.path.exists())
                        self.assertEqual(f.saved.pointer.read_bytes(), f.saved.pointer_bytes)
                        self.assertIsNone(g.server.gate_manager.snapshot(g.board))
                        assert_closed(self, g)
                self.assertEqual(
                    restore_route(self, g, str(f.backup))["status"], "policy_restore_completed"
                )
                assert_committed(self, g)
                self.assertIsNone(g.server.gate_manager.snapshot(g.board))

    def test_pack_partition_exact_recovery_restores_whole_record_without_gate(self):
        with full_fixture(self, "pack") as f:
            f.allocate()
            self.assertEqual(f.admit()["status"], "safety_refresh_completed")
            self.assertEqual(f.validate()["status"], "validation_passed")
            saved = f.record()
            backup = f.root / "exact-partition-backup.json"
            backup.write_bytes(saved.data)
            f.flow.call("disconnect", dict(board_id=f.board))
            saved.path.unlink()
            f.start()
            caps = f.caps()
            load = caps["recovery"]["load_call"]
            f.flow.json(load["tool"], load["arguments"])
            calls = list(f.backend.calls)
            reply = f.flow.json(
                "board_policy_restore", dict(board_id=f.board, generation_backup_path=str(backup))
            )
            self.assertEqual(reply["status"], "policy_restore_completed", reply)
            self.assertEqual(f.backend.calls, calls)
            self.assertEqual(f.record().data, saved.data)
            self.assertEqual(f.record().pointer_bytes, saved.pointer_bytes)
            self.assertFalse(reply["ready_for_code"])
            self.assertIsNone(f.server.gate_manager.snapshot(f.board))
            f.start()
            self.assertIsNone(f.caps()["identity"]["capability"])
            f.reconnect()
            self.assertEqual(f.validate()["status"], "validation_passed")
            self.assertEqual(f.caps()["identity"]["capability"], "exact")
            self.assertEqual(f.record().data, saved.data)
            self.assertEqual(f.record().pointer_bytes, saved.pointer_bytes)


if __name__ == "__main__":
    unittest.main()
