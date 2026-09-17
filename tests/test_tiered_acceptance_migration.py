"""AT01: legacy migration must choose raw only for positive initial remnants."""

from __future__ import annotations

import copy
import unittest

from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.safety.map_build import SafetyMapDocument

from tests.tiered_acceptance_support import (
    fresh_server,
    fixture,
    isolated_project,
    public_call,
    seed_legacy_state,
    write_json,
)


class TieredMigrationAcceptanceTests(unittest.TestCase):
    """Exercise persisted input states independently of the production writers."""

    def test_recognized_initial_remnant_is_persisted_as_incomplete_no_setup_across_restart(
        self,
    ) -> None:
        """AT01: a legacy profile without a completed map is the one safe raw migration."""

        with isolated_project() as root:
            expected = seed_legacy_state(root, "recognized_initial_remnant")["expected"]
            first = CapabilityPolicyRepository(root).resolve("legacy_board")
            restarted = CapabilityPolicyRepository(root).resolve("legacy_board")

            for state in (first, restarted):
                self.assertEqual(state.tier.value if state.tier else None, expected["tier"])
                self.assertEqual(state.status, expected["policy_status"])
                self.assertEqual(state.setup_incomplete, expected["setup_incomplete"])
                self.assertEqual(state.hardware_allowed, expected["hardware_allowed"])
            self.assertTrue(
                (root / ".firm" / "capabilities" / "legacy_board" / "current.json").is_file()
            )

    def test_missing_or_corrupt_committed_map_never_opens_a_raw_window(self) -> None:
        """AT01/C09: a historical safety reference makes a missing map corruption, not setup."""

        with isolated_project() as root:
            expected = seed_legacy_state(root, "missing_committed_map")["expected"]
            state = CapabilityPolicyRepository(root).resolve("legacy_board")
            self.assertEqual(state.tier, None)
            self.assertEqual(state.status, expected["policy_status"])
            self.assertFalse(state.hardware_allowed)

            # A restart must make the same fail-closed classification rather
            # than treating the failed first migration as a new project.
            restarted = CapabilityPolicyRepository(root).resolve("legacy_board")
            self.assertEqual(restarted.tier, None)
            self.assertEqual(restarted.status, "corrupt")

    def test_malformed_committed_map_is_not_reclassified_as_initial_incompleteness(self) -> None:
        """AT01: unreadable protected-map bytes fail closed just like a missing committed map."""

        with isolated_project() as root:
            expected = seed_legacy_state(root, "corrupt_committed_map")["expected"]
            map_path = root / ".firm" / "safety" / "legacy_board" / "memory_map.yaml"
            map_path.parent.mkdir(parents=True)
            map_path.write_text("{this is not a complete YAML mapping", encoding="utf-8")

            state = CapabilityPolicyRepository(root).resolve("legacy_board")
            self.assertIsNone(state.tier)
            self.assertEqual(state.status, expected["policy_status"])
            self.assertFalse(state.hardware_allowed)
            self.assertEqual(
                CapabilityPolicyRepository(root).resolve("legacy_board").status, "corrupt"
            )

    def test_ambiguous_legacy_provenance_denies_hardware_after_restart(self) -> None:
        """AT01: contradictory legacy artifacts cannot be guessed into no-setup."""

        with isolated_project() as root:
            expected = seed_legacy_state(root, "ambiguous_provenance")["expected"]
            state = CapabilityPolicyRepository(root).resolve("legacy_board")
            self.assertIsNone(state.tier)
            self.assertEqual(state.status, expected["policy_status"])
            self.assertFalse(state.hardware_allowed)

            # The public status route is the caller-visible proof that this
            # state is corrupt, contains no hardware capability, and supplies a
            # repair remedy.  It does not require a target connection.
            server = fresh_server(root)
            payload = public_call(server, "get_capabilities", {"board_id": "legacy_board"})
            self.assertEqual(payload["status"], "capability_status")
            self.assertIsNone(payload["tier"])
            self.assertEqual(payload["policy_status"], "corrupt")
            self.assertEqual(payload["capabilities"], [])
            self.assertTrue(payload["remedies"])

    def test_valid_legacy_profile_and_map_migrate_to_full_and_preserve_provenance_on_restart(
        self,
    ) -> None:
        """AT01/C02: independently-recorded, schema-v2 authority stays full after restart."""

        with isolated_project() as root:
            board = "legacy_full"
            baseline = fixture("legacy_full_v2.json")
            profile = baseline["profile"]
            memory_map = baseline["memory_map"]
            self.assertIsInstance(profile, dict)
            self.assertIsInstance(memory_map, dict)
            # This fixture is checked independently before it ever reaches the
            # migration code.  It is static known-good legacy bytes, not a
            # current profile/map writer output that could mask a bad migrator.
            document = SafetyMapDocument.from_document(memory_map)
            self.assertEqual(document.board_id, board)
            self.assertEqual(document.canonical_digest, document.canonical_digest)
            write_json(
                root / ".firm" / "boards" / f"{board}.json",
                profile,
            )
            write_json(
                root / ".firm" / "safety" / board / "memory_map.yaml",
                memory_map,
            )

            migrated = CapabilityPolicyRepository(root).resolve(board)
            self.assertEqual(migrated.tier, Tier.SETUP_FULL)
            self.assertEqual(migrated.status, "legacy-migrated")
            self.assertFalse(migrated.setup_incomplete)

            # Migration provenance is client-visible policy state.  It must
            # not silently become a generic committed state merely because the
            # process restarted and reloaded its immutable generation.
            restarted = CapabilityPolicyRepository(root).resolve(board)
            self.assertEqual(restarted.tier, Tier.SETUP_FULL)
            self.assertEqual(restarted.status, "legacy-migrated")

    def test_semantically_invalid_legacy_map_or_source_reference_never_promotes_to_full(
        self,
    ) -> None:
        """AT01: valid JSON/YAML alone is not enough evidence for full authority."""

        baseline = fixture("legacy_full_v2.json")
        profile = baseline["profile"]
        memory_map = baseline["memory_map"]
        self.assertIsInstance(profile, dict)
        self.assertIsInstance(memory_map, dict)
        invalid_cases = {
            "unsupported_schema": (
                profile,
                {**memory_map, "schema_version": 999},
            ),
            "profile_digest_mismatch": (
                profile,
                {
                    **memory_map,
                    "source_digests": {
                        **memory_map["source_digests"],  # type: ignore[index]
                        "semantic_profile": "0" * 64,
                    },
                },
            ),
            "identity_mismatch": (
                profile,
                {
                    **memory_map,
                    "identity": {**memory_map["identity"], "pyocd_target": "wrong-target"},  # type: ignore[index]
                },
            ),
            "source_reference_mismatch": (
                {**profile, "safety_ref": ".firm/safety/other_board/memory_map.yaml"},
                memory_map,
            ),
            "non_authoritative_region": (
                profile,
                {
                    **memory_map,
                    "regions": [
                        {
                            **memory_map["regions"][0],  # type: ignore[index]
                            "provenance": [
                                {
                                    "authority": "build",
                                    "source_id": "ephemeral-artifact",
                                    "detail": "must not become durable safety authority",
                                }
                            ],
                        },
                        memory_map["regions"][1],  # type: ignore[index]
                    ],
                },
            ),
        }
        for name, (case_profile, case_map) in invalid_cases.items():
            with self.subTest(name=name), isolated_project() as root:
                board = "legacy_full"
                write_json(root / ".firm" / "boards" / f"{board}.json", copy.deepcopy(case_profile))
                write_json(
                    root / ".firm" / "safety" / board / "memory_map.yaml",
                    copy.deepcopy(case_map),
                )
                state = CapabilityPolicyRepository(root).resolve(board)
                self.assertIsNone(state.tier, state)
                self.assertEqual(state.status, "corrupt", state)
                self.assertFalse(state.hardware_allowed, state)
                self.assertEqual(CapabilityPolicyRepository(root).resolve(board).status, "corrupt")


if __name__ == "__main__":
    unittest.main()
