"""M1: real regenerated full map constrains registered peripheral actions.

Only hardware effects are simulated. Existing valid pack/datasheet inputs flow
through actual setup, SVD derivation, publication, guards, plans and handlers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest

from tests import test_lite_application_flash as full_fixture


class FinalRegeneratedMap(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("VI01A_FULL_PROJECT"):
            return
        self.fixture = full_fixture.FullGenericAllocationTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def observe(self, label, response):
        destination = os.environ.get("BYO_FINAL_CASE_EVIDENCE")
        if destination:
            root = Path(destination) / self._testMethodName
            root.mkdir(parents=True, exist_ok=True)
            (root / (label + ".json")).write_text(
                json.dumps(
                    {
                        "response": response,
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
                try:
                    response = self.fixture.invoke(
                        "register_write", {"address": address, "mask": 0xFFFFFFFF, "value": 0}
                    )
                    self.assertIn("safety/prohibited", response)
                    self.assertEqual(self.fixture.backend.calls, [])
                finally:
                    self.observe(name, response)

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
                    call[0] == "write_memory" and call[2] == 0x48000000
                    for call in self.fixture.backend.calls
                ),
                self.fixture.backend.calls,
            )
        finally:
            self.observe("ordinary-GPIO-control", response)


if __name__ == "__main__":
    unittest.main()
