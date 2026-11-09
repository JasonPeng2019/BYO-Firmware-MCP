"""VI01A: public lite flash and real full-tier generic allocation contracts."""

from __future__ import annotations
from tests.lite_flash_support import FullGenericAllocationCase
from tests.lite_flash_support import PublicActions, fresh_full_project, observe

import hashlib
import unittest

from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.capabilities.lite import normalize_lite_confirmation
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.capabilities.routing import TierRouter
from pyocd_debug_mcp.safety.enforce import SafetyPolicyError
from pyocd_debug_mcp.safety.map_build import require_reconciled_authority
from tests.lite_flash_support import FLASH, RAM, cortex_elf, region
from tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    public_text,
    tiered_test_server,
)

import pytest


class LiteApplicationFlashTests(PublicActions, unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(isolated_project())
        self.server = self.enterContext(tiered_test_server(self.root))
        self.board_id = "vi01a_lite"
        self.backend = DeterministicTargetBackend()
        self.server.configure_tiered_test_seams(
            backend=self.backend, auto_target_resolver=lambda _: "nrf52840"
        )
        self.handle = self.backend.open(board=None, unique_id="vi01a-lite-probe", target="nrf52840")
        self.server._promote_open_session(self.board_id, self.handle, gate_reason="VI01A simulated")
        self.addCleanup(self.server.disconnect, self.board_id)
        self.artifact = cortex_elf(self.root / "application.elf", FLASH)

    def commit(self, rows, sectors=None):
        confirmation = normalize_lite_confirmation(
            self.board_id,
            {
                "decision": "confirm-or-correct",
                "regions": [region("ram", RAM, RAM + 0x100), *rows],
                "flash": {
                    "backend_target": "nrf52840",
                    "erase_sectors": sectors
                    if sectors is not None
                    else [{"start": FLASH, "end": FLASH + 0x100}],
                },
                "recovery": None,
            },
        )
        committed = CapabilityPolicyRepository(self.root).commit(
            self.board_id,
            Tier.SETUP_LITE,
            map_snapshot=confirmation.map_snapshot,
            evidence=confirmation.evidence,
        )
        repository = CapabilityPolicyRepository(self.root)
        resolved = TierRouter(repository).require_safe("flash_application", self.board_id)
        self.assertEqual(resolved.generation_id, committed.generation_id)
        self.assertEqual(resolved.policy_digest, committed.policy_digest)
        self.assertEqual(resolved.map_snapshot, confirmation.map_snapshot)
        self.server.configure_tiered_test_seams(policy_repository=repository)
        observe(
            self,
            stage="commit-reopen-router",
            generation=resolved.generation_id,
            digest=resolved.policy_digest,
            snapshot=resolved.map_snapshot,
        )
        return resolved

    @pytest.mark.mvp
    def test_public_lite_application_programs_and_binds_artifact_without_full_allocation(self):
        before = self.commit([region("application_flash", FLASH, FLASH + 0x100)])
        self.server._check_flash_safety("flash_application", self.board_id, self.artifact)
        start = len(self.backend.calls)
        response = self.invoke("flash_application", {"artifact": str(self.artifact)})
        self.assertIn("Flashed", response)
        self.assertIn("target left halted", response)
        self.assertEqual(
            self.backend.calls[start:],
            [
                ("flash", "vi01a-lite-probe", self.artifact.resolve(), False),
                ("release_reset", "vi01a-lite-probe"),
            ],
        )
        self.assertEqual(
            self.server._current_symbol_artifacts[self.board_id],
            (self.artifact.resolve(), hashlib.sha256(self.artifact.read_bytes()).hexdigest()),
        )
        after = CapabilityPolicyRepository(self.root).resolve(self.board_id)
        self.assertEqual(after.generation_id, before.generation_id)
        self.assertEqual(after.map_snapshot, before.map_snapshot)
        self.assertNotIn(self.board_id, self.server._pending_generic_allocations)

    def test_public_lite_bootloader_still_programs_and_finalizes(self):
        self.commit([region("bootloader", FLASH, FLASH + 0x100)])
        start = len(self.backend.calls)
        response = self.invoke("flash_bootloader", {"artifact": str(self.artifact)})
        self.assertIn("Flashed", response)
        self.assertEqual(
            self.backend.calls[start:],
            [
                ("flash", "vi01a-lite-probe", self.artifact.resolve(), False),
                ("release_reset", "vi01a-lite-probe"),
            ],
        )

    def test_public_lite_prohibited_role_and_sector_refusals_precede_all_effects(self):
        app = region("application_flash", FLASH, FLASH + 0x100)
        cases = [
            ([app, region("prohibited", FLASH + 7, FLASH + 8)], None, "not fully confirmed"),
            ([app, region("bootloader", FLASH, FLASH + 0x10)], None, "not fully confirmed"),
            ([app, region("bootloader", FLASH + 0x80, FLASH + 0x100)], None, "not fully confirmed"),
            ([app], [{"start": FLASH, "end": FLASH + 8}], "do not fully cover"),
            (
                [region("application_flash", FLASH, FLASH + 0x100, writable=False)],
                None,
                "not fully confirmed",
            ),
        ]
        for rows, sectors, message in cases:
            with self.subTest(rows=rows, sectors=sectors):
                self.commit(rows, sectors)
                before = list(self.backend.calls)
                response = self.invoke("flash_application", {"artifact": str(self.artifact)})
                self.assertIn(message, response)
                self.assertNotIn("No active full safety snapshot", response)
                self.assertEqual(self.backend.calls, before)

    def test_no_setup_is_refused_in_public_route_and_both_allocation_helpers(self):
        repository = CapabilityPolicyRepository(self.root)
        repository.commit(self.board_id, Tier.NO_SETUP)
        self.server.configure_tiered_test_seams(policy_repository=repository)
        before = list(self.backend.calls)
        try:
            response = public_text(
                self.server,
                "flash_application",
                {"board_id": self.board_id, "artifact": str(self.artifact)},
            )
        except ToolError as exc:
            response = str(exc)
        self.assertIn("no safety map", response)
        for call in (
            lambda: self.server._stage_generic_allocation(
                self.board_id, self.artifact, self.handle
            ),
            lambda: self.server._prepare_generic_allocation(
                "flash_application", self.board_id, self.artifact, self.handle
            ),
        ):
            with self.assertRaises(SafetyPolicyError) as caught:
                call()
            self.assertEqual(caught.exception.code, "safety/map-refresh-required")
        self.assertEqual(self.backend.calls, before)


class FullGenericAllocationTests(FullGenericAllocationCase):


    @fresh_full_project
    def test_public_full_allocation_publishes_proof_before_programming(self):
        response = self.invoke("flash_application", {"artifact": str(self.artifact)})
        self.assertIn("Flashed", response)
        self.assertEqual(len(self.backend.policies_at_programming), 1)
        at_programming = self.backend.policies_at_programming[0]
        self.assertNotEqual(at_programming.generation_id, self.state.generation_id)
        self.assertEqual(
            at_programming.map_snapshot["deployment_policy"]["kind"],
            "artifact_application_allocation",
        )
        proof = at_programming.map_snapshot["deployment_policy"]
        self.assertEqual(proof["creation_map_digest"], self.document.canonical_digest)
        self.assertEqual(
            proof["creation_artifact_digest"],
            hashlib.sha256(self.artifact.read_bytes()).hexdigest(),
        )
        active = self.server._active_full_document(self.board_id)
        self.assertEqual(
            self.server._safety_repository.load_current(self.board_id).canonical_digest,
            active.canonical_digest,
        )
        self.assertEqual(
            self.server.gate_manager.snapshot(self.board_id).safety_map.map_digest,
            active.canonical_digest,
        )
        self.assertNotIn(self.board_id, self.server._pending_generic_allocations)

    @fresh_full_project
    def test_full_prepare_requires_the_staged_allocation_proof(self):
        before = list(self.backend.calls)
        with self.assertRaises(SafetyPolicyError) as caught:
            self.server._prepare_generic_allocation(
                "flash_application", self.board_id, self.artifact, self.handle
            )
        self.assertEqual(caught.exception.code, "safety/allocation-proof-missing")
        self.assertEqual(self.backend.calls, before)

    @fresh_full_project
    @pytest.mark.mvp
    def test_full_prepare_rejects_changed_artifact(self):
        self.stage()
        data = self.artifact.read_bytes()
        self.artifact.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        self.assert_stale_prepare()

    @fresh_full_project
    def test_full_prepare_rejects_changed_committed_map(self):
        self.stage()
        document = self.document.to_document()
        document["regions"][0]["name"] += " refreshed"
        self.server._capability_policies.commit(
            self.board_id,
            Tier.SETUP_FULL,
            profile_snapshot=self.state.profile_snapshot,
            map_snapshot=document,
            evidence=self.state.evidence,
            expected_state=self.state,
        )
        require_reconciled_authority(self.server._active_full_document(self.board_id))
        self.assert_stale_prepare()

    @fresh_full_project
    def test_full_prepare_rejects_replaced_connection(self):
        self.stage()
        prior = self.server.connection_manager.clear(self.board_id)
        self.server.connection_manager.assign(
            self.board_id,
            prior.handle,
            prior.runtime_session,
            connection_id="probeid:stlink:replacement",
        )
        self.assert_stale_prepare()


if __name__ == "__main__":
    unittest.main()
