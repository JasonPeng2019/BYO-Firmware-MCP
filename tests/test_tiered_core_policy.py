"""Focused tier-policy persistence and migration contracts."""

from __future__ import annotations

import tempfile
import unittest
import shutil
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp.capabilities.lite import normalize_lite_confirmation
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.safety.map_build import GenericSafetyMapDocument
from pyocd_debug_mcp.setup_flow.preflight import PreflightDecision, ProbeCandidate, SetupUserInput
from pyocd_debug_mcp.setup_flow.setup import SetupPhaseContext
from pyocd_debug_mcp.setup_flow.validate import (
    ValidationBackend,
    ValidationInventory,
    ValidationProbe,
)


class CapabilityPolicyRepositoryTests(unittest.TestCase):
    @staticmethod
    def _lite_map(board_id: str) -> dict[str, object]:
        return normalize_lite_confirmation(
            board_id,
            {
                "decision": "confirm-or-correct",
                "regions": [
                    {
                        "name": "RAM",
                        "kind": "ram",
                        "start": 0x20000000,
                        "end": 0x20001000,
                        "readable": True,
                        "writable": True,
                        "executable": False,
                        "source_pages": [1],
                        "source_note": "table",
                    }
                ],
                "flash": {"backend_target": None, "erase_sectors": []},
                "recovery": None,
            },
        ).map_snapshot

    def test_fresh_generic_full_setup_commits_candidate_and_live_gate_together(self) -> None:
        """A fresh generic full setup validates staged authority before publishing it."""

        from tests.tiered_acceptance_support import DeterministicTargetBackend, tiered_test_server

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_root = (
                Path(__file__).resolve().parents[1]
                / "fallback-support-evidence"
                / "results"
                / "hil-artifacts"
                / ".firm"
            )
            shutil.copytree(fixture_root / "packs", root / ".firm" / "packs")
            source_datasheet = next((fixture_root / "evidence" / "datasheets").glob("*.pdf"))
            datasheet = root / "STM32L476RG.pdf"
            shutil.copy2(source_datasheet, datasheet)
            with tiered_test_server(root) as server:
                board_id = "fresh_generic"
                target = "stm32l476rgtx"
                probe = ProbeCandidate("probe-1", "ST-Link", "stlink", "serial-1")
                connection_id = server.probe_connection_id("stlink", "serial-1")
                backend = DeterministicTargetBackend(
                    # Arm Cortex-M4 CPUID: enough for the generic compatible
                    # proof, without any physical target access.
                    read_value=0x410FC240,
                    supported_targets=frozenset({target}),
                )
                server.configure_tiered_test_seams(backend=backend)
                server._setup_tiers[board_id] = Tier.SETUP_FULL
                server.assignment_store.assign(connection_id, board_id)
                user_input = SetupUserInput(
                    board_id,
                    connection_id,
                    "Fresh generic STM32L4",
                    "STM32L476RGT6",
                    None,
                    datasheet_path=str(datasheet),
                    requires_uart=False,
                )
                context = SetupPhaseContext(
                    "continuation",
                    "attempt",
                    "setup",
                    user_input,
                    PreflightDecision(
                        "preflight_ready",
                        "setup/preflight-ready",
                        "ready",
                        selected_probe=probe,
                        selected_target=target,
                    ),
                    {},
                )

                with patch.object(
                    server._board_validator,
                    "_backend",
                    ValidationBackend(
                        inventory=lambda: ValidationInventory(
                            probes=(ValidationProbe("probe-1", "ST-Link", "stlink", "serial-1"),)
                        ),
                        target_supported=server._validation_target_supported,
                        connect=server._validation_connect,
                        read_memory=server._validation_read,
                        capture_serial=server._validation_capture,
                        close=server._validation_close,
                    ),
                ):
                    self.assertTrue(server._setup_connection_phase(context).verified)
                    # The early hardware pass intentionally has no active map yet.
                    validation = server._setup_validation_phase(context)
                    self.assertTrue(validation.verified, validation.agent_prompt)
                    self.assertTrue(server._setup_safety_research_phase(context).verified)
                    safety_map = server._setup_safety_map_phase(context)
                    self.assertTrue(
                        safety_map.verified, f"{safety_map.agent_prompt}: {safety_map.details}"
                    )
                    self.assertEqual(
                        server._capability_policies.resolve(board_id).tier, Tier.NO_SETUP
                    )

                    outcome = server._setup_commit_phase(context)
                state = server._capability_policies.resolve(board_id)
                stamp = server.gate_manager.snapshot(board_id)

                self.assertTrue(outcome.verified, outcome.agent_prompt)
                self.assertEqual(state.tier, Tier.SETUP_FULL)
                self.assertIsInstance(state.map_snapshot, dict)
                document = GenericSafetyMapDocument.from_document(state.map_snapshot)
                self.assertIsNotNone(stamp)
                assert stamp is not None
                self.assertEqual(stamp.safety_map.map_digest, document.canonical_digest)
                self.assertEqual(stamp.live_identity.identity_capability, "compatible")

    def test_fresh_named_boards_commit_explicit_no_setup_policy_per_board(self) -> None:
        """A fresh logical board gets its own persisted raw policy, not inferred state."""

        with tempfile.TemporaryDirectory() as temporary:
            repository = CapabilityPolicyRepository(Path(temporary))

            left = repository.resolve("left_controller")
            right = repository.resolve("right_controller")

            self.assertEqual(left.tier, Tier.NO_SETUP)
            self.assertEqual(right.tier, Tier.NO_SETUP)
            self.assertEqual(left.status, "committed")
            self.assertNotEqual(left.policy_digest, right.policy_digest)
            self.assertTrue(
                (
                    Path(temporary) / ".firm" / "capabilities" / "left_controller" / "current.json"
                ).is_file()
            )

    def test_legacy_hyphenated_board_ids_are_safe_policy_path_components(self) -> None:
        """Legacy logical IDs retain their raw-policy path without permitting traversal."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = CapabilityPolicyRepository(root)

            state = repository.resolve("profile-board")

            self.assertEqual(state.board_id, "profile-board")
            self.assertTrue(
                (root / ".firm" / "capabilities" / "profile-board" / "current.json").is_file()
            )
            for unsafe in (
                "",
                " profile-board",
                "profile-board ",
                "../escape",
                "nested/board",
                "a" * 65,
            ):
                with self.subTest(board_id=unsafe):
                    with self.assertRaisesRegex(RuntimeError, "board_id"):
                        repository.resolve(unsafe)

    def test_legacy_missing_committed_map_fails_closed_but_initial_remnant_is_raw(self) -> None:
        """Only a positively recognized incomplete setup can become raw during migration."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            boards = root / ".firm" / "boards"
            boards.mkdir(parents=True)
            (boards / "incomplete.yaml").write_text("board_id: incomplete\n", encoding="utf-8")
            (boards / "damaged.yaml").write_text(
                "board_id: damaged\nsafety_ref: .firm/safety/damaged/memory_map.yaml\n",
                encoding="utf-8",
            )
            repository = CapabilityPolicyRepository(root)

            incomplete = repository.resolve("incomplete")
            damaged = repository.resolve("damaged")

            self.assertEqual(incomplete.tier, Tier.NO_SETUP)
            self.assertEqual(incomplete.status, "setup-incomplete")
            self.assertTrue(incomplete.setup_incomplete)
            self.assertIsNone(damaged.tier)
            self.assertEqual(damaged.status, "corrupt")

    def test_parseable_unrecognized_lone_profiles_do_not_become_raw(self) -> None:
        """Only an exact known initial remnant is a no-setup migration input."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            boards = root / ".firm" / "boards"
            boards.mkdir(parents=True)
            (boards / "unknown.yaml").write_text(
                "board_id: unknown\noperator_note: arbitrary\n", encoding="utf-8"
            )
            (boards / "contradictory.yaml").write_text(
                "board_id: somebody_else\n", encoding="utf-8"
            )
            repository = CapabilityPolicyRepository(root)

            self.assertEqual(repository.resolve("unknown").status, "corrupt")
            self.assertEqual(repository.resolve("contradictory").status, "corrupt")

    def test_pre_pointer_failure_retains_previous_complete_generation(self) -> None:
        """A failed replacement cannot produce a raw window or mixed protected state."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = CapabilityPolicyRepository(root)
            original = repository.commit(
                "controller",
                Tier.SETUP_LITE,
                map_snapshot=self._lite_map("controller"),
            )

            def fail_before_pointer(name: str) -> None:
                if name == "before_pointer_replace":
                    raise RuntimeError("simulated persistence interruption")

            failing = CapabilityPolicyRepository(root, fault_hook=fail_before_pointer)
            with self.assertRaisesRegex(RuntimeError, "simulated persistence interruption"):
                failing.commit("controller", Tier.NO_SETUP)

            restored = CapabilityPolicyRepository(root).resolve("controller")
            self.assertEqual(restored.tier, Tier.SETUP_LITE)
            self.assertEqual(restored.policy_digest, original.policy_digest)

    def test_lite_requires_one_confirmed_map_dependent_capability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = CapabilityPolicyRepository(Path(temporary))

            with self.assertRaisesRegex(RuntimeError, "invalid|map-dependent"):
                repository.commit("controller", Tier.SETUP_LITE, map_snapshot={"regions": []})

            unusable = self._lite_map("controller")
            unusable["regions"][0].update(
                {"readable": False, "writable": False, "executable": False}
            )  # type: ignore[index]
            with self.assertRaisesRegex(RuntimeError, "map-dependent"):
                repository.commit("controller", Tier.SETUP_LITE, map_snapshot=unusable)

    def test_downgrade_and_later_escalation_preserve_inactive_evidence_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = CapabilityPolicyRepository(Path(temporary))
            from tests.tiered_acceptance_support import known_good_full_policy

            profile, full_map = known_good_full_policy()
            full = repository.commit(
                "legacy_full", Tier.SETUP_FULL, profile_snapshot=profile, map_snapshot=full_map
            )
            downgraded = repository.downgrade_to_no_setup("legacy_full")
            self.assertIn(full.generation_id, downgraded.retained_generations)

            escalated = repository.commit(
                "legacy_full",
                Tier.SETUP_LITE,
                map_snapshot=self._lite_map("legacy_full"),
            )
            restarted = CapabilityPolicyRepository(Path(temporary)).resolve("legacy_full")
            self.assertIn(full.generation_id, escalated.retained_generations)
            self.assertEqual(restarted.retained_generations, escalated.retained_generations)

    def test_failed_setup_attempt_marks_active_policy_incomplete_without_replacing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = CapabilityPolicyRepository(Path(temporary))
            raw = repository.resolve("controller")
            marked = repository.mark_setup_incomplete("controller")
            restarted = CapabilityPolicyRepository(Path(temporary)).resolve("controller")
            self.assertEqual(marked.tier, Tier.NO_SETUP)
            self.assertNotEqual(marked.generation_id, raw.generation_id)
            self.assertTrue(marked.setup_incomplete)
            self.assertEqual(marked.status, "setup-incomplete")
            self.assertTrue(restarted.setup_incomplete)

    def test_committed_snapshots_are_detached_from_caller_and_resolve_mutation(self) -> None:
        """An in-flight operation cannot gain authority through a shared dict reference."""

        with tempfile.TemporaryDirectory() as temporary:
            repository = CapabilityPolicyRepository(Path(temporary))
            supplied = self._lite_map("controller")
            committed = repository.commit("controller", Tier.SETUP_LITE, map_snapshot=supplied)
            supplied["regions"][0]["writable"] = False  # type: ignore[index]
            self.assertTrue(committed.map_snapshot["regions"][0]["writable"])  # type: ignore[index]

            resolved = repository.resolve("controller")
            resolved.map_snapshot["regions"][0]["writable"] = False  # type: ignore[index]
            self.assertTrue(repository.resolve("controller").map_snapshot["regions"][0]["writable"])  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
