from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from firmware_mcp.board_config import BoardConfig
from firmware_mcp.firmstore.profiles import ProfileError, _verify_registered_device_support
from firmware_mcp.pack_provision import LiveIdentityProof


class ProfileIdentityReplayTests(unittest.TestCase):
    expected_support = {
        "kind": "resolved_builtin_target",
        "pyocd_target": "test-target",
        "support_id": "replayed-support",
    }

    def _board(self, **changes: object) -> BoardConfig:
        values: dict[str, object] = {
            "board_id": "board",
            "display_name": "Board",
            "mcu_family": "test",
            "probe_family": "test",
            "target": "test-target",
            "probe_type": "test",
            "probe_hint_terms": (),
            "serial_hint_terms": (),
            "test_addr": None,
            "silicon_id_addr": 0x4000,
            "silicon_id_expected": 0x1234,
            "silicon_id_mask": 0xFFFF,
            "silicon_id_width_bits": 32,
            "silicon_id_label": "DEVICEID",
            "silicon_id_capability": "exact",
            "silicon_id_provenance": "datasheet: section 12 says unique to TEST-123",
            "silicon_id_bound_part_number": "TEST-123",
            "silicon_id_support_identity": "replayed-support",
        }
        values.update(changes)
        return BoardConfig(**values)  # type: ignore[arg-type]

    def _verify(self, board: BoardConfig, proof: LiveIdentityProof | None) -> BoardConfig:
        candidate = SimpleNamespace(
            identity_proof=proof,
            to_authority_document=lambda: dict(self.expected_support),
        )
        with patch(
            "firmware_mcp.setup_flow.device_support.resolve_persisted_builtin_target_support",
            return_value=candidate,
        ):
            return _verify_registered_device_support("TEST-123", board, self.expected_support)

    def test_independent_datasheet_exact_evidence_is_not_part_authority(self) -> None:
        with self.assertRaisesRegex(ProfileError, "replayed exact support proof"):
            self._verify(self._board(), proof=None)

    def test_compatible_candidate_cannot_promote_caller_evidence_to_exact(self) -> None:
        compatible = LiveIdentityProof(
            "compatible", 0xE000ED00, 0x410FC241, 0xFFF0FFF0, 32, "CPUID"
        )

        with self.assertRaisesRegex(ProfileError, "replayed exact support proof"):
            self._verify(self._board(), proof=compatible)

    def test_independent_exact_evidence_rejects_support_part_or_provenance_mismatch(self) -> None:
        cases = {
            "support": {"silicon_id_support_identity": "caller-string"},
            "part": {"silicon_id_bound_part_number": "OTHER-999"},
            "provenance": {"silicon_id_provenance": ""},
        }
        for label, changes in cases.items():
            with self.subTest(label=label), self.assertRaises(ProfileError):
                self._verify(self._board(**changes), proof=None)

    def test_embedded_exact_proof_requires_complete_equality(self) -> None:
        proof = LiveIdentityProof(
            "exact",
            0x4000,
            0x5678,
            0xFFFF,
            32,
            "DEVICEID",
            "datasheet: section 12 says unique to TEST-123",
            "TEST-123",
            "replayed-support",
        )

        with self.assertRaisesRegex(ProfileError, "does not match"):
            self._verify(self._board(), proof=proof)

    def test_exact_candidate_proof_cannot_be_downgraded_to_compatible_profile_evidence(
        self,
    ) -> None:
        proof = LiveIdentityProof(
            "exact",
            0x4000,
            0x1234,
            0xFFFF,
            32,
            "DEVICEID",
            "datasheet: section 12 says unique to TEST-123",
            "TEST-123",
            "replayed-support",
        )

        with self.assertRaisesRegex(ProfileError, "compatible identity evidence"):
            self._verify(
                self._board(
                    silicon_id_capability="compatible",
                    silicon_id_provenance="",
                    silicon_id_bound_part_number="",
                    silicon_id_support_identity="",
                ),
                proof=proof,
            )


if __name__ == "__main__":
    unittest.main()
