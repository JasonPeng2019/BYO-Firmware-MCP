"""Focused setup-lite confirmation schema contracts."""

from __future__ import annotations

import unittest
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from pyocd_debug_mcp.capabilities.lite import (
    LiteConfirmationError,
    conservative_lite_proposal,
    normalize_lite_confirmation,
)


class LiteConfirmationTests(unittest.TestCase):
    def test_confirmation_normalizes_geometry_and_retains_operator_provenance(self) -> None:
        """Safe containment reads numeric ranges while evidence retains the submitted facts."""

        confirmation = normalize_lite_confirmation(
            "lite_board",
            {
                "decision": "confirm-or-correct",
                "regions": [
                    {
                        "name": "SRAM",
                        "kind": "ram",
                        "start": "0x20000000",
                        "end": "0x20010000",
                        "readable": True,
                        "writable": True,
                        "executable": False,
                        "source_pages": [42],
                        "source_note": "operator-confirmed datasheet table",
                    }
                ],
                "flash": {"backend_target": None, "erase_sectors": []},
                "recovery": None,
            },
        )
        self.assertEqual(confirmation.map_snapshot["regions"][0]["start"], 0x20000000)
        self.assertEqual(confirmation.evidence["confirmed_regions"][0]["start"], "0x20000000")

    def test_empty_or_uncertain_confirmation_never_establishes_lite_containment(self) -> None:
        with self.assertRaisesRegex(LiteConfirmationError, "at least one"):
            normalize_lite_confirmation(
                "lite_board",
                {
                    "decision": "confirm-or-correct",
                    "regions": [],
                    "flash": {"backend_target": None, "erase_sectors": []},
                    "recovery": None,
                },
            )

    def test_native_recovery_requires_typed_documented_evidence(self) -> None:
        response = {
            "decision": "confirm-or-correct",
            "regions": [
                {
                    "name": "SRAM",
                    "kind": "ram",
                    "start": 0x20000000,
                    "end": 0x20001000,
                    "readable": True,
                    "writable": True,
                    "executable": False,
                    "source_pages": [1],
                    "source_note": "datasheet RAM table",
                },
                {
                    "name": "main flash",
                    "kind": "physical_flash",
                    "start": 0x08000000,
                    "end": 0x08002000,
                    "readable": True,
                    "writable": False,
                    "executable": False,
                    "source_pages": [78],
                    "source_note": "datasheet flash geometry",
                },
            ],
            "flash": {
                "backend_target": None,
                "erase_sectors": [{"start": 0x08000000, "end": 0x08002000}],
            },
            "recovery": {
                "mechanism": "backend_mass_erase",
                "source_pages": [77],
                "source_note": "datasheet recovery section",
            },
        }
        confirmed = normalize_lite_confirmation("lite_board", response)
        self.assertEqual(confirmed.map_snapshot["recovery"], response["recovery"])
        response["recovery"] = {"mechanism": "invented"}
        with self.assertRaisesRegex(LiteConfirmationError, "exactly mechanism"):
            normalize_lite_confirmation("lite_board", response)

    def test_local_extractor_reports_unavailable_failure_and_conservative_candidates(self) -> None:
        with patch("pyocd_debug_mcp.capabilities.lite.shutil.which", return_value=None):
            unavailable = conservative_lite_proposal(__file__)
        self.assertEqual(unavailable["extractor_status"], "unavailable")

        with (
            patch("pyocd_debug_mcp.capabilities.lite.shutil.which", return_value="pdftotext"),
            patch(
                "pyocd_debug_mcp.capabilities.lite.run_owned",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr="bad PDF"),
            ),
        ):
            failed = conservative_lite_proposal(__file__)
        self.assertEqual(failed["extractor_status"], "failed")

        with (
            patch("pyocd_debug_mcp.capabilities.lite.shutil.which", return_value="pdftotext"),
            patch(
                "pyocd_debug_mcp.capabilities.lite.run_owned",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout="Application Flash 0x08000000 \u2013 0x0800FFFF\fSRAM 0x20000000 - 0x200000FF",
                    stderr="",
                ),
            ),
        ):
            extracted = conservative_lite_proposal(__file__)
        self.assertEqual(extracted["extractor_status"], "ok")
        self.assertTrue(extracted["uncertain"])
        self.assertEqual(extracted["regions"][0]["kind"], "application_flash")
        self.assertEqual(extracted["regions"][0]["source_pages"], [1])
        self.assertEqual(extracted["regions"][1]["source_pages"], [2])

    def test_local_extractor_timeout_and_oversized_output_fail_closed(self) -> None:
        with (
            patch("pyocd_debug_mcp.capabilities.lite.shutil.which", return_value="pdftotext"),
            patch(
                "pyocd_debug_mcp.capabilities.lite.run_owned",
                side_effect=subprocess.TimeoutExpired(("pdftotext",), 10),
            ),
        ):
            timed_out = conservative_lite_proposal(__file__)
        self.assertEqual(timed_out["extractor_status"], "failed")

        with (
            patch("pyocd_debug_mcp.capabilities.lite.shutil.which", return_value="pdftotext"),
            patch(
                "pyocd_debug_mcp.capabilities.lite.run_owned",
                return_value=SimpleNamespace(returncode=0, stdout="x" * 1_000_001, stderr=""),
            ),
        ):
            oversized = conservative_lite_proposal(__file__)
        self.assertEqual(oversized["extractor_status"], "failed")


if __name__ == "__main__":
    unittest.main()
