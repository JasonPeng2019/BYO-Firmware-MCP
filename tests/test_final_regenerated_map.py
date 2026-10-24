"""M1: real regenerated full map constrains registered peripheral actions.

Only hardware effects are simulated. Existing valid pack/datasheet inputs flow
through actual setup, SVD derivation, publication, guards, plans and handlers.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import unittest

from pyocd_debug_mcp.safety.enforce import SafetyPolicyError
from pyocd_debug_mcp.safety.regions import ActionCategory, AddressRange, Refusal, RegionKind
from tests import test_lite_application_flash as full_fixture


class FinalRegeneratedMap(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("VI01A_FULL_PROJECT"):
            return
        self.fixture = full_fixture.FullGenericAllocationTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def observe(self, label, response, **details):
        destination = os.environ.get("BYO_FINAL_CASE_EVIDENCE")
        if destination:
            root = Path(destination) / self._testMethodName
            root.mkdir(parents=True, exist_ok=True)
            (root / (label + ".json")).write_text(
                json.dumps(
                    {
                        "response": response,
                        **details,
                        "backend_calls": self.fixture.backend.calls,
                        "generated_map": self.fixture.server._active_full_document(
                            self.fixture.board_id
                        ).to_document(),
                    },
                    indent=2,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )

    @full_fixture.fresh_full_project
    def test_regenerated_protective_register_refusals_have_zero_backend_effects(self):
        # Addresses are fixed to the actual STM32L476 pack used by this source
        # fixture. They are never sent to a physical target.
        for name, address in (
            ("KEYR", 0x40022008),
            ("OPTKEYR", 0x4002200C),
            ("CR", 0x40022014),
            ("OPTR", 0x40022020),
            ("WRP1AR", 0x4002202C),
        ):
            with self.subTest(register=name):
                self.fixture.backend.calls.clear()
                response = None
                details = {}
                try:
                    server = self.fixture.server
                    requested = AddressRange.from_start_size(address, 4)
                    document = server._active_full_document(self.fixture.board_id)
                    overlaps = [
                        region
                        for region in document.regions
                        if region.kind is RegionKind.PROHIBITED
                        and region.address_range.overlaps(requested)
                    ]
                    details["prohibited_overlaps"] = [region.to_document() for region in overlaps]
                    self.assertTrue(overlaps, name)
                    decision = document.safety_map.check(
                        ActionCategory.REGISTER_WRITE, (requested,)
                    )
                    details["map_decision"] = asdict(decision)
                    self.assertIsInstance(decision, Refusal)
                    self.assertEqual(decision.offending_range, requested)
                    self.assertIs(decision.classification, RegionKind.PROHIBITED)
                    self.assertEqual(decision.code, "safety/prohibited")
                    self.assertEqual(
                        decision.remedy, "Choose an operation that does not touch prohibited memory."
                    )
                    with self.assertRaises(SafetyPolicyError) as caught:
                        server._check_active_snapshot_range(
                            self.fixture.board_id, ActionCategory.REGISTER_WRITE, requested
                        )
                    refusal = caught.exception
                    details["policy_refusal"] = {
                        "code": refusal.code,
                        "remedy": refusal.remedy,
                        "text": str(refusal),
                    }
                    self.assertEqual(refusal.code, "safety/prohibited")
                    self.assertEqual(refusal.remedy, ("board_safety_refresh",))
                    self.assertEqual(self.fixture.backend.calls, [])
                    response = self.fixture.invoke(
                        "register_write", {"address": address, "mask": 0xFFFFFFFF, "value": 0}
                    )
                    # Public text carries the refusal/remedy, not an internal code field.
                    self.assertIn(str(refusal), response)
                    self.assertEqual(self.fixture.backend.calls, [])
                finally:
                    self.observe(name, response, **details)

    @full_fixture.fresh_full_project
    def test_regenerated_map_keeps_an_ordinary_peripheral_control_usable(self):
        self.fixture.backend.calls.clear()
        response = None
        try:
            response = self.fixture.invoke(
                "register_write", {"address": 0x48000000, "mask": 1, "value": 1}
            )
            self.assertIn("mapped peripheral write allowed", response)
            self.assertTrue(
                any(
                    call[0] == "read_memory" and call[2] == 0x48000000
                    for call in self.fixture.backend.calls
                ),
                self.fixture.backend.calls,
            )
            self.assertTrue(
                any(
                    call[0] == "write_memory" and call[2] == 0x48000000
                    for call in self.fixture.backend.calls
                ),
                self.fixture.backend.calls,
            )
        finally:
            self.observe("ordinary-GPIO-control", response)


if __name__ == "__main__":
    unittest.main()
