"""M04 repair checks; the independent B01 regressions remain unchanged."""

from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from pyocd.target import TARGET
from pyocd.target.builtin import BUILTIN_TARGETS

from pyocd_debug_mcp.firmstore.profiles import ProfileError
from pyocd_debug_mcp.setup_flow.device_support import (
    BuiltInTargetCompatibilityError,
    BuiltInTargetSupportCandidate,
    resolve_builtin_target_support,
    resolve_persisted_builtin_target_support,
)
from tests.test_tiered_acceptance_lite import (
    _accepted_setup_plan,
    _batch_payload,
    _child_setup_response,
    _minimal_local_pdf,
    _setup_inventory,
)
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    public_text,
    tiered_test_server,
)


class InstalledPartMetadataTests(unittest.TestCase):
    def test_installed_explicit_part_pattern_and_literal_device_replay(self):
        for part in ("nRF52840-QIAA", "nrf52840", "NRF52840_QIAA"):
            with self.subTest(part=part):
                candidate = resolve_builtin_target_support(part, "nrf52840").with_identity_proof(
                    0x410FC241
                )
                self.assertEqual(
                    resolve_persisted_builtin_target_support(part, candidate.to_authority_document()),
                    candidate,
                )

    def test_other_device_prefix_and_unsupported_package_pattern_require_pack(self):
        for part in ("STM32L476RGT6", "nRF528400-QIAA", "nRF52840-QIAB", "nRF52840-QIAA-R"):
            with self.subTest(part=part), self.assertRaises(BuiltInTargetCompatibilityError):
                resolve_builtin_target_support(part, "nrf52840")

    def test_general_metadata_matching_and_ambiguous_variant_module(self):
        # A synthetic second vendor exercises the same path, with real static
        # geometry as fixture data. No production board/catalog entry is added.
        module = "pyocd.target.builtin.target_Example77_xxAB"
        first = type(
            "FirstVariant",
            (),
            {"__module__": module, "MEMORY_MAP": TARGET["nrf52840"].MEMORY_MAP},
        )
        second = type(
            "SecondVariant", (), {"__module__": module, "MEMORY_MAP": first.MEMORY_MAP}
        )
        entries = {"example_alias": first}
        with patch.dict(TARGET, entries), patch.dict(BUILTIN_TARGETS, entries):
            self.assertEqual(
                resolve_builtin_target_support("Example77-QIAB", "example_alias").pyocd_target,
                "example_alias",
            )
            with self.assertRaises(BuiltInTargetCompatibilityError):
                resolve_builtin_target_support("Example77-QIAA", "example_alias")
            variants = {"other_variant": second, "example77qiab": first}
            with patch.dict(TARGET, variants), patch.dict(BUILTIN_TARGETS, variants):
                # The literal device alias still selects the first class.
                resolve_builtin_target_support("Example77-QIAB", "example_alias")
                # A module shared by distinct classes cannot select this part.
                with self.assertRaises(BuiltInTargetCompatibilityError):
                    resolve_builtin_target_support("Example77-QJAB", "example_alias")

    def test_pre_repair_incompatible_authority_cannot_replay(self):
        compatible = resolve_builtin_target_support("nRF52840-QIAA", "nrf52840")
        # Construct a fully self-consistent authority using the old defective
        # rules, including its hash and recognized shared-core proof.
        old = BuiltInTargetSupportCandidate(
            "STM32L476RGT6", compatible.pyocd_target, compatible.geometry_sha256
        ).with_identity_proof(0x410FC241)
        with self.assertRaises(BuiltInTargetCompatibilityError):
            resolve_persisted_builtin_target_support(old.part_number, old.to_authority_document())


class InstalledChoiceAdmissionTests(unittest.TestCase):
    def fixture(self, part):
        project = isolated_project()
        root = project.__enter__()
        self.addCleanup(project.__exit__, None, None, None)
        context = tiered_test_server(root)
        server = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        backend = DeterministicTargetBackend(read_value=0x410FC241)
        server.configure_tiered_test_seams(backend=backend)
        for dependency in (
            patch.object(server, "_hardware_inventory", _setup_inventory()),
            patch.object(server, "_built_in_target_names", return_value={"nrf52840", "cortex_m"}),
        ):
            dependency.start()
            self.addCleanup(dependency.stop)
        board, _, plan = _accepted_setup_plan(
            self,
            server,
            display_name="M04 repair installed support",
            target_tier="setup-full",
            datasheet=_minimal_local_pdf(root, "official-fixture.pdf"),
            mcu_part_number=part,
        )
        call = plan["stable_client_fallback"]
        pending = _child_setup_response(
            _batch_payload(public_text(server, call["tool_name"], call["arguments"]))
        )
        return server, backend, board, plan, pending

    @staticmethod
    def submit(server, board, pending, response):
        return json.loads(
            public_text(
                server,
                "continue_setup",
                {
                    "board_id": board,
                    "continuation_id": pending["continuation_id"],
                    "response": response,
                },
            )
        )

    def test_incompatible_choice_omitted_and_explicit_submission_returns_pack_before_io(self):
        server, backend, board, _, pending = self.fixture("STM32L476RGT6")
        form = pending["accepted_response"]
        self.assertNotIn("alternatives", form)
        self.assertEqual(server._installed_setup_target_candidates("STM32L476RGT6"), ())
        self.assertEqual(backend.calls, [])
        # Positive prose and valid attachment options cannot confer part support.
        response = {
            "pyocd_target": "nrf52840",
            "evidence": [{"source": "official fixture", "claim": "Supports this exact part."}],
            "reasoning_summary": "The installed target supports the supplied part.",
            "debug_protocol": "swd",
            "debug_connect_mode": "attach",
            "debug_clock_hz": 1_000_000,
        }
        refused = self.submit(server, board, pending, response)
        self.assertEqual(refused["status"], "setup_research_required", refused)
        self.assertEqual(set(refused["exact_response_fields"]), set(form["response"]))
        self.assertIn("official", refused["agent_prompt"])
        self.assertEqual(
            refused["rejected_candidates"][0]["observed"], {"research_route": "official_pack"}
        )
        self.assertEqual(backend.calls, [])
        self.assertNotIn(board, server._setup_builtin_candidates)
        self.assertNotIn(board, server._setup_target_overrides)
        self.assertNotIn(board, server._setup_attachment_overrides)
        with self.assertRaises(ProfileError):
            server._profile_repository.load(board)

    def test_compatible_returned_choice_reaches_public_profile_persistence_and_replay(self):
        server, backend, board, plan, pending = self.fixture("nRF52840-QIAA")
        alternative = pending["accepted_response"]["alternatives"][0]
        self.assertEqual(alternative["eligible_targets"], ["nrf52840"])
        response = copy.deepcopy(alternative["response"])
        response.update(
            pyocd_target=alternative["eligible_targets"][0],
            evidence=[{"source": "official fixture", "claim": "Supports supplied part."}],
            reasoning_summary="Installed explicit device pattern supports the supplied ordering code.",
        )
        accepted = self.submit(server, board, pending, response)
        self.assertEqual(accepted["accepted"], "target", accepted)
        self.assertEqual([row[0] for row in backend.calls], ["open", "read_memory", "close"])
        repair = plan["paired_action_fallbacks"][0]["call"]
        result = _child_setup_response(
            _batch_payload(public_text(server, repair["tool_name"], repair["arguments"]))
        )
        profile = server._profile_repository.load(board)
        self.assertEqual(profile.mcu_part_number, "nRF52840-QIAA", result)
        self.assertEqual(profile.board.pyocd_target, "nrf52840")
        persisted = profile.device_support
        self.assertIsNotNone(persisted)
        replayed = server._replay_profile_device_support(profile)
        self.assertEqual(replayed.to_authority_document(), persisted)
        self.assertEqual(replayed.part_number, "nRF52840-QIAA")
        self.assertTrue(any(row[0] == "read_memory" and row[2] == 0xE000ED00 for row in backend.calls))
        self.assertFalse(any(row[0] in {"program", "mass_erase"} for row in backend.calls))


if __name__ == "__main__":
    unittest.main()
