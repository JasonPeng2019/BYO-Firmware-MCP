"""M15 public committed-policy restoration, genuine FULL setup at a controlled I/O boundary."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from tests.m15_public_support import BOARD, RAM, RAM_VALUE, public_fixture


class PublicPolicyRecovery(unittest.TestCase):
    def assert_refused(self, fixture, tool, arguments, *, contains):
        before = list(fixture.target.calls)
        try:
            reply = fixture.call(tool, arguments)
        except ToolError as exc:
            reply = str(exc)
        self.assertIn(contains.casefold(), reply.casefold(), reply)
        self.assertEqual(fixture.target.calls, before, "refusal reached no target I/O")
        return reply

    def corrupt_refusals(self, fixture):
        for tool in ("read_memory_address", "read_memory_raw"):
            self.assert_refused(
                fixture,
                tool,
                {"board_id": BOARD, "address": RAM, "width": 32},
                contains="corrupt committed safety policy",
            )
        status = fixture.flow.json("get_setup_status", {"board_id": BOARD})
        self.assertFalse(status["ready_for_code"], status)
        capabilities = fixture.flow.json("get_capabilities", {"board_id": BOARD})
        self.assertEqual(capabilities["policy_status"], "corrupt", capabilities)
        self.assertEqual(capabilities["capabilities"], [])
        return capabilities

    def load_advertised_recovery(self, fixture):
        # Deliberately try the historical public remedy first. Against the exact
        # old source this fails on the real setup_plan_ineligible behavior, before
        # attempting to call any new API.
        loaded = fixture.flow.json(
            "load_setup_tool", {"board_id": BOARD, "tool_name": "board_setup-plan"}
        )
        self.assertEqual(loaded["status"], "setup_policy_recovery_required", loaded)
        call = loaded["recovery"]["load_call"]
        recovery = fixture.flow.json(call["tool"], call["arguments"])
        self.assertEqual(recovery["status"], "setup_tool_loaded", recovery)
        return recovery

    def restore(self, fixture, loaded, source):
        call = loaded["next_call"]
        arguments = {**call["arguments"], "generation_backup_path": source}
        return fixture.flow.json(call["tool"], arguments)

    def assert_planned_read(self, fixture):
        plan = fixture.read_plan()
        self.assertEqual(plan["status"], "plan_accepted", plan)
        reply = fixture.call(
            "read_memory_address", {"board_id": BOARD, "address": RAM, "width": 32, "length": None}
        )
        self.assertIn(f"0x{RAM_VALUE:08X}".casefold(), reply.casefold(), reply)
        return reply

    def test_loss_restart_public_restore_restart_validate_read_preserves_owned_authority(self):
        with public_fixture(self) as fixture:
            fixture.full_setup()
            original = fixture.server._capability_policies.resolve(BOARD)
            self.assertEqual(original.tier, Tier.SETUP_FULL)
            self.assertIsNotNone(original.map_snapshot["partitions"]["application"])
            self.assertNotEqual(original.map_snapshot["deployment_policy"]["kind"], "none")
            memory = bytes(fixture.target.memory)
            fixture.call("disconnect", {"board_id": BOARD})
            pointer, generation, backup = fixture.lose_generation()
            expected_pointer, expected_generation = pointer.read_bytes(), backup.read_bytes()
            selected = json.loads(expected_pointer)
            self.assertEqual(
                hashlib.sha256(expected_generation).hexdigest(), selected["generation_digest"]
            )
            fixture.start()
            capabilities = self.corrupt_refusals(fixture)
            overview = fixture.flow.overview("M15 host")
            self.assertEqual(overview["status"], "setup_policy_recovery_required", overview)
            self.assertEqual(overview["routes"][0]["next_tool"], "board_policy_restore")
            loaded = self.load_advertised_recovery(fixture)
            self.assertEqual(capabilities["recovery"]["generation_id"], selected["generation_id"])
            self.assertEqual(loaded["recovery"]["generation_digest"], selected["generation_digest"])
            before = list(fixture.target.calls)
            reply = self.restore(fixture, loaded, str(backup.relative_to(fixture.root)))
            self.assertEqual(reply["status"], "policy_restore_completed", reply)
            self.assertTrue(reply["validation_required"])
            self.assertFalse(reply["ready_for_code"])
            self.assertEqual(fixture.target.calls, before, "restoration is host-only")
            self.assertEqual(pointer.read_bytes(), expected_pointer)
            self.assertEqual(generation.read_bytes(), expected_generation)
            self.assertEqual(backup.read_bytes(), expected_generation)
            restored = fixture.server._capability_policies.resolve(BOARD)
            self.assertEqual(restored, original)
            fixture.start()
            self.assertEqual(len(set(fixture.runs)), 3, "three distinct server run identities")
            status = fixture.flow.json("get_setup_status", {"board_id": BOARD})
            self.assertTrue(status["configuration_ready"], status)
            self.assertFalse(status["ready_for_code"], status)
            fixture.flow.overview("M15 host")
            fixture.call("connect", {"board_id": BOARD})
            self.assertFalse(
                fixture.flow.json("get_setup_status", {"board_id": BOARD})["ready_for_code"]
            )
            # Existing plans describe intent; execution requires the live gate.
            self.assertEqual(fixture.read_plan()["status"], "plan_accepted")
            self.assert_refused(
                fixture,
                "read_memory_address",
                {"board_id": BOARD, "address": RAM, "width": 32, "length": None},
                contains="validat",
            )
            fixture.validate()
            status = fixture.flow.json("get_setup_status", {"board_id": BOARD})
            self.assertTrue(status["ready_for_code"], status)
            self.assertIn(status["identity_capability"], {"compatible", "exact"})
            self.assert_planned_read(fixture)
            self.assertEqual(bytes(fixture.target.memory), memory)
            self.assertEqual(fixture.server._capability_policies.resolve(BOARD), original)
            self.assertEqual(pointer.read_bytes(), expected_pointer)

    def test_altered_projections_cannot_change_restored_ownership_or_identity(self):
        with public_fixture(self) as fixture:
            fixture.full_setup()
            original = fixture.server._capability_policies.resolve(BOARD)
            fixture.call("disconnect", {"board_id": BOARD})
            pointer, generation, backup = fixture.lose_generation()
            # A parsable projection deliberately removes deployment ownership.
            altered_map = json.loads(backup.read_bytes())["map_snapshot"]
            altered_map["partitions"]["application"] = None
            altered_map["deployment_policy"] = {"kind": "none"}
            projection = fixture.root / ".firm/safety" / BOARD / "memory_map.yaml"
            projection.write_text(json.dumps(altered_map), encoding="utf-8")
            profile = fixture.root / ".firm/boards" / (BOARD + ".yaml")
            import yaml

            altered_profile = yaml.safe_load(profile.read_text(encoding="utf-8"))
            altered_profile["mcu_part_number"] = "STM32L476RGT7"
            profile.write_text(json.dumps(altered_profile), encoding="utf-8")
            projection_bytes, profile_bytes = projection.read_bytes(), profile.read_bytes()
            altered_backup = fixture.root / "altered-projections"
            altered_backup.mkdir()
            (altered_backup / "memory_map.yaml").write_bytes(projection_bytes)
            (altered_backup / "profile.yaml").write_bytes(profile_bytes)
            fixture.start()
            self.corrupt_refusals(fixture)
            loaded = self.load_advertised_recovery(fixture)
            reply = self.restore(fixture, loaded, str(backup))
            self.assertEqual(reply["status"], "policy_restore_completed", reply)
            self.assertEqual(fixture.server._capability_policies.resolve(BOARD), original)
            self.assertEqual(generation.read_bytes(), backup.read_bytes())
            self.assertEqual(yaml.safe_load(projection.read_bytes()), original.map_snapshot)
            self.assertEqual(yaml.safe_load(profile.read_bytes()), original.profile_snapshot)
            self.assertNotEqual(projection.read_bytes(), projection_bytes)
            self.assertNotEqual(profile.read_bytes(), profile_bytes)
            fixture.start()
            fixture.validate()
            self.assert_planned_read(fixture)
            self.assertEqual(fixture.server._capability_policies.resolve(BOARD), original)

    def test_absent_mismatched_stale_and_unpublished_sources_stay_blocked(self):
        with public_fixture(self) as fixture:
            fixture.full_setup()
            original = fixture.server._capability_policies.resolve(BOARD)
            fixture.call("disconnect", {"board_id": BOARD})
            pointer, generation, backup = fixture.lose_generation()
            pointer_bytes, backup_bytes = pointer.read_bytes(), backup.read_bytes()

            def interrupt(name):
                if name == "before_pointer_replace":
                    raise OSError("unpublished replacement fixture")

            repository = CapabilityPolicyRepository(fixture.root, fault_hook=interrupt)
            before = set(generation.parent.iterdir())
            with self.assertRaisesRegex(OSError, "unpublished replacement"):
                repository.commit(
                    BOARD,
                    Tier.SETUP_FULL,
                    profile_snapshot=original.profile_snapshot,
                    map_snapshot=original.map_snapshot,
                    evidence={"uncommitted": True},
                )
            (orphan,) = set(generation.parent.iterdir()) - before
            stale = next(
                path for path in before if json.loads(path.read_bytes()).get("tier") == "setup-full"
            )
            wrong = fixture.root / "wrong-generation.json"
            wrong.write_bytes(backup_bytes + b" ")  # same semantic JSON, wrong full-file digest
            fixture.start()
            loaded = self.load_advertised_recovery(fixture)
            calls = list(fixture.target.calls)
            for source in (
                None,
                "",
                str(fixture.root / "missing.json"),
                str(wrong),
                str(stale),
                str(orphan),
            ):
                with self.subTest(source=source):
                    reply = self.restore(fixture, loaded, source)
                    self.assertEqual(reply["status"], "policy_restore_blocked", reply)
                    self.assertIn("byte-for-byte backup", reply["agent_prompt"])
                    self.assertEqual(
                        reply["recovery"]["generation_digest"],
                        json.loads(pointer_bytes)["generation_digest"],
                    )
                    self.assertFalse(generation.exists())
                    self.assertEqual(pointer.read_bytes(), pointer_bytes)
                    self.assertEqual(backup.read_bytes(), backup_bytes)
                    self.corrupt_refusals(fixture)
            self.assertEqual(fixture.target.calls, calls)
            reply = self.restore(fixture, loaded, str(backup))
            self.assertEqual(reply["status"], "policy_restore_completed", reply)
            self.assertEqual(fixture.server._capability_policies.resolve(BOARD), original)

    def test_invalid_pointer_requires_original_project_backup_and_is_never_replaced(self):
        with public_fixture(self) as fixture:
            fixture.full_setup(ownership=False)
            fixture.call("disconnect", {"board_id": BOARD})
            pointer, generation, backup = fixture.lose_generation()
            pointer.write_bytes(b"{broken pointer")
            fixture.start()
            loaded = self.load_advertised_recovery(fixture)
            before = list(fixture.target.calls)
            for source in (None, str(backup)):
                reply = self.restore(fixture, loaded, source)
                self.assertEqual(reply["status"], "policy_restore_blocked", reply)
                self.assertIn("original committed current.json", reply["agent_prompt"])
                self.assertEqual(pointer.read_bytes(), b"{broken pointer")
                self.assertFalse(generation.exists())
            self.assertEqual(fixture.target.calls, before)
            self.corrupt_refusals(fixture)

    def test_repair_clears_a_preexisting_live_stamp_and_plan_even_for_same_generation(self):
        with public_fixture(self) as fixture:
            fixture.full_setup(ownership=False)
            original = fixture.server._capability_policies.resolve(BOARD)
            self.assertEqual(fixture.read_plan()["status"], "plan_accepted")
            self.assertTrue(
                fixture.flow.json("get_setup_status", {"board_id": BOARD})["ready_for_code"]
            )
            pointer, generation, backup = fixture.lose_generation()
            loaded = self.load_advertised_recovery(fixture)
            before = list(fixture.target.calls)
            reply = self.restore(fixture, loaded, str(backup))
            self.assertEqual(reply["status"], "policy_restore_completed", reply)
            self.assertEqual(fixture.target.calls, before)
            self.assertIsNone(fixture.server.plan_engine.active_plan("read_memory_address", BOARD))
            self.assertFalse(
                fixture.flow.json("get_setup_status", {"board_id": BOARD})["ready_for_code"]
            )
            self.assertEqual(fixture.server._capability_policies.resolve(BOARD), original)
            fixture.validate()
            self.assert_planned_read(fixture)
            # Exact repeat never resurrects the new stamp either.
            self.assertEqual(
                self.restore(fixture, loaded, str(backup))["status"], "policy_restore_completed"
            )
            self.assertFalse(
                fixture.flow.json("get_setup_status", {"board_id": BOARD})["ready_for_code"]
            )

    def test_abrupt_public_repair_leaves_only_corrupt_or_exact_restored_generation(self):
        with public_fixture(self) as fixture:
            fixture.full_setup()
            original = fixture.server._capability_policies.resolve(BOARD)
            fixture.call("disconnect", {"board_id": BOARD})
            pointer, generation, backup = fixture.lose_generation()
            pointer_bytes, generation_bytes = pointer.read_bytes(), backup.read_bytes()
            projection = fixture.root / ".firm/safety" / BOARD / "memory_map.yaml"
            projection.write_text("damaged compatibility projection", encoding="utf-8")
            fixture.stop()
            for fault, restored in (
                ("before_generation_restore", False),
                ("after_generation_restore", True),
            ):
                with self.subTest(fault=fault):
                    attempt = fixture.root / fault
                    attempt.mkdir()
                    script = attempt / "repair.py"
                    script.write_text(
                        "import hashlib, json, os\n"
                        "from pathlib import Path\n"
                        "from tests.tiered_acceptance_support import fresh_server, public_text\n"
                        "root = Path(os.environ['BYO_MCP_ARTIFACT_ROOT'])\n"
                        "server = fresh_server(root)\n"
                        "import pyocd_debug_mcp.capabilities.policy as policy\n"
                        "paths = [server.__file__, policy.__file__]\n"
                        f"Path({str(attempt / 'imports.json')!r}).write_text(json.dumps([{{'path': p, 'sha256': hashlib.sha256(Path(p).read_bytes()).hexdigest()}} for p in paths]))\n"
                        f"public_text(server, 'load_setup_tool', {{'board_id': {BOARD!r}, 'tool_name': 'board_policy_restore'}})\n"
                        f"public_text(server, 'board_policy_restore', {{'board_id': {BOARD!r}, 'generation_backup_path': {str(backup)!r}}})\n",
                        encoding="utf-8",
                    )
                    # The generated script lives outside this checkout.
                    checkout = Path(__file__).resolve().parents[1]
                    env = {
                        **os.environ,
                        "PYTHONPATH": os.pathsep.join((str(checkout), str(checkout / "src"))),
                        "BYO_MCP_ARTIFACT_ROOT": str(fixture.root),
                        "BYO_MCP_MONITOR_ROOT": str(attempt / "monitor"),
                        "BYO_MCP_TIER_FAULT": fault,
                        "BYO_MCP_TEST_FAULTS": "1",
                    }
                    argv = [sys.executable, "-B", str(script)]
                    result = subprocess.run(
                        argv,
                        cwd=Path(__file__).resolve().parents[1],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    (attempt / "result.json").write_text(
                        json.dumps(
                            {
                                "argv": argv,
                                "cwd": str(Path.cwd()),
                                "exit": result.returncode,
                                "stdout": result.stdout,
                                "stderr": result.stderr,
                                "selected_env": {
                                    name: env[name]
                                    for name in (
                                        "BYO_MCP_ARTIFACT_ROOT",
                                        "BYO_MCP_MONITOR_ROOT",
                                        "BYO_MCP_TIER_FAULT",
                                    )
                                },
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    self.assertEqual(result.returncode, 86, result)
                    self.assertEqual(pointer.read_bytes(), pointer_bytes)
                    self.assertEqual(backup.read_bytes(), generation_bytes)
                    self.assertEqual(generation.exists(), restored)
                    fixture.start()
                    if restored:
                        self.assertEqual(generation.read_bytes(), generation_bytes)
                        self.assertEqual(
                            fixture.server._capability_policies.resolve(BOARD), original
                        )
                        self.assertFalse(
                            fixture.flow.json("get_setup_status", {"board_id": BOARD})[
                                "ready_for_code"
                            ]
                        )
                    else:
                        self.corrupt_refusals(fixture)
                    fixture.stop()
            fixture.start()
            loaded = fixture.flow.json(
                "load_setup_tool", {"board_id": BOARD, "tool_name": "board_policy_restore"}
            )
            reply = self.restore(fixture, loaded, str(backup))
            self.assertEqual(reply["status"], "policy_restore_completed", reply)
            fixture.start()
            fixture.validate()
            self.assert_planned_read(fixture)
            self.assertEqual(fixture.server._capability_policies.resolve(BOARD), original)

    def test_projection_write_failure_is_blocked_and_public_restart_retry_finishes_replay(self):
        with public_fixture(self) as fixture:
            fixture.full_setup()
            original = fixture.server._capability_policies.resolve(BOARD)
            fixture.call("disconnect", {"board_id": BOARD})
            pointer, generation, backup = fixture.lose_generation()
            pointer_bytes, generation_bytes = pointer.read_bytes(), backup.read_bytes()
            projection = fixture.root / ".firm/safety" / BOARD / "memory_map.yaml"
            projection.write_text("damaged map projection", encoding="utf-8")
            fixture.start()
            loaded = self.load_advertised_recovery(fixture)
            write = fixture.server._firm_store.atomic_write_json

            def fail_map(path, document):
                if path == projection:
                    raise OSError("controlled map projection write failure")
                return write(path, document)

            calls = list(fixture.target.calls)
            with patch.object(
                fixture.server._firm_store, "atomic_write_json", side_effect=fail_map
            ):
                failed = self.restore(fixture, loaded, str(backup))
            self.assertEqual(failed["status"], "policy_restore_blocked", failed)
            self.assertTrue(failed["generation_restored"])
            self.assertIn("controlled map projection write failure", failed["reason"])
            self.assertIn("same exact backup", failed["agent_prompt"])
            self.assertTrue(failed["validation_required"])
            self.assertFalse(failed["ready_for_code"])
            self.assertEqual(fixture.target.calls, calls)
            self.assertEqual(generation.read_bytes(), generation_bytes)
            self.assertEqual(pointer.read_bytes(), pointer_bytes)
            self.assertEqual(fixture.server._capability_policies.resolve(BOARD), original)
            fixture.start()
            self.assertFalse(
                fixture.flow.json("get_setup_status", {"board_id": BOARD})["ready_for_code"]
            )
            # Reload after restart, then follow the exact returned retry call.
            fixture.flow.json(
                "load_setup_tool", {"board_id": BOARD, "tool_name": "board_policy_restore"}
            )
            retry = failed["retry_call"]
            reply = fixture.flow.json(retry["tool"], retry["arguments"])
            self.assertEqual(reply["status"], "policy_restore_completed", reply)
            self.assertEqual(generation.read_bytes(), generation_bytes)
            self.assertEqual(backup.read_bytes(), generation_bytes)
            self.assertEqual(pointer.read_bytes(), pointer_bytes)
            fixture.start()
            fixture.validate()
            self.assert_planned_read(fixture)
            self.assertEqual(fixture.server._capability_policies.resolve(BOARD), original)

    def test_healthy_public_refresh_keeps_ownership_and_current_validation_usable(self):
        """Unaffected passing control for old-source behavioral sensitivity."""
        with public_fixture(self) as fixture:
            fixture.full_setup()
            policy = fixture.server._capability_policies.resolve(BOARD)
            fixture.flow.json(
                "load_setup_tool", {"board_id": BOARD, "tool_name": "board_safety_refresh"}
            )
            reply = fixture.flow.json("board_safety_refresh", {"board_id": BOARD})
            self.assertEqual(reply["status"], "safety_refresh_completed", reply)
            self.assertEqual(
                fixture.server._capability_policies.resolve(BOARD).map_snapshot, policy.map_snapshot
            )
            self.assertTrue(
                fixture.flow.json("get_setup_status", {"board_id": BOARD})["ready_for_code"]
            )
            self.assert_planned_read(fixture)


if __name__ == "__main__":
    unittest.main()
