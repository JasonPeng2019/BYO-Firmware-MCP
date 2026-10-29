"""Controlled fingerprint and genuine old-producer migration are separate.

Only target I/O is simulated by the existing registered-pack fixture. These
controls must run in the frozen complete Actions pass, never on the coordinator.
The historical schema below is from a7982ab4645020f0e85ff8a682a30e24ff7cc3be,
safety/map_build.py, and differs from policy 2 in exactly the policy discriminator.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.safety.enforce import SafetyPolicyError
from pyocd_debug_mcp.safety.map_build import (
    GenericSafetyMapDocument,
    SafetyMapError,
    SafetyMapRepository,
)
from tests.test_generic_builtin_safety_map import nrf_registered_pack_fixture, observe_nrf_control
from tests.tiered_acceptance_support import public_text


POLICY_ONE_SCHEMA = {
    "schema_version": 3,
    "map_schema_version": 3,
    "range_semantics": "unsigned-64-bit-half-open",
    "partition_executable_authority": "per-operation-elf-only",
    "prohibited_precedence": True,
    "authority_kinds": ["resolved_builtin_target", "resolved_pack"],
    "nullable_partitions": True,
    "artifact_application_allocation": 1,
    "cmsis_svd_access_default": "read-write",
    "svd_prohibited_register_policy": 1,
}


def generator_digest(schema):
    canonical = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(b"firm-safety-v2:map-generator-schema\0" + canonical.encode()).hexdigest()


class FinalPolicyFingerprintMigration(unittest.TestCase):
    def setUp(self):
        self.fixture = nrf_registered_pack_fixture(self)

    def previous_document(self):
        current = self.fixture.document
        expected = generator_digest({**POLICY_ONE_SCHEMA, "svd_prohibited_register_policy": 2})
        self.assertEqual(current.source_digests.map_generator_schema, expected)
        previous_digest = generator_digest(POLICY_ONE_SCHEMA)
        self.assertNotEqual(previous_digest, expected)
        return replace(
            current,
            source_digests=replace(current.source_digests, map_generator_schema=previous_digest),
        )

    def test_previous_fingerprint_round_trip_refuses_and_current_authority_reopens(self):
        fixture = self.fixture
        repository = SafetyMapRepository(fixture.server._profile_repository.store)
        previous = self.previous_document()
        fixture.backend.calls.clear()
        try:
            repository.commit(fixture.board_id, previous)
            reopened = SafetyMapRepository(repository.store).load_current(fixture.board_id)
            self.assertEqual(reopened.to_document(), previous.to_document())
            with self.assertRaisesRegex(SafetyMapError, "board_safety_refresh") as caught:
                fixture.server._require_map_authority(reopened)
            self.assertIn("unsupported generator", str(caught.exception))
            self.assertEqual(fixture.backend.calls, [])
            repository.commit(fixture.board_id, fixture.document)
            current = SafetyMapRepository(repository.store).load_current(fixture.board_id)
            fixture.server._require_map_authority(current)
            self.assertEqual(current.to_document(), fixture.document.to_document())
            self.assertEqual(fixture.backend.calls, [])
        finally:
            observe_nrf_control(
                self,
                fixture,
                "policy-one-reader-and-policy-two-control",
                previous_map=previous.to_document(),
                current_map=fixture.document.to_document(),
            )

    def test_public_refresh_migrates_a_consistent_previous_policy_generation(self):
        fixture = self.fixture
        server = fixture.server
        previous = self.previous_document()
        state = server._capability_policies.resolve(fixture.board_id)
        server._capability_policies.commit(
            fixture.board_id,
            Tier.SETUP_FULL,
            profile_snapshot=state.profile_snapshot,
            map_snapshot=previous.to_document(),
            evidence=state.evidence,
            expected_state=state,
        )
        server._safety_repository.commit(fixture.board_id, previous)
        reloaded = CapabilityPolicyRepository(fixture.root).resolve(fixture.board_id)
        self.assertIs(reloaded.tier, Tier.SETUP_FULL)
        self.assertEqual(reloaded.map_snapshot, previous.to_document())
        fixture.backend.calls.clear()
        response = None
        try:
            with self.assertRaisesRegex(SafetyPolicyError, "unsupported generator"):
                server._active_full_document(fixture.board_id)
            response = json.loads(
                public_text(server, "board_safety_refresh", {"board_id": fixture.board_id})
            )
            self.assertEqual(response["status"], "safety_refresh_completed", response)
            current = server._active_full_document(fixture.board_id)
            self.assertEqual(
                current.source_digests.map_generator_schema,
                fixture.document.source_digests.map_generator_schema,
            )
            server._require_map_authority(current)
            self.assertEqual(fixture.backend.calls, [])
        finally:
            observe_nrf_control(
                self,
                fixture,
                "public-policy-one-to-two-refresh",
                previous_map=previous.to_document(),
                public_response=response,
                reloaded_generation=reloaded.generation_id,
            )

    def test_genuine_original_producer_map_refreshes_separately(self):
        fixture = self.fixture
        server = fixture.server
        state = server._capability_policies.resolve(fixture.board_id)
        profile = fixture.root / "old-producer-profile.json"
        profile.write_text(json.dumps(state.profile_snapshot), encoding="utf-8")
        output = fixture.root / "old-producer"
        command = [
            sys.executable,
            "-B",
            str(Path(__file__).with_name("policy_one_producer.py")),
            "--project",
            str(fixture.root),
            "--profile",
            str(profile),
            "--output",
            str(output),
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", timeout=180, check=False
        )
        produced = None
        response = None
        try:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            produced = json.loads((output / "produced.json").read_text(encoding="utf-8"))
            self.assertEqual(produced["native_source"], "a7982ab4645020f0e85ff8a682a30e24ff7cc3be")
            self.assertTrue(produced["imports"])
            previous = GenericSafetyMapDocument.from_document(produced["document"])
            self.assertEqual(
                previous.source_digests.map_generator_schema, generator_digest(POLICY_ONE_SCHEMA)
            )
            self.assertEqual(previous.authority_source, fixture.document.authority_source)
            self.assertNotEqual(previous.to_document(), self.previous_document().to_document())
            server._capability_policies.commit(
                fixture.board_id,
                Tier.SETUP_FULL,
                profile_snapshot=state.profile_snapshot,
                map_snapshot=previous.to_document(),
                evidence=state.evidence,
                expected_state=state,
            )
            server._safety_repository.commit(fixture.board_id, previous)
            fixture.backend.calls.clear()
            with self.assertRaisesRegex(SafetyMapError, "unsupported generator"):
                server._require_map_authority(previous)
            with self.assertRaisesRegex(SafetyPolicyError, "unsupported generator"):
                server._active_full_document(fixture.board_id)
            response = json.loads(
                public_text(server, "board_safety_refresh", {"board_id": fixture.board_id})
            )
            self.assertEqual(response["status"], "safety_refresh_completed", response)
            current = server._active_full_document(fixture.board_id)
            server._require_map_authority(current)
            self.assertEqual(current.to_document(), fixture.document.to_document())
            self.assertEqual(fixture.backend.calls, [])
        finally:
            observe_nrf_control(
                self,
                fixture,
                "genuine-original-producer-migration",
                producer_command=command,
                producer_exit=result.returncode,
                producer_stdout=result.stdout,
                producer_stderr=result.stderr,
                produced=produced,
                public_response=response,
            )


if __name__ == "__main__":
    unittest.main()
