from __future__ import annotations

import unittest

from firmware_mcp.adapters.debug_interface import FlashVerification


class FlashVerificationRecordTests(unittest.TestCase):
    def valid(self) -> dict[str, object]:
        return {
            "firmware_path": "firmware.hex",
            "byte_count": 3,
            "verified_ranges": [[0, 1], [4, 6]],
            "expected_sha256": "A" * 64,
            "observed_sha256": "a" * 64,
            "final_reset_postcondition": "RUNNING",
        }

    def test_normalizes_digest_case(self) -> None:
        result = FlashVerification.from_record(self.valid())
        self.assertEqual(result.expected_sha256, "a" * 64)

    def test_rejects_every_success_record_invariant(self) -> None:
        cases = {
            "empty path": {"firmware_path": ""},
            "bad digest": {"expected_sha256": "x" * 64},
            "digest mismatch": {"observed_sha256": "b" * 64},
            "unknown reset": {"final_reset_postcondition": "unknown"},
            "adjacent ranges": {"verified_ranges": [[0, 1], [1, 3]]},
            "unsorted ranges": {"verified_ranges": [[4, 6], [0, 1]]},
            "wrong byte count": {"byte_count": 2},
        }
        for label, changes in cases.items():
            with self.subTest(label=label):
                record = self.valid()
                record.update(changes)
                with self.assertRaisesRegex(ValueError, "worker flash verification"):
                    FlashVerification.from_record(record)

    def test_uncertain_record_requires_and_retains_real_reset_error(self) -> None:
        record = self.valid()
        record.update(
            {
                "final_reset_postcondition": "unknown",
                "final_reset_error_type": "DistinctResetDrop",
                "final_reset_error_message": "wired reset link disappeared",
            }
        )
        result = FlashVerification.from_record(record, allow_uncertain_final_reset=True)
        self.assertEqual(result.final_reset_error_type, "DistinctResetDrop")
        self.assertEqual(result.final_reset_error_message, "wired reset link disappeared")

        record.pop("final_reset_error_message")
        with self.assertRaisesRegex(ValueError, "worker flash verification"):
            FlashVerification.from_record(record, allow_uncertain_final_reset=True)

    def test_failed_final_reset_is_an_uncertain_verified_write_record(self) -> None:
        record = self.valid()
        record.update(
            {
                "final_reset_postcondition": "failed",
                "final_reset_error_type": "ObservedResetState",
                "final_reset_error_message": (
                    "halt_after_reset=true; observed_state=RUNNING; expected_state=HALTED"
                ),
            }
        )
        result = FlashVerification.from_record(record, allow_uncertain_final_reset=True)
        self.assertEqual(result.final_reset_postcondition, "failed")
        self.assertEqual(result.final_reset_error_type, "ObservedResetState")

        with self.assertRaisesRegex(ValueError, "worker flash verification"):
            FlashVerification.from_record(record)


if __name__ == "__main__":
    unittest.main()
