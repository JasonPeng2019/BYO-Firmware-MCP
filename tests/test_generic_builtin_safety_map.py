"""Regression for persisted built-in-target safety-map replay."""

from __future__ import annotations

import unittest

from pyocd_debug_mcp.safety.map_build import (
    GenericSafetyMapDocument,
    require_reconciled_authority,
)
from pyocd_debug_mcp.setup_flow.device_support import resolve_builtin_target_support


class BuiltInTargetMapReplay(unittest.TestCase):
    def test_round_trips_resolved_builtin_target_authority(self) -> None:
        authority = (
            resolve_builtin_target_support("nRF52840-QIAA", "nrf52840")
            .with_identity_proof(0x410FC241)
            .to_authority_document()
        )
        digest = authority["support_id"]
        provenance = [
            {
                "authority": "device_support",
                "source_id": f"builtin-target:{digest}",
                "detail": "installed built-in target nrf52840",
            }
        ]
        document = {
            "schema_version": 3,
            "board_id": "nrf52840",
            "identity": {
                "mcu_part_number": "nRF52840-QIAA",
                "pyocd_target": "nrf52840",
                "authority_kind": "resolved_builtin_target",
                "support_id": digest,
            },
            "authority_source": authority,
            "source_digests": {
                "semantic_profile": "c" * 64,
                "device_support": "d" * 64,
                "datasheet_evidence": "e" * 64,
                "deployment_policy": "f" * 64,
                "map_generator_schema": "0" * 64,
            },
            "geometry": {
                "physical_flash": [{"start": 0, "end": 4096}],
                "physical_ram": [{"start": 0x20000000, "end": 0x20001000}],
                "erase": {"kind": "unavailable"},
            },
            "partitions": {"application": None, "bootloader": None},
            "deployment_policy": {"kind": "none"},
            "regions": [
                {
                    "name": "built-in flash",
                    "kind": "physical_flash",
                    "start": 0,
                    "end": 4096,
                    "executable": False,
                    "provenance": provenance,
                },
                {
                    "name": "built-in RAM",
                    "kind": "physical_ram",
                    "start": 0x20000000,
                    "end": 0x20001000,
                    "executable": False,
                    "provenance": provenance,
                },
            ],
        }

        replayed = GenericSafetyMapDocument.from_document(document)

        self.assertEqual(replayed.to_document(), document)
        require_reconciled_authority(replayed)


if __name__ == "__main__":
    unittest.main()
