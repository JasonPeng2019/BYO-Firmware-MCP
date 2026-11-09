"""Regression for persisted built-in-target safety-map replay."""

from __future__ import annotations
from tests.nrf_fixture_support import NRF_FIXTURES, NRF_PACK_SHA256, nrf_registered_pack_fixture

import hashlib
import io
import unittest
import zipfile
from types import SimpleNamespace

from pyocd_debug_mcp.safety.map_build import (
    GenericSafetyMapDocument,
    GenericSourceDigests,
    require_reconciled_authority,
)
from pyocd_debug_mcp.setup_flow import device_support
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
                "map_generator_schema": GenericSourceDigests.build(
                    profile={},
                    device_support={},
                    datasheet_evidence={},
                    deployment_policy={},
                ).map_generator_schema,
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


class NrfRegisteredPackGeometry(unittest.TestCase):
    def test_real_nrf_pdsc_svd_geometry_survives_public_regeneration(self) -> None:
        fixture = nrf_registered_pack_fixture(self)
        # Expected vendor addresses detect selecting an STM/generic fallback;
        # the requests themselves are obtained from the interpreted SVD rows.
        expected = {
            "NVMC.CONFIG": (0x4001E504, True),
            "NVMC.ERASEPAGE": (0x4001E508, True),
            "NVMC.ERASEALL": (0x4001E50C, True),
            "NVMC.ERASEUICR": (0x4001E514, True),
            "UICR.APPROTECT": (0x10001208, True),
            "UICR.NFCPINS": (0x1000120C, True),
            "P0.OUT": (0x50000504, False),
        }
        # Intersected aliases retain a display suffix; it is not a missing
        # register. Addresses/access/prohibitions below still come from pyOCD.
        rows = {row.name.split(" (+", 1)[0]: row for row in fixture.geometry.peripheral_regions}
        for name, (address, prohibited) in expected.items():
            with self.subTest(register=name):
                row = rows[name]
                self.assertEqual((row.start, row.end), (address, address + 4))
                self.assertEqual(row.prohibited, prohibited)
                self.assertTrue(row.writable)
                if name.startswith("UICR."):
                    self.assertTrue(
                        any(
                            memory.start <= row.start and row.end <= memory.end
                            for memory in fixture.geometry.flash_regions
                        )
                    )
        self.assertTrue(rows["P0.OUT"].readable)
        self.assertFalse(rows["NVMC.ERASEALL"].readable)
        self.assertTrue(
            any(row.start == 0 and row.end == 0x100000 for row in fixture.geometry.flash_regions)
        )
        self.assertTrue(
            any(
                row.start == 0x20000000 and row.end == 0x20040000
                for row in fixture.geometry.ram_regions
            )
        )
        self.assertEqual(fixture.document.authority_source["kind"], "resolved_pack")
        self.assertEqual(fixture.document.authority_source["pack_sha256"], NRF_PACK_SHA256)
        self.assertEqual(fixture.backend.calls, [])

    def test_real_svd_memory_overlap_preserves_prohibitions_without_granting_access(self) -> None:
        # Use the original verified SVD bytes with an explicit exclusion covering
        # all registers: ordinary P0 must disappear, security controls must not.
        source = NRF_FIXTURES / "NordicSemiconductor.nRF_DeviceFamilyPack.8.44.1.pack"
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), NRF_PACK_SHA256)
        with zipfile.ZipFile(source) as archive:
            svd = archive.read("SVD/nrf52840.svd")
        rows = device_support._svd_peripheral_regions(
            SimpleNamespace(svd=io.BytesIO(svd)),
            (device_support.PackAddressRegion("excluded physical memory", 0, 0xE0000000),),
        )
        self.assertTrue(rows)
        self.assertTrue(all(row.prohibited for row in rows))
        self.assertFalse(any(row.start == 0x50000504 for row in rows))
        for address in (0x10001208, 0x1000120C, 0x4001E508):
            with self.subTest(address=address):
                self.assertTrue(any((row.start, row.end) == (address, address + 4) for row in rows))


if __name__ == "__main__":
    unittest.main()
