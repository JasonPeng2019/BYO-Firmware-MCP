"""Independent M14 behavioral oracles through registered public MCP dispatch."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from pyocd_debug_mcp.adapters.swd_interface import session_metadata

from tests.m05_public_support import PublicSetup
from tests.b02_public_support import elf_fixture
from tests.m14_independent_support import (
    advertised_validation,
    approve,
    attempt,
    connect,
    disclose,
    erase_fields,
    grant,
    ordinary_full,
    safe_read,
    safe_value,
)
from tests.test_tiered_acceptance_lite import _setup_inventory
from tests.tiered_acceptance_support import fresh_server


class GenericEraseIndependentTests(unittest.TestCase):
    def test_ordinary_full_safe_read_control(self):
        """Unaffected public FULL/validation/read control for old-source sensitivity."""
        with ordinary_full(self) as f:
            self.assertEqual(safe_value(f), 0x6D143A72)
            self.assertEqual(f.backend.at_entry, [])

    def test_proposal_ranges_and_sectors_follow_two_installed_geometries(self):
        # These expected facts are independent of the product disclosure builder:
        # installed Nordic target main flash ends differ, while UICR has no
        # ordinary FULL setup erase-sector authority and must remain unknown.
        for target, end in (("nrf52840", 0x100000), ("nrf52832", 0x80000)):
            with self.subTest(target=target), ordinary_full(self, target=target) as f:
                proposal, refusal = None, None
                try:
                    proposal = disclose(f)
                except Exception as exc:
                    refusal = str(exc)
                self.assertIsNone(
                    refusal, f"Ordinary generic FULL must disclose recovery: {refusal}"
                )
                self.assertEqual(proposal["status"], "unlock_permission_requested", proposal)
                disclosure = proposal["disclosure"]
                spans = disclosure["erased_ranges"]
                self.assertEqual([(r["start"], r["end"]) for r in spans], [(0, end)])
                self.assertEqual(spans[0]["sectors"], list(range(end // 4096)))
                self.assertFalse(disclosure["all_nonvolatile_erased"])
                unknown = " ".join(disclosure["unknown_effects"]).lower()
                self.assertIn("10001000", unknown)
                self.assertIn("chip", unknown)
                self.assertIn("not established", unknown)
                self.assertIn("protection", unknown)
                self.assertIn("whole-chip", proposal["agent_prompt"])
                self.assertEqual(f.backend.at_entry, [])

    def test_one_grant_one_attempt_consumed_before_success_or_uncertain_failure(self):
        for failure in (False, True):
            with self.subTest(uncertain_failure=failure), ordinary_full(self) as f:
                proposal = disclose(f)
                grant(f, proposal)
                approved = approve(f)
                self.assertEqual(approved["status"], "unlock_plan_approved", approved)
                self.assertEqual(
                    f.server._manual_permissions.status("mass-erase").state, "reserved"
                )
                self.assertIsNotNone(f.server.gate_manager.snapshot(f.board))
                if failure:
                    f.backend.error = RuntimeError("independent uncertain erase failure")
                    with self.assertRaisesRegex(Exception, "independent uncertain erase failure"):
                        attempt(f)
                else:
                    self.assertIn("Target unlock completed", attempt(f))
                self.assertEqual(len(f.backend.at_entry), 1)
                self.assert_revoked_then_reusable(f)

    def assert_revoked_then_reusable(self, f):
        self.assertIsNone(f.server.gate_manager.snapshot(f.board))
        self.assertIsNone(f.server.connection_manager.maybe_connection(f.board))
        prior = [c for c in f.backend.calls if c[0] in {"erase", "read"}]
        with self.assertRaises(Exception):
            attempt(f)
        self.assert_read_refused(f)
        self.assertEqual([c for c in f.backend.calls if c[0] in {"erase", "read"}], prior)
        f.backend.error = None
        connect(f)
        self.assert_read_refused(f)
        self.assertEqual(advertised_validation(f.flow, f.board)["status"], "validation_passed")
        self.assertEqual(safe_value(f), 0x6D143A72)
        disclose(f)
        with self.assertRaises(Exception):
            approve(f)
        self.assertEqual(len(f.backend.at_entry), 1)

    def assert_read_refused(self, f):
        reads = [c for c in f.backend.calls if c[0] == "read"]
        try:
            response = safe_read(f)
        except Exception as exc:
            self.assertRegex(str(exc).lower(), "validat|connect|ready|locked")
        else:
            self.assertEqual(response["status"], "batch_failed", response)
        self.assertEqual([c for c in f.backend.calls if c[0] == "read"], reads)

    def test_missing_full_session_and_wrong_durable_bindings_never_erase(self):
        cases = (
            ({}, "manual/locked"),
            ({"board_id": "different-board"}, "manual/wrong-board"),
            ({"policy_digest": "0" * 64}, "manual/stale-policy"),
            ({"binding_digest": "0" * 64}, "manual/binding-mismatch"),
        )
        for overrides, code in cases:
            with self.subTest(code=code), ordinary_full(self) as f:
                proposal = disclose(f)
                with self.assertRaisesRegex(Exception, "fresh one-time permission"):
                    f.flow.call("target_unlock-plan", erase_fields(f.board, "full-session"))
                if overrides:
                    grant(f, proposal, **overrides)
                with self.assertRaisesRegex(Exception, code):
                    approve(f)
                self.assertEqual(f.backend.at_entry, [])
                self.assertFalse(any(c[0] == "erase" for c in f.backend.calls))
                self.assertIsNotNone(f.server.gate_manager.snapshot(f.board))
                self.assertEqual(safe_value(f), 0x6D143A72)

    def test_target_probe_session_and_mechanism_changes_refuse_before_backend(self):
        for changed in ("target", "probe", "session", "mechanism", "policy"):
            with self.subTest(changed=changed), ordinary_full(self) as f:
                proposal = disclose(f)
                grant(f, proposal)
                self.assertEqual(approve(f)["status"], "unlock_plan_approved")
                handle = f.server.connection_manager.connection_for(f.board).handle
                if changed in {"target", "probe"}:
                    metadata = session_metadata(handle)
                    # Simulate changed physical facts at the fake probe boundary.
                    metadata = replace(
                        metadata,
                        **(
                            {"live_part_number": "different-live-target"}
                            if changed == "target"
                            else {"probe_uid": "different-probe"}
                        ),
                    )
                    object.__setattr__(handle, "metadata", metadata)
                elif changed == "session":
                    f.flow.call("disconnect", {"board_id": f.board})
                    connect(f)
                    advertised_validation(f.flow, f.board)
                elif changed == "policy":
                    path = f.server._capability_policies.pointer_path(f.board)
                    original_pointer = path.read_bytes()
                    path.write_text("{}", encoding="utf-8")
                if changed == "mechanism":
                    with self.assertRaises(Exception):
                        f.flow.call(
                            "target_unlock",
                            {"board_id": f.board, "recovery_mechanism": "manual_only"},
                        )
                else:
                    with self.assertRaises(Exception):
                        attempt(f)
                if changed == "policy":
                    # End the controlled filesystem fault before ordinary
                    # cleanup; policy-corruption recovery belongs to M15.
                    path.write_bytes(original_pointer)
                self.assertEqual(f.backend.at_entry, [])
                self.assertFalse(any(c[0] == "erase" for c in f.backend.calls))

    def test_changed_plan_prose_cannot_inherit_fresh_grant(self):
        with ordinary_full(self) as f:
            proposal = disclose(f)
            grant(f, proposal)
            fields = erase_fields(f.board, "one-time")
            fields["strategy"] = "A replacement destructive scope after disclosure"
            with self.assertRaisesRegex(Exception, "plan field changed"):
                f.flow.call("target_unlock-plan", fields)
            with self.assertRaises(Exception):
                attempt(f)
            self.assertEqual(f.backend.at_entry, [])

    def test_public_allocation_changes_trusted_affected_range_and_refuses_old_grant(self):
        """A real public deployment changes affected scope, holding live identity fixed."""
        with ordinary_full(self) as f:
            before = disclose(f)
            grant(f, before)
            self.assertEqual(approve(f)["status"], "unlock_plan_approved")
            connection = f.server.connection_manager.connection_for(f.board)
            session = connection.runtime_session.session_id
            metadata = session_metadata(connection.handle)
            policy = f.server._capability_policies.resolve(f.board)
            self.assertEqual(policy.map_snapshot["deployment_policy"], {"kind": "none"})
            self.assertFalse(
                any(
                    row["kind"] == "application_flash"
                    for row in before["disclosure"]["affected_regions"]
                )
            )
            artifact = elf_fixture(f.root / "new-deployment.elf", base=0)
            f.flow.call("flash_application-plan", dict.fromkeys(erase_fields(f.board)))
            plan = f.flow.plan("flash_application", f.board, {"artifact": str(artifact)})
            self.assertEqual(plan["status"], "plan_accepted", plan)
            reply = f.flow.call(
                "flash_application", {"board_id": f.board, "artifact": str(artifact)}
            )
            self.assertIn("matching addressed bytes", reply)
            after_policy = f.server._capability_policies.resolve(f.board)
            self.assertNotEqual(after_policy.policy_digest, policy.policy_digest)
            allocation = after_policy.map_snapshot["partitions"]["application"]
            # The 256-byte image occupies the first complete 4096-byte
            # Nordic sector; allocation/disclosure must include that sector.
            self.assertEqual((allocation["start"], allocation["end"]), (0, 4096))
            current = f.server.connection_manager.connection_for(f.board)
            self.assertEqual(current.runtime_session.session_id, session)
            self.assertEqual(session_metadata(current.handle), metadata)
            # The still-old disclosure/grant must not authorize recovery after
            # this valid committed allocation has changed the affected range.
            with self.assertRaises(Exception):
                attempt(f)
            self.assertEqual(f.backend.at_entry, [])
            after = disclose(f)
            affected = [
                row
                for row in after["disclosure"]["affected_regions"]
                if row["kind"] == "application_flash"
            ]
            self.assertEqual([(row["start"], row["end"]) for row in affected], [(0, 4096)])
            self.assertNotEqual(
                after["manual_grant"]["binding_digest"], before["manual_grant"]["binding_digest"]
            )
            with self.assertRaises(Exception):
                approve(f)
            self.assertEqual(f.backend.at_entry, [])

    def test_restart_relocks_prior_approval_and_supports_fresh_validation_recovery(self):
        with ordinary_full(self) as f:
            proposal = disclose(f)
            grant(f, proposal)
            approve(f)
            first_run = f.server.server_run.run_id
            f.flow.call("disconnect", {"board_id": f.board})
            restarted = fresh_server(f.root)
            with patch.object(restarted, "_hardware_inventory", _setup_inventory()):
                restarted.configure_tiered_test_seams(backend=f.backend)
                f.server = restarted
                f.flow = PublicSetup(restarted)
                self.assertNotEqual(restarted.server_run.run_id, first_run)
                self.assertEqual(restarted._manual_permissions.status("mass-erase").state, "locked")
                self.assertEqual(
                    f.flow.json("get_capabilities", {"board_id": f.board})["tier"], "setup-full"
                )
                with self.assertRaises(Exception):
                    attempt(f)
                connect(f)
                self.assert_read_refused(f)
                self.assertEqual(
                    advertised_validation(f.flow, f.board)["status"], "validation_passed"
                )
                self.assertEqual(safe_value(f), 0x6D143A72)
                fresh = disclose(f)
                self.assertNotEqual(
                    fresh["manual_grant"]["binding_digest"],
                    proposal["manual_grant"]["binding_digest"],
                )
                with self.assertRaisesRegex(Exception, "manual/locked"):
                    approve(f)
                self.assertEqual(f.backend.at_entry, [])
                grant(f, fresh)
                self.assertEqual(approve(f)["status"], "unlock_plan_approved")
                self.assertIn("Target unlock completed", attempt(f))
                self.assertEqual(len(f.backend.at_entry), 1)
                self.assertIsNone(restarted.connection_manager.maybe_connection(f.board))
                self.assert_revoked_then_reusable(f)
                f.flow.call("disconnect", {"board_id": f.board})


if __name__ == "__main__":
    unittest.main()
