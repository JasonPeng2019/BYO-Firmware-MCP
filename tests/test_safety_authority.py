"""Focused canonical-map and live-semantic authority regressions."""

from __future__ import annotations

import json
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from firmware_mcp.adapters.debug_interface import PhysicalMemoryRegion, TargetSessionHandle
from firmware_mcp.board_config import BoardConfig
from firmware_mcp.firmstore.profiles import ProfileRepository
from firmware_mcp.firmstore.store import FirmStore
from firmware_mcp.services.safety_authority import (
    SafetyAuthority,
    SafetyAuthorityError,
    build_document,
    map_digest,
    validate_document,
)
from firmware_mcp.safety.linker import LinkerEvidenceError
from firmware_mcp.services.physical_memory import PhysicalMemoryAccessError
from firmware_mcp.setup_flow.device_support import PackAddressRegion, PackProvisionError


def _elf_with_file_bytes_beyond_snapshot() -> bytes:
    """A structurally recognizable 32-bit ELF whose PT_LOAD file bytes exceed EOF."""

    header = struct.pack(
        "<16sHHIIIIIHHHHHH",
        b"\x7fELF\x01\x01\x01" + b"\0" * 9,
        2,
        40,
        1,
        0,
        52,
        0,
        0,
        52,
        32,
        1,
        0,
        0,
        0,
    )
    load = struct.pack("<IIIIIIII", 1, 0x100, 0, 0, 4, 4, 5, 4)
    return header + load


class SafetyAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = FirmStore(self.root)
        self.board = SimpleNamespace(
            board_id="board-a",
            mcu_part_number="PART-A",
            provider_id="provider-a",
            target="part-a",
            provider_support_identity="support-a",
            silicon_id_capability="exact",
            silicon_id_provenance="datasheet section 12",
            silicon_id_bound_part_number="PART-A",
            silicon_id_support_identity="support-a",
            silicon_id_addr=0,
            silicon_id_expected=0,
            silicon_id_mask=0xFFFFFFFF,
            silicon_id_width_bits=32,
        )
        self.handle = cast(
            TargetSessionHandle,
            SimpleNamespace(
                board=cast(BoardConfig, self.board),
                metadata=SimpleNamespace(runtime_token="session-a"),
            ),
        )
        self.regions: tuple[PhysicalMemoryRegion, ...] = (
            PhysicalMemoryRegion(
                0, 0x100, True, True, True, "physical_flash", "flash", "provider", "session-a"
            ),
            PhysicalMemoryRegion(
                0x100, 0x200, True, True, False, "physical_ram", "ram", "provider", "session-a"
            ),
            PhysicalMemoryRegion(
                0x400, 0x500, True, True, False, "peripheral", "io", "provider", "session-a"
            ),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _layout(self) -> dict[str, object]:
        source = self.root / "datasheet.txt"
        source.write_text("section 12", encoding="utf-8")
        return {
            "schema_version": 1,
            "board_id": "board-a",
            "regions": [
                {
                    "name": "code",
                    "role": "application",
                    "start": 0,
                    "end": 0x80,
                    "source_path": str(source),
                    "source_locator": "section 12",
                }
            ],
        }

    def _document(self) -> dict[str, object]:
        return build_document(
            board_id="board-a",
            handle=self.handle,
            regions_for=lambda _handle: self.regions,
            layout=self._layout(),
            read_memory=lambda _handle, _address, _width: 0,
        )

    def test_canonical_digest_and_partitioned_unknown_evidence(self) -> None:
        document = self._document()
        checked = validate_document(document, board_id="board-a")

        self.assertEqual(checked["digest"], map_digest(checked))
        self.assertEqual(
            [(region["start"], region["end"], region["role"]) for region in checked["regions"]],
            [
                (0, 0x80, "application"),
                (0x80, 0x100, "unknown"),
                (0x100, 0x200, "ordinary_ram"),
                (0x400, 0x500, "peripheral"),
            ],
        )
        corrupted = dict(checked)
        corrupted["digest"] = "0" * 64
        with self.assertRaisesRegex(SafetyAuthorityError, "digest"):
            validate_document(corrupted, board_id="board-a")

    def test_layout_needs_explicit_source_not_filename_authority(self) -> None:
        layout = self._layout()
        region = cast(list[dict[str, object]], layout["regions"])[0]
        assert isinstance(region, dict)
        region["source_path"] = "not-a-proof.txt"

        with self.assertRaisesRegex(SafetyAuthorityError, "evidence file"):
            build_document(
                board_id="board-a",
                handle=self.handle,
                regions_for=lambda _handle: self.regions,
                layout=layout,
            )

    def test_live_map_binding_rejects_changed_provider_geometry(self) -> None:
        document = self._document()
        path = self.store.layout.safety_board("board-a") / "memory-map.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        reference = path.relative_to(self.root).as_posix()
        profiles = SimpleNamespace(load=lambda _board_id: SimpleNamespace(safety_ref=reference))
        authority = SafetyAuthority(
            self.store,
            cast(ProfileRepository, profiles),
            lambda _handle: self.regions,
            lambda _handle, _address, _width: 0,
        )

        self.assertEqual(
            authority.binding("board-a", self.handle)["safety_map_digest"], document["digest"]
        )
        self.regions = (
            PhysicalMemoryRegion(
                0, 0x80, True, True, True, "physical_flash", "flash", "provider", "session-a"
            ),
        )
        with self.assertRaisesRegex(SafetyAuthorityError, "physical-region"):
            authority.binding("board-a", self.handle)

    def test_live_map_binding_rejects_provider_native_split_and_access_change(self) -> None:
        document = self._document()
        original_regions = self.regions
        path = self.store.layout.safety_board("board-a") / "memory-map.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        profiles = SimpleNamespace(
            load=lambda _board_id: SimpleNamespace(
                safety_ref=path.relative_to(self.root).as_posix()
            )
        )
        authority = SafetyAuthority(
            self.store,
            cast(ProfileRepository, profiles),
            lambda _handle: self.regions,
            lambda _handle, _address, _width: 0,
        )
        self.regions = (
            PhysicalMemoryRegion(
                0, 0x80, True, True, True, "physical_flash", "flash", "provider", "session-a"
            ),
            PhysicalMemoryRegion(
                0x80, 0x100, True, True, True, "physical_flash", "flash", "provider", "session-a"
            ),
            *original_regions[1:],
        )
        with self.assertRaisesRegex(SafetyAuthorityError, "provider-native"):
            authority.binding("board-a", self.handle)

        variants = {
            "removed": original_regions[1:],
            "expanded": (
                PhysicalMemoryRegion(
                    0,
                    0x101,
                    True,
                    True,
                    True,
                    "physical_flash",
                    "flash",
                    "provider",
                    "session-a",
                ),
                PhysicalMemoryRegion(
                    0x101,
                    0x200,
                    True,
                    True,
                    False,
                    "physical_ram",
                    "ram",
                    "provider",
                    "session-a",
                ),
                original_regions[2],
            ),
            "access-changed": (
                PhysicalMemoryRegion(
                    0,
                    0x100,
                    True,
                    False,
                    True,
                    "physical_flash",
                    "flash",
                    "provider",
                    "session-a",
                ),
                *original_regions[1:],
            ),
        }
        for label, changed in variants.items():
            with self.subTest(label=label):
                self.regions = changed
                with self.assertRaisesRegex(SafetyAuthorityError, "provider-native"):
                    authority.binding("board-a", self.handle)

    def test_schema_rejects_extra_and_missing_per_fact_provenance(self) -> None:
        document = self._document()
        extra = json.loads(json.dumps(document))
        extra["regions"][0]["provenance"]["extra"] = []
        extra["digest"] = map_digest(extra)
        with self.assertRaisesRegex(SafetyAuthorityError, "separate canonical provenance"):
            validate_document(extra)
        missing = json.loads(json.dumps(document))
        del missing["regions"][0]["provenance"]["executable"]
        missing["digest"] = map_digest(missing)
        with self.assertRaisesRegex(SafetyAuthorityError, "separate canonical provenance"):
            validate_document(missing)
        wrong = json.loads(json.dumps(document))
        provider = next(
            source["identifier"]
            for source in wrong["sources"]
            if source["kind"] == "provider-region"
        )
        wrong["regions"][0]["provenance"]["role"] = [f"source:{provider}"]
        wrong["digest"] = map_digest(wrong)
        with self.assertRaisesRegex(SafetyAuthorityError, "inappropriate source kind"):
            validate_document(wrong)

    def test_replayed_support_geometry_publishes_erase_evidence(self) -> None:
        geometry = {
            "flash_regions": [{"name": "flash", "start": 0, "end": 0x100, "access": "rwx"}],
            "ram_regions": [{"name": "ram", "start": 0x100, "end": 0x200, "access": "rw"}],
            "rom_regions": [],
            "peripheral_regions": [],
            "cpu_system_regions": [],
            "erase_sectors": [{"start": 0, "end": 0x80}, {"start": 0x80, "end": 0x100}],
        }
        document = build_document(
            board_id="board-a",
            handle=self.handle,
            regions_for=lambda _handle: self.regions,
            support_geometry=geometry,
            support_identity="support-a",
        )
        self.assertTrue(document["erase_geometry"]["available"])
        self.assertEqual(len(document["erase_geometry"]["ranges"]), 2)
        region = next(item for item in document["regions"] if item["start"] == 0x100)
        support = next(
            source["identifier"]
            for source in document["sources"]
            if source["detail"] == "replayed ram_regions[0] exact physical range"
        )
        self.assertIn(f"source:{support}", region["provenance"]["readable"])
        self.assertIn(f"source:{support}", region["provenance"]["writable"])
        self.assertIn(f"source:{support}", region["provenance"]["executable"])

    def test_conflicting_support_geometry_does_not_override_live_provider_facts(self) -> None:
        geometry = {
            "flash_regions": [{"name": "not-ram", "start": 0x100, "end": 0x200, "access": "rw"}],
            "ram_regions": [],
            "rom_regions": [],
            "peripheral_regions": [],
            "cpu_system_regions": [],
            "erase_sectors": [],
        }
        with self.assertRaisesRegex(
            SafetyAuthorityError, "conflict with live provider physical facts"
        ):
            build_document(
                board_id="board-a",
                handle=self.handle,
                regions_for=lambda _handle: self.regions,
                support_geometry=geometry,
                support_identity="support-a",
            )

    def _geometry(self, **groups: object) -> dict[str, object]:
        geometry: dict[str, object] = {
            "flash_regions": [],
            "ram_regions": [],
            "rom_regions": [],
            "peripheral_regions": [],
            "cpu_system_regions": [],
            "erase_sectors": [],
        }
        geometry.update(groups)
        return geometry

    def test_exact_support_access_vocabulary_reconciles_write_only(self) -> None:
        write_only = (
            PhysicalMemoryRegion(
                0, 0x100, False, True, False, "physical_flash", "flash", "provider", "session-a"
            ),
        )
        document = build_document(
            board_id="board-a",
            handle=self.handle,
            regions_for=lambda _handle: write_only,
            support_geometry=self._geometry(
                flash_regions=[{"name": "flash", "start": 0, "end": 0x100, "access": "write-only"}]
            ),
            support_identity="support-a",
        )
        region = document["regions"][0]
        self.assertFalse(region["readable"])
        self.assertTrue(region["writable"])
        self.assertFalse(region["executable"])
        self.assertTrue(PackAddressRegion("write", 0, 1, "write-only").writable)
        self.assertFalse(PackAddressRegion("write", 0, 1, "write-only").readable)
        self.assertTrue(PackAddressRegion("code", 0, 1, "rx").executable)
        with self.assertRaises(PackProvisionError):
            PackAddressRegion("bad", 0, 1, "read-write-execute")

    def test_compatible_rx_sources_attest_false_writable_fact(self) -> None:
        document = build_document(
            board_id="board-a",
            handle=self.handle,
            regions_for=lambda _handle: (
                PhysicalMemoryRegion(
                    0,
                    0x100,
                    True,
                    False,
                    True,
                    "physical_flash",
                    "flash",
                    "provider",
                    "session-a",
                ),
            ),
            support_geometry=self._geometry(
                flash_regions=[{"name": "flash", "start": 0, "end": 0x100, "access": "rx"}]
            ),
            support_identity="support-a",
        )
        provider = next(
            source["identifier"]
            for source in document["sources"]
            if source["kind"] == "provider-region"
        )
        support = next(
            source["identifier"]
            for source in document["sources"]
            if source["detail"] == "replayed flash_regions[0] exact physical range"
        )

        self.assertIn(f"source:{provider}", document["regions"][0]["provenance"]["writable"])
        self.assertIn(f"source:{support}", document["regions"][0]["provenance"]["writable"])

    def test_compatible_write_only_sources_attest_false_readable_fact(self) -> None:
        document = build_document(
            board_id="board-a",
            handle=self.handle,
            regions_for=lambda _handle: (
                PhysicalMemoryRegion(
                    0,
                    0x100,
                    False,
                    True,
                    False,
                    "physical_flash",
                    "flash",
                    "provider",
                    "session-a",
                ),
            ),
            support_geometry=self._geometry(
                flash_regions=[{"name": "flash", "start": 0, "end": 0x100, "access": "write-only"}]
            ),
            support_identity="support-a",
        )
        provider = next(
            source["identifier"]
            for source in document["sources"]
            if source["kind"] == "provider-region"
        )
        support = next(
            source["identifier"]
            for source in document["sources"]
            if source["detail"] == "replayed flash_regions[0] exact physical range"
        )

        self.assertIn(f"source:{provider}", document["regions"][0]["provenance"]["readable"])
        self.assertIn(f"source:{support}", document["regions"][0]["provenance"]["readable"])

    def test_support_and_live_access_facts_reconcile_bidirectionally(self) -> None:
        cases = (
            (
                "executable",
                (True, True, True),
                "rw",
            ),
            (
                "support-read-live-write",
                (False, True, False),
                "r",
            ),
            (
                "support-write-live-read",
                (True, False, False),
                "w",
            ),
        )
        for label, (readable, writable, executable), access in cases:
            with self.subTest(label=label):
                live = (
                    PhysicalMemoryRegion(
                        0,
                        0x100,
                        readable,
                        writable,
                        executable,
                        "physical_flash",
                        "flash",
                        "provider",
                        "session-a",
                    ),
                )
                with self.assertRaisesRegex(SafetyAuthorityError, "conflict with live provider"):
                    build_document(
                        board_id="board-a",
                        handle=self.handle,
                        regions_for=lambda _handle, values=live: values,
                        support_geometry=self._geometry(
                            flash_regions=[
                                {"name": "flash", "start": 0, "end": 0x100, "access": access}
                            ]
                        ),
                        support_identity="support-a",
                    )

    def test_support_subrange_partitions_live_region_with_exact_provenance(self) -> None:
        document = build_document(
            board_id="board-a",
            handle=self.handle,
            regions_for=lambda _handle: self.regions,
            support_geometry=self._geometry(
                flash_regions=[{"name": "middle", "start": 0x20, "end": 0x40, "access": "rwx"}]
            ),
            support_identity="support-a",
        )
        flash = [region for region in document["regions"] if region["end"] <= 0x100]
        self.assertEqual(
            [(region["start"], region["end"]) for region in flash],
            [(0, 0x20), (0x20, 0x40), (0x40, 0x100)],
        )
        middle = flash[1]
        self.assertEqual(len(middle["provenance"]["physical"]), 2)
        self.assertEqual(len(flash[0]["provenance"]["physical"]), 1)
        self.assertEqual(len(flash[2]["provenance"]["physical"]), 1)

    def test_support_only_facts_keep_roles_access_and_no_provider_provenance(self) -> None:
        provider = (self.regions[0],)
        document = build_document(
            board_id="board-a",
            handle=self.handle,
            regions_for=lambda _handle: provider,
            support_geometry=self._geometry(
                ram_regions=[{"name": "ram", "start": 0x100, "end": 0x200, "access": "rw"}],
                peripheral_regions=[
                    {"name": "io", "start": 0x300, "end": 0x400, "access": "write-only"}
                ],
                rom_regions=[{"name": "rom", "start": 0x500, "end": 0x600, "access": "rx"}],
                flash_regions=[
                    {"name": "other-flash", "start": 0x700, "end": 0x800, "access": "rwx"}
                ],
                erase_sectors=[{"start": 0x700, "end": 0x800}],
            ),
            support_identity="support-a",
        )
        regions = {region["start"]: region for region in document["regions"]}
        self.assertEqual(regions[0x100]["role"], "ordinary_ram")
        self.assertEqual(regions[0x300]["role"], "peripheral")
        self.assertEqual(regions[0x500]["role"], "rom")
        self.assertEqual(regions[0x700]["role"], "unknown")
        self.assertFalse(regions[0x300]["readable"])
        self.assertTrue(regions[0x300]["writable"])
        source_kinds = {
            f"source:{source['identifier']}": source["kind"] for source in document["sources"]
        }
        for start in (0x100, 0x300, 0x500, 0x700):
            for refs in regions[start]["provenance"].values():
                self.assertNotIn("provider-region", {source_kinds[ref] for ref in refs})
        self.assertTrue(document["erase_geometry"]["available"])
        nonflash_erase = json.loads(json.dumps(document))
        support = next(
            source["identifier"]
            for source in nonflash_erase["sources"]
            if source["detail"] == "replayed ram_regions[0] exact physical range"
        )
        nonflash_erase["erase_geometry"] = {
            "available": True,
            "ranges": [{"start": 0x100, "end": 0x200, "provenance": [f"source:{support}"]}],
        }
        nonflash_erase["digest"] = map_digest(nonflash_erase)
        with self.assertRaisesRegex(SafetyAuthorityError, "flash evidence"):
            validate_document(nonflash_erase)

    def test_overlapping_support_facts_merge_or_conflict_without_source_priority(self) -> None:
        live = (
            PhysicalMemoryRegion(
                0x1000, 0x1100, True, True, False, "physical_ram", "ram", "provider", "session-a"
            ),
        )
        merged = build_document(
            board_id="board-a",
            handle=self.handle,
            regions_for=lambda _handle: live,
            support_geometry=self._geometry(
                flash_regions=[
                    {"name": "one", "start": 0, "end": 0x50, "access": "r"},
                    {"name": "two", "start": 0x20, "end": 0x70, "access": "r"},
                ]
            ),
            support_identity="support-a",
        )
        overlap = next(region for region in merged["regions"] if region["start"] == 0x20)
        self.assertEqual(len(overlap["provenance"]["physical"]), 2)
        with self.assertRaisesRegex(SafetyAuthorityError, "Conflicting replayed support"):
            build_document(
                board_id="board-a",
                handle=self.handle,
                regions_for=lambda _handle: live,
                support_geometry=self._geometry(
                    flash_regions=[
                        {"name": "one", "start": 0, "end": 0x50, "access": "r"},
                        {"name": "two", "start": 0x20, "end": 0x70, "access": "w"},
                    ]
                ),
                support_identity="support-a",
            )

    def test_support_only_records_do_not_replace_live_freshness_or_access(self) -> None:
        live = (self.regions[0],)
        document = build_document(
            board_id="board-a",
            handle=self.handle,
            regions_for=lambda _handle: live,
            support_geometry=self._geometry(
                ram_regions=[{"name": "ram", "start": 0x100, "end": 0x200, "access": "rw"}]
            ),
            support_identity="support-a",
            read_memory=lambda _handle, _address, _width: 0,
        )
        path = self.store.layout.safety_board("board-a") / "memory-map.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        profiles = SimpleNamespace(
            load=lambda _board_id: SimpleNamespace(
                safety_ref=path.relative_to(self.root).as_posix()
            )
        )
        authority = SafetyAuthority(
            self.store,
            cast(ProfileRepository, profiles),
            lambda _handle: live,
            lambda _handle, _address, _width: 0,
        )
        self.assertEqual(
            authority.binding("board-a", self.handle)["safety_map_digest"], document["digest"]
        )
        with self.assertRaisesRegex(
            PhysicalMemoryAccessError, "Live provider facts do not fully cover"
        ):
            authority.require("board-a", self.handle, 0x100, 4, "read", roles={"ordinary_ram"})
        changed = live + (
            PhysicalMemoryRegion(
                0x100, 0x200, True, True, False, "physical_ram", "ram", "provider", "session-a"
            ),
        )
        authority = SafetyAuthority(
            self.store,
            cast(ProfileRepository, profiles),
            lambda _handle: changed,
            lambda _handle, _address, _width: 0,
        )
        with self.assertRaisesRegex(SafetyAuthorityError, "provider-native"):
            authority.binding("board-a", self.handle)

    def test_file_backed_non_executable_load_is_application_evidence(self) -> None:
        """Only breakpoint authority is restricted to PF_X load bytes."""

        with patch(
            "firmware_mcp.services.safety_authority.file_backed_elf_ranges",
            return_value=((0x100, 0x104),),
        ):
            document = build_document(
                board_id="board-a",
                handle=self.handle,
                regions_for=lambda _handle: self.regions,
                application_elf=(self.root / "firmware.elf", b"immutable ELF bytes"),
            )
        region = next(item for item in document["regions"] if item["start"] == 0x100)
        self.assertEqual(region["role"], "application")
        self.assertFalse(region["executable"])

    def test_refresh_authority_rejects_file_backed_load_past_immutable_snapshot(self) -> None:
        with self.assertRaisesRegex(LinkerEvidenceError, "immutable snapshot"):
            build_document(
                board_id="board-a",
                handle=self.handle,
                regions_for=lambda _handle: self.regions,
                application_elf=(
                    self.root / "malformed.elf",
                    _elf_with_file_bytes_beyond_snapshot(),
                ),
            )

    def test_available_erase_ranges_are_canonical_contained_provenanced_facts(self) -> None:
        document = self._document()
        provider = next(
            source["identifier"]
            for source in cast(list[dict[str, object]], document["sources"])
            if source["kind"] == "provider-region"
        )
        document["erase_geometry"] = {
            "available": True,
            "ranges": [{"start": 0, "end": 0x80, "provenance": [f"source:{provider}"]}],
        }
        document["digest"] = map_digest(document)
        self.assertEqual(validate_document(document)["erase_geometry"], document["erase_geometry"])

        malformed = json.loads(json.dumps(document))
        malformed["erase_geometry"]["ranges"][0]["provenance"] = ["source:not-present"]
        malformed["digest"] = map_digest(malformed)
        with self.assertRaisesRegex(SafetyAuthorityError, "erase provenance"):
            validate_document(malformed)

        outside = json.loads(json.dumps(document))
        outside["erase_geometry"]["ranges"][0]["end"] = 0x300
        outside["digest"] = map_digest(outside)
        with self.assertRaisesRegex(SafetyAuthorityError, "not contained"):
            validate_document(outside)

        non_flash = json.loads(json.dumps(document))
        non_flash["erase_geometry"]["ranges"][0] = {
            "start": 0x100,
            "end": 0x180,
            "provenance": [f"source:{provider}"],
        }
        non_flash["digest"] = map_digest(non_flash)
        with self.assertRaisesRegex(SafetyAuthorityError, "flash evidence"):
            validate_document(non_flash)

    def test_conflicting_semantic_facts_fail_without_source_priority(self) -> None:
        source = self.root / "datasheet.txt"
        source.write_text("section 12", encoding="utf-8")
        layout = {
            "schema_version": 1,
            "board_id": "board-a",
            "regions": [
                {
                    "name": "application",
                    "role": "application",
                    "start": 0,
                    "end": 0x80,
                    "source_path": str(source),
                    "source_locator": "section 12",
                },
                {
                    "name": "boot",
                    "role": "bootloader",
                    "start": 0,
                    "end": 0x80,
                    "source_path": str(source),
                    "source_locator": "section 12",
                },
            ],
        }
        with self.assertRaisesRegex(SafetyAuthorityError, "Conflicting semantic roles"):
            build_document(
                board_id="board-a",
                handle=self.handle,
                regions_for=lambda _handle: self.regions,
                layout=layout,
            )

    def test_read_unknown_is_observational_but_write_requires_role(self) -> None:
        document = self._document()
        path = self.store.layout.safety_board("board-a") / "memory-map.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        profiles = SimpleNamespace(
            load=lambda _board_id: SimpleNamespace(
                safety_ref=path.relative_to(self.root).as_posix()
            )
        )
        authority = SafetyAuthority(
            self.store,
            cast(ProfileRepository, profiles),
            lambda _handle: self.regions,
            lambda _handle, _address, _width: 0,
        )

        observed = authority.require(
            "board-a", self.handle, 0x80, 4, "read", allow_unknown_read=True
        )
        self.assertTrue(cast(dict[str, object], observed)["unknown"])
        with self.assertRaisesRegex(SafetyAuthorityError, "role unknown"):
            authority.require("board-a", self.handle, 0x80, 4, "write", roles={"ordinary_ram"})

    def test_special_role_is_classified_as_destructive_not_permanently_refused(self) -> None:
        document = self._document()
        region = next(
            region
            for region in cast(list[dict[str, object]], document["regions"])
            if region["start"] == 0x400
        )
        region["role"] = "security"
        document["digest"] = map_digest(document)
        path = self.store.layout.safety_board("board-a") / "memory-map.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        profiles = SimpleNamespace(
            load=lambda _board_id: SimpleNamespace(
                safety_ref=path.relative_to(self.root).as_posix()
            )
        )
        authority = SafetyAuthority(
            self.store,
            cast(ProfileRepository, profiles),
            lambda _handle: self.regions,
            lambda _handle, _address, _width: 0,
        )
        classified = authority.classify_write("board-a", self.handle, 0x400, 4)
        self.assertEqual(classified["risk"], "destructive")


if __name__ == "__main__":
    unittest.main()
