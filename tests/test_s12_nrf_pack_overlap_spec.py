"""Adversarial specifications for generic CMSIS-Pack physical-region canonicalization."""

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

from pyocd.target.pack.cmsis_pack import CmsisPack as NativeCmsisPack

from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.pack_provision import (
    DeviceBinding,
    PackProvisionError,
    PackSpec,
    pack_spec_document,
    sha256_bytes,
)
from pyocd_debug_mcp.safety.map_build import GenericMapGeometry, SafetyMapError
from pyocd_debug_mcp.safety.regions import AddressRange
from pyocd_debug_mcp.setup_flow.device_support import (
    DeviceSupportCandidate,
    _canonical_physical_regions,
    resolve_registered_pack_geometry,
)


@dataclass(frozen=True)
class _PdscRegion:
    """Only the parsed PDSC region traits used by the canonicalization boundary."""

    name: str
    start: int
    length: int
    type: str = "FlashRegion"
    access: str = "rwx"
    is_default: bool = False
    is_boot_memory: bool = False
    is_testable: bool = False


@dataclass(frozen=True)
class _ParsedPackRegion:
    """The parsed CMSIS-region contract consumed by the production resolver."""

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


def _rows(*regions: _PdscRegion, memory_kind: str = "flash") -> tuple[tuple[str, int, int, str], ...]:
    return tuple(
        (region.name, region.start, region.end, region.access)
        for region in _canonical_physical_regions(regions, memory_kind=memory_kind)
    )


def _pack_bytes(pdsc: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("Spec.Device.pdsc", pdsc)
    return stream.getvalue()


def _overlapping_pdsc() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<package schemaVersion="1.0">
  <name>Spec Pack</name>
  <vendor>Example</vendor>
  <description>offline geometry fixture</description>
  <releases><release version="1.0.0" date="2026-07-27">fixture</release></releases>
  <devices><family Dvendor="Example" Dfamily="Generic">
    <device Dname="SPEC-OVERLAP">
      <memory name="preferred flash" access="rx" start="0x0" size="0x100000" default="1" startup="1" />
      <memory name="descriptive flash" access="rx" start="0x0" size="0x200000" />
      <memory name="working RAM" access="rwx" start="0x20000000" size="0x40000" default="1" />
      <flashinfo start="0x0" pagesize="0x1000"><block size="0x1000" count="512" /></flashinfo>
    </device>
  </family></devices>
</package>
"""


def _registered_fixture(store: FirmStore) -> tuple[DeviceSupportCandidate, bytes]:
    payload = _pack_bytes(_overlapping_pdsc())
    digest = sha256_bytes(payload)
    binding = DeviceBinding("SPEC-OVERLAP", "SPEC-OVERLAP", "spec-overlap-target")
    spec = PackSpec(
        "spec-overlap-pack",
        "1.0.0",
        "spec-overlap.pack",
        "https://example.invalid/spec-overlap.pack",
        digest,
        provides_targets=(binding.pyocd_target,),
        device_bindings=(binding,),
    )
    store.ensure_layout()
    store.layout.pack_files.mkdir(exist_ok=True)
    store.atomic_write_bytes(store.layout.pack_files / spec.filename, payload)
    store.atomic_write_pack_manifest({"packs": [pack_spec_document(spec)]})
    return (
        DeviceSupportCandidate(
            "spec-overlap-candidate",
            binding.part_number,
            binding.pdsc_device,
            binding.pyocd_target,
            spec.id,
            spec.filename,
            spec.sha256,
            (),
        ),
        payload,
    )


class PackPhysicalOverlapSpecTests(unittest.TestCase):
    """CL-001/CL-002: only unambiguous whole PDSC descriptors become authority."""

    def test_preferred_nordic_shaped_descriptor_discards_broader_row_without_tail(self) -> None:
        preferred = _PdscRegion("preferred", 0, 0x100000, is_default=True, is_boot_memory=True, is_testable=True)
        broader = _PdscRegion("descriptive container", 0, 0x200000)

        rows = _rows(broader, preferred)

        self.assertEqual(rows, (("preferred", 0, 0x100000, "rwx"),))
        geometry = GenericMapGeometry(
            tuple(AddressRange(start, end) for _, start, end, _ in rows),
            (AddressRange(0x20000000, 0x20010000),),
            erase_available=False,
        )
        self.assertEqual(geometry.physical_flash, (AddressRange(0, 0x100000),))
        self.assertFalse(geometry.contains_flash(AddressRange(0x100000, 0x100001)))
        self.assertFalse(geometry.erase_available)
        self.assertEqual(geometry.erase_sectors, ())

    def test_verified_pack_replay_canonicalizes_physical_geometry_before_generic_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FirmStore(Path(directory))
            candidate, payload = _registered_fixture(store)
            parsed_leaf = NativeCmsisPack(io.BytesIO(payload)).devices[0]
            self.assertEqual(parsed_leaf.part_number, candidate.pdsc_device)
            parsed_device = SimpleNamespace(
                part_number=candidate.pdsc_device,
                processors_map={},
                svd=None,
                memory_map=SimpleNamespace(
                    regions=(
                        _ParsedPackRegion(
                            "preferred flash",
                            0,
                            0x100000,
                            "flash",
                            "rx",
                            is_default=True,
                            is_boot_memory=True,
                            is_testable=True,
                        ),
                        _ParsedPackRegion("descriptive flash", 0, 0x200000, "flash", "rx"),
                        _ParsedPackRegion("working RAM", 0x20000000, 0x40000, "ram", "rwx"),
                    )
                ),
            )
            with patch(
                "pyocd_debug_mcp.setup_flow.device_support.CmsisPack",
                return_value=SimpleNamespace(devices=(parsed_device,)),
            ):
                geometry = resolve_registered_pack_geometry(candidate, store)

        self.assertEqual(
            tuple((row.start, row.end) for row in geometry.flash_regions), ((0, 0x100000),)
        )
        self.assertEqual((geometry.flash_start, geometry.flash_end), (0, 0x100000))
        self.assertEqual((geometry.ram_start, geometry.ram_end), (0x20000000, 0x20040000))
        self.assertEqual(
            tuple((row.start, row.end) for row in geometry.ram_regions),
            ((0x20000000, 0x20040000),),
        )
        self.assertNotIn(
            (0x100000, 0x200000), tuple((row.start, row.end) for row in geometry.flash_regions)
        )
        generic = GenericMapGeometry(
            tuple(AddressRange(row.start, row.end) for row in geometry.flash_regions),
            tuple(AddressRange(row.start, row.end) for row in geometry.ram_regions),
            erase_available=False,
        )
        self.assertEqual(generic.physical_flash, (AddressRange(0, 0x100000),))
        self.assertFalse(generic.contains_flash(AddressRange(0x100000, 0x100001)))
        self.assertFalse(generic.erase_available)
        self.assertEqual(generic.erase_sectors, ())

    def test_lower_precedence_partial_overlap_is_discarded_whole_not_clipped(self) -> None:
        preferred = _PdscRegion("boot", 0x1000, 0x1000, is_boot_memory=True)
        lower = _PdscRegion("broad", 0x1800, 0x1000)

        self.assertEqual(_rows(lower, preferred), (("boot", 0x1000, 0x2000, "rwx"),))

    def test_exact_duplicate_collapse_uses_precedence_and_is_independent_of_input_order(self) -> None:
        ordinary = _PdscRegion("ordinary", 0, 0x1000, access="r")
        default = _PdscRegion("default", 0, 0x1000, access="rx", is_default=True)
        bank = _PdscRegion("bank-b", 0x3000, 0x1000)

        expected = (("default", 0, 0x1000, "rx"), ("bank-b", 0x3000, 0x4000, "rwx"))
        self.assertEqual(_rows(ordinary, bank, default), expected)
        self.assertEqual(_rows(default, ordinary, bank), expected)

    def test_disjoint_flash_banks_and_writable_ram_banks_remain_separate_and_sorted(self) -> None:
        flash = (
            _PdscRegion("late", 0x8000, 0x1000),
            _PdscRegion("early", 0x1000, 0x1000),
        )
        ram = (
            _PdscRegion("ram-b", 0x20004000, 0x1000),
            _PdscRegion("ram-a", 0x20000000, 0x1000),
        )

        self.assertEqual(
            _rows(*flash),
            (("early", 0x1000, 0x2000, "rwx"), ("late", 0x8000, 0x9000, "rwx")),
        )
        self.assertEqual(
            _rows(*ram, memory_kind="RAM"),
            (("ram-a", 0x20000000, 0x20001000, "rwx"), ("ram-b", 0x20004000, 0x20005000, "rwx")),
        )

    def test_equal_precedence_flash_partial_and_nested_overlaps_fail_in_either_order(self) -> None:
        cases = (
            (_PdscRegion("left", 0x1000, 0x1000), _PdscRegion("right", 0x1800, 0x1000)),
            (_PdscRegion("outer", 0x1000, 0x2000), _PdscRegion("inner", 0x1800, 0x400)),
        )
        for first, second in cases:
            for candidates in ((first, second), (second, first)):
                with self.subTest(candidates=candidates), self.assertRaisesRegex(
                    PackProvisionError,
                    r"ambiguous verified PDSC physical flash descriptions overlap: \[0x[0-9a-f]+, 0x[0-9a-f]+\) and \[0x[0-9a-f]+, 0x[0-9a-f]+\)",
                ):
                    _rows(*candidates)

    def test_equal_precedence_writable_ram_overlap_fails_honestly(self) -> None:
        first = _PdscRegion("ram-first", 0x20000000, 0x1000)
        second = _PdscRegion("ram-second", 0x20000800, 0x1000)

        with self.assertRaisesRegex(
            PackProvisionError, "ambiguous verified PDSC physical RAM descriptions overlap"
        ) as raised:
            _rows(first, second, memory_kind="RAM")
        self.assertIn("[0x20000000, 0x20001000)", str(raised.exception))
        self.assertIn("[0x20000800, 0x20001800)", str(raised.exception))

    def test_generic_map_geometry_remains_strict_for_direct_overlapping_authority(self) -> None:
        cases = (
            (
                (AddressRange(0, 0x1000), AddressRange(0x800, 0x1800)),
                (AddressRange(0x20000000, 0x20001000),),
                "generic physical flash regions must not overlap",
            ),
            (
                (AddressRange(0, 0x1000),),
                (AddressRange(0x20000000, 0x20001000), AddressRange(0x20000800, 0x20001800)),
                "generic physical RAM regions must not overlap",
            ),
            (
                (AddressRange(0x20000000, 0x20001000),),
                (AddressRange(0x20000800, 0x20001800),),
                "generic physical flash and RAM must not overlap",
            ),
        )
        for flash, ram, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(SafetyMapError, message):
                GenericMapGeometry(flash, ram, erase_available=False)


if __name__ == "__main__":
    unittest.main()
