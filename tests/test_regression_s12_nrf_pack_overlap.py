"""Regression coverage for replayed pack geometry entering generic safety maps."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.pack_provision import DeviceBinding, PackSpec, pack_spec_document, sha256_bytes
from pyocd_debug_mcp.safety.map_build import GenericMapGeometry
from pyocd_debug_mcp.safety.regions import AddressRange
from pyocd_debug_mcp.setup_flow.device_support import (
    DeviceSupportCandidate,
    _geometry_digest,
    resolve_registered_pack_geometry,
)


@dataclass(frozen=True)
class _ParsedRegion:
    """Minimal parser-shaped PDSC region used by the registered-pack resolver."""

    name: str
    start: int
    length: int
    type: str
    access: str
    is_default: bool = False
    is_boot_memory: bool = False
    is_testable: bool = False

    @property
    def is_writable(self) -> bool:
        return "w" in self.access


def _pack_payload() -> bytes:
    pdsc = b"""<?xml version="1.0" encoding="UTF-8"?>
<package schemaVersion="1.0"><name>Regression Pack</name><vendor>Example</vendor>
<description>offline replay fixture</description><releases><release version="1.0.0" date="2026-07-27">fixture</release></releases>
<devices><family Dvendor="Example" Dfamily="Generic"><device Dname="REPLAY-OVERLAP" />
</family></devices></package>"""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("Regression.Device.pdsc", pdsc)
    return stream.getvalue()


def _registered_candidate(store: FirmStore) -> DeviceSupportCandidate:
    payload = _pack_payload()
    digest = sha256_bytes(payload)
    binding = DeviceBinding("REPLAY-OVERLAP", "REPLAY-OVERLAP", "generic-replay-target")
    spec = PackSpec(
        "regression-overlap-pack",
        "1.0.0",
        "regression-overlap.pack",
        "https://example.invalid/regression-overlap.pack",
        digest,
        provides_targets=(binding.pyocd_target,),
        device_bindings=(binding,),
    )
    store.ensure_layout()
    store.layout.pack_files.mkdir(exist_ok=True)
    store.atomic_write_bytes(store.layout.pack_files / spec.filename, payload)
    store.atomic_write_pack_manifest({"packs": [pack_spec_document(spec)]})
    return DeviceSupportCandidate(
        "replay-overlap-candidate",
        binding.part_number,
        binding.pdsc_device,
        binding.pyocd_target,
        spec.id,
        spec.filename,
        spec.sha256,
        (),
    )


def _parsed_device(regions: tuple[_ParsedRegion, ...]) -> SimpleNamespace:
    return SimpleNamespace(
        part_number="REPLAY-OVERLAP",
        processors_map={},
        svd=None,
        memory_map=SimpleNamespace(regions=regions),
    )


class ReplayedPackGeometryRegressionTests(unittest.TestCase):
    def test_registered_pack_replay_is_deterministic_and_conservative_at_generic_map_boundary(self) -> None:
        preferred_flash = _ParsedRegion(
            "preferred flash", 0, 0x1000, "flash", "rx", is_default=True, is_boot_memory=True
        )
        descriptive_flash = _ParsedRegion("descriptive flash", 0, 0x2000, "flash", "rx")
        disjoint_flash = _ParsedRegion("second flash bank", 0x4000, 0x1000, "flash", "rx")
        default_ram = _ParsedRegion(
            "default RAM", 0x20000000, 0x1000, "ram", "rwx", is_default=True
        )
        disjoint_ram = _ParsedRegion("second RAM bank", 0x20004000, 0x1000, "ram", "rwx")
        first_order = (
            descriptive_flash,
            disjoint_ram,
            default_ram,
            disjoint_flash,
            preferred_flash,
        )
        second_order = tuple(reversed(first_order))

        with tempfile.TemporaryDirectory() as directory:
            store = FirmStore(Path(directory))
            candidate = _registered_candidate(store)
            parsed_packs = (
                SimpleNamespace(devices=(_parsed_device(first_order),)),
                SimpleNamespace(devices=(_parsed_device(second_order),)),
            )
            with patch(
                "pyocd_debug_mcp.setup_flow.device_support.CmsisPack", side_effect=parsed_packs
            ):
                first = resolve_registered_pack_geometry(candidate, store)
                replayed = resolve_registered_pack_geometry(candidate, store)

        self.assertEqual((first.flash_start, first.flash_end), (0, 0x1000))
        self.assertEqual((first.ram_start, first.ram_end), (0x20000000, 0x20001000))
        self.assertEqual(first.flash_regions, replayed.flash_regions)
        self.assertEqual(first.ram_regions, replayed.ram_regions)
        self.assertEqual(_geometry_digest(first), _geometry_digest(replayed))
        self.assertEqual(
            tuple((row.start, row.end) for row in first.flash_regions),
            ((0, 0x1000), (0x4000, 0x5000)),
        )
        self.assertEqual(
            tuple((row.start, row.end) for row in first.ram_regions),
            ((0x20000000, 0x20001000), (0x20004000, 0x20005000)),
        )
        self.assertEqual(first.erase_sectors, ())
        self.assertIsNone(first.driver_proof_digest)
        self.assertIsNone(first.erased_byte_value)

        generic = GenericMapGeometry(
            tuple(AddressRange(row.start, row.end) for row in first.flash_regions),
            tuple(AddressRange(row.start, row.end) for row in first.ram_regions),
            erase_available=False,
        )
        self.assertEqual(
            generic.physical_flash, (AddressRange(0, 0x1000), AddressRange(0x4000, 0x5000))
        )
        self.assertFalse(generic.contains_flash(AddressRange(0x1000, 0x1001)))
        self.assertFalse(generic.erase_available)
        self.assertEqual(generic.erase_sectors, ())


if __name__ == "__main__":
    unittest.main()
