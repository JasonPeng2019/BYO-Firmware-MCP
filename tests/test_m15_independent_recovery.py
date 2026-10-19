"""Independent public U02 regressions. No M15 implementer fixture/oracle imports."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tests.m05_public_support import NULL_PLAN

from tests.m15_independent_support import (
    RAM,
    abrupt,
    assert_closed,
    assert_committed,
    assert_read,
    corpus,
    lose,
    planned_read,
    restart,
    restore_route,
)
from tests.test_m05_lite_validation import advertised_validation


class IndependentRecoveryTests(unittest.TestCase):
    def test_old_public_loader_offers_eligible_route_for_corrupt_committed_state(self):
        with corpus(self) as f:
            lose(f)
            with restart(f) as g:
                assert_closed(self, g)
                reply = g.flow.json(
                    "load_setup_tool", dict(board_id=f.board, tool_name="board_setup-plan")
                )
                # Existing public behavior, rather than availability of a new API/key,
                # is the old-source sensitivity oracle for F13's circular loader.
                self.assertEqual(reply.get("status"), "setup_policy_recovery_required", reply)
                self.assertEqual(
                    reply["recovery"]["load_call"]["arguments"]["tool_name"], "board_policy_restore"
                )

    def test_loss_public_route_two_restarts_fresh_validation_and_exact_ownership(self):
        with corpus(self) as f:
            lose(f)
            with restart(f) as broken:
                assert_closed(self, broken)
                for tool in ("board_setup-plan", "board_safety_refresh", "board_validate"):
                    reply = broken.flow.json(
                        "load_setup_tool", dict(board_id=f.board, tool_name=tool)
                    )
                    self.assertEqual(reply["status"], "setup_policy_recovery_required", reply)
                    self.assertEqual(
                        reply["recovery"]["generation_digest"],
                        f.saved.reference["generation_digest"],
                    )
                status = broken.flow.json("get_setup_status", dict(board_id=f.board))
                self.assertIn("board_policy_restore", json.dumps(status))
                overview = broken.flow.overview("Independent M14 generic erase")
                self.assertEqual(overview["status"], "setup_policy_recovery_required", overview)
                restored = restore_route(self, broken, str(f.backup))
                self.assertEqual(restored["status"], "policy_restore_completed", restored)
                self.assertFalse(restored["ready_for_code"])
                self.assertTrue(restored["validation_required"])
                assert_committed(self, broken)
            with restart(f) as fresh:
                self.assertNotEqual(fresh.server.server_run.run_id, broken.server.server_run.run_id)
                caps = fresh.flow.json("get_capabilities", dict(board_id=f.board))
                self.assertIsNone(caps["identity"]["capability"], caps)
                status = fresh.flow.json("get_setup_status", dict(board_id=f.board))
                self.assertFalse(status["ready_for_code"], status)
                fresh.flow.overview("Independent M14 generic erase")
                fresh.flow.call("connect", dict(board_id=f.board))
                _, denied = planned_read(fresh)
                self.assertEqual(denied["status"], "batch_failed", denied)
                self.assertIn("validat", json.dumps(denied).lower())
                self.assertEqual(
                    advertised_validation(fresh.flow, f.board)["status"], "validation_passed"
                )
                assert_read(self, fresh)
                assert_committed(self, fresh)
                self.assertEqual(f.backend.programmed, f.target_before)

    def test_altered_projections_replay_only_complete_original_snapshots(self):
        with corpus(self) as f:
            profile = next((f.root / ".firm/boards").glob(f.board + ".*"))
            projection = f.root / ".firm/safety" / f.board / "memory_map.yaml"
            lose(f)
            profile.write_text(
                json.dumps(
                    dict(
                        board_id=f.board,
                        display_name="wrong projection",
                        pyocd_target="cortex_m",
                        mcu_part_number="wrong",
                    )
                )
            )
            projection.write_text('{"deployment_policy":{"kind":"invented"}}')
            with restart(f) as g:
                restored = restore_route(self, g, str(f.backup.relative_to(f.root)))
                self.assertEqual(restored["status"], "policy_restore_completed", restored)
                self.assertEqual(
                    json.loads(profile.read_text()), f.saved.document["profile_snapshot"]
                )
                self.assertEqual(
                    json.loads(projection.read_text()), f.saved.document["map_snapshot"]
                )
                assert_committed(self, g)
            with restart(f) as g:
                overview = g.flow.overview("Independent M14 generic erase")
                self.assertEqual(overview["status"], "setup_routes_ready", overview)
                self.assertEqual(
                    advertised_validation(g.flow, f.board)["status"], "validation_passed"
                )
                assert_read(self, g)
                assert_committed(self, g)

    def test_absent_blank_wrong_stale_and_changed_byte_evidence_never_writes(self):
        with corpus(self) as f:
            lose(f)
            wrong = f.root / "wrong.json"
            wrong.write_bytes(f.saved.data + b"\n")  # Valid JSON, wrong full-file digest.
            stale = f.root / "stale.json"
            stale.write_bytes(f.stale)
            malformed = f.root / "malformed.json"
            malformed.write_bytes(b"{")
            with restart(f) as g:
                original_files = {
                    str(p.relative_to(f.root)): p.read_bytes()
                    for p in (f.root / ".firm").rglob("*")
                    if p.is_file() and "runs" not in p.parts
                }
                for source in (
                    None,
                    "",
                    "  ",
                    str(f.root / "absent.json"),
                    str(f.root),
                    str(wrong),
                    str(stale),
                    str(malformed),
                ):
                    with self.subTest(source=source):
                        result = restore_route(self, g, source)
                        self.assertEqual(result["status"], "policy_restore_blocked", result)
                        self.assertIn("byte-for-byte", result["recovery"]["prerequisite"])
                        self.assertFalse(result["ready_for_code"])
                        self.assertFalse(f.saved.path.exists())
                        self.assertEqual(f.saved.pointer.read_bytes(), f.saved.pointer_bytes)
                        self.assertEqual(f.backup.read_bytes(), f.saved.data)
                        self.assertEqual(
                            {
                                str(p.relative_to(f.root)): p.read_bytes()
                                for p in (f.root / ".firm").rglob("*")
                                if p.is_file() and "runs" not in p.parts
                            },
                            original_files,
                        )
                        assert_closed(self, g)

    def test_missing_and_invalid_pointer_names_original_trusted_pointer_prerequisite(self):
        with corpus(self) as f:
            lose(f)
            variants = (
                None,
                b"{",
                b"{}",
                json.dumps({**f.saved.reference, "extra": True}).encode(),
                json.dumps({**f.saved.reference, "board_id": "another_board"}).encode(),
            )
            with restart(f) as g:
                for payload in variants:
                    with self.subTest(pointer=payload):
                        if payload is None:
                            f.saved.pointer.unlink(missing_ok=True)
                        else:
                            f.saved.pointer.write_bytes(payload)
                        g.flow.json(
                            "load_setup_tool",
                            dict(board_id=f.board, tool_name="board_policy_restore"),
                        )
                        before = len(f.backend.calls)
                        result = g.flow.json(
                            "board_policy_restore",
                            dict(board_id=f.board, generation_backup_path=str(f.backup)),
                        )
                        self.assertEqual(result["status"], "policy_restore_blocked", result)
                        self.assertIn(
                            "original committed current.json", result["recovery"]["prerequisite"]
                        )
                        self.assertNotIn("generation_id", result["recovery"])
                        self.assertEqual(len(f.backend.calls), before)
                        self.assertFalse(f.saved.path.exists())
                        self.assertEqual(
                            f.saved.pointer.read_bytes() if payload is not None else None, payload
                        )
                        self.assertEqual(f.backup.read_bytes(), f.saved.data)
                f.saved.pointer.write_bytes(f.saved.pointer_bytes)

    def test_live_plan_gate_invalidation_and_repeat_require_validation(self):
        with corpus(self) as f:
            for repeat in range(2):
                with self.subTest(repeat=repeat):
                    self.assertTrue(
                        f.flow.json("get_setup_status", dict(board_id=f.board))["ready_for_code"]
                    )
                    f.flow.call("read_memory_address-plan", NULL_PLAN)
                    plan = f.flow.plan(
                        "read_memory_address", f.board, dict(address=RAM, width=32, length=None)
                    )
                    self.assertEqual(plan["status"], "plan_accepted", plan)
                    f.flow.json(
                        "load_setup_tool", dict(board_id=f.board, tool_name="board_policy_restore")
                    )
                    before = len(f.backend.calls)
                    result = f.flow.json(
                        "board_policy_restore",
                        dict(board_id=f.board, generation_backup_path=str(f.backup)),
                    )
                    self.assertEqual(result["status"], "policy_restore_completed", result)
                    self.assertEqual(len(f.backend.calls), before)
                    self.assertFalse(
                        f.flow.json("get_setup_status", dict(board_id=f.board))["ready_for_code"]
                    )
                    denied = f.flow.execute(plan["stable_client_fallback"])
                    self.assertEqual(denied["status"], "batch_failed", denied)
                    reads_before = [c for c in f.backend.calls if c[0] == "read"]
                    _, denied = planned_read(f)
                    self.assertEqual(denied["status"], "batch_failed", denied)
                    self.assertEqual([c for c in f.backend.calls if c[0] == "read"], reads_before)
                    self.assertEqual(
                        advertised_validation(f.flow, f.board)["status"], "validation_passed"
                    )
                    assert_read(self, f)
                    assert_committed(self, f)

    def test_abrupt_generation_restore_boundaries_restart_and_public_retry(self):
        with corpus(self) as f:
            for point in ("before_generation_restore", "after_generation_restore"):
                with self.subTest(point=point):
                    if f.saved.path.exists():
                        lose(f)
                    abrupt(self, f, point)
                    self.assertEqual(f.saved.path.exists(), point == "after_generation_restore")
                    self.assertEqual(f.saved.pointer.read_bytes(), f.saved.pointer_bytes)
                    with restart(f) as g:
                        self.assertFalse(
                            g.flow.json("get_setup_status", dict(board_id=f.board))[
                                "ready_for_code"
                            ]
                        )
                        g.flow.json(
                            "load_setup_tool",
                            dict(board_id=f.board, tool_name="board_policy_restore"),
                        )
                        result = g.flow.json(
                            "board_policy_restore",
                            dict(board_id=f.board, generation_backup_path=str(f.backup)),
                        )
                        self.assertEqual(result["status"], "policy_restore_completed", result)
                        assert_committed(self, g)
                        g.flow.overview("Independent M14 generic erase")
                        self.assertEqual(
                            advertised_validation(g.flow, f.board)["status"], "validation_passed"
                        )
                        assert_read(self, g)

    def test_failed_projection_replay_truthful_block_and_fresh_public_retry(self):
        with corpus(self) as f:
            lose(f)
            with restart(f) as g:
                real = g.server._firm_store.atomic_write_json

                def fail_map(path, document):
                    if path.name == "memory_map.yaml":
                        raise OSError("independent controlled map-write failure")
                    return real(path, document)

                with patch.object(g.server._firm_store, "atomic_write_json", fail_map):
                    result = restore_route(self, g, str(f.backup))
                self.assertEqual(result["status"], "policy_restore_blocked", result)
                self.assertTrue(result["generation_restored"])
                self.assertEqual(
                    result["retry_call"]["arguments"]["generation_backup_path"], str(f.backup)
                )
                self.assertFalse(result["ready_for_code"])
                assert_committed(self, g)
            with restart(f) as g:
                g.flow.json(
                    "load_setup_tool", dict(board_id=f.board, tool_name="board_policy_restore")
                )
                retried = g.flow.json(
                    result["retry_call"]["tool"], result["retry_call"]["arguments"]
                )
                self.assertEqual(retried["status"], "policy_restore_completed", retried)
                g.flow.overview("Independent M14 generic erase")
                self.assertEqual(
                    advertised_validation(g.flow, f.board)["status"], "validation_passed"
                )
                assert_read(self, g)
                assert_committed(self, g)

    def test_abrupt_projection_replay_usable_restart_exact_backup_retry(self):
        with corpus(self) as f:
            for point in ("before_profile_projection", "before_map_projection"):
                with self.subTest(point=point):
                    lose(f)
                    abrupt(self, f, point)
                    self.assertEqual(f.saved.path.read_bytes(), f.saved.data)
                    with restart(f) as g:
                        self.assertFalse(
                            g.flow.json("get_setup_status", dict(board_id=f.board))[
                                "ready_for_code"
                            ]
                        )
                        g.flow.json(
                            "load_setup_tool",
                            dict(board_id=f.board, tool_name="board_policy_restore"),
                        )
                        result = g.flow.json(
                            "board_policy_restore",
                            dict(board_id=f.board, generation_backup_path=str(f.backup)),
                        )
                        self.assertEqual(result["status"], "policy_restore_completed", result)
                        g.flow.overview("Independent M14 generic erase")
                        self.assertEqual(
                            advertised_validation(g.flow, f.board)["status"], "validation_passed"
                        )
                        assert_read(self, g)
                        assert_committed(self, g)

    def test_unpublished_generation_cannot_replace_pointer_selected_evidence(self):
        with corpus(self) as f:
            f.flow.call("disconnect", dict(board_id=f.board))
            before = set(f.saved.path.parent.glob("*.json"))
            abrupt(self, f, "before_pointer_replace", action="deploy")
            self.assertEqual(f.saved.pointer.read_bytes(), f.saved.pointer_bytes)
            orphans = set(f.saved.path.parent.glob("*.json")) - before
            self.assertEqual(len(orphans), 1, orphans)
            orphan = orphans.pop()
            self.assertNotEqual(orphan.read_bytes(), f.saved.data)
            lose(f)
            with restart(f) as g:
                result = restore_route(self, g, str(orphan))
                self.assertEqual(result["status"], "policy_restore_blocked", result)
                self.assertFalse(f.saved.path.exists())
                assert_closed(self, g)
                result = restore_route(self, g, str(f.backup))
                self.assertEqual(result["status"], "policy_restore_completed", result)
                assert_committed(self, g)

    def test_healthy_refresh_and_planned_read_control(self):
        with corpus(self) as f:
            f.flow.json("load_setup_tool", dict(board_id=f.board, tool_name="board_safety_refresh"))
            reply = f.flow.json("board_safety_refresh", dict(board_id=f.board))
            self.assertEqual(reply["status"], "safety_refresh_completed", reply)
            self.assertEqual(advertised_validation(f.flow, f.board)["status"], "validation_passed")
            assert_read(self, f)
            self.assertEqual(
                f.server._capability_policies.resolve(f.board).map_snapshot,
                f.saved.document["map_snapshot"],
            )
            self.assertEqual(f.backend.programmed, f.target_before)


if __name__ == "__main__":
    unittest.main()
