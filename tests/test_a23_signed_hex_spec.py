"""Adversarial specification for connected deployment-HEX companion evidence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pyocd_debug_mcp.safety.enforce import LoadedSafetyMap, SafetyPolicy, SafetyPolicyError
from pyocd_debug_mcp.safety.linker import (
    BuildArtifactSelection,
    BuildRole,
    LinkerEvidenceError,
    LoadableSegment,
    extract_build_evidence,
)
from pyocd_debug_mcp.safety.map_build import (
    EraseSector,
    GenericMapGeometry,
    GenericSafetyMapDocument,
)
from pyocd_debug_mcp.safety.regions import (
    ActionCategory,
    AddressRange,
    Allowed,
    Refusal,
    RegionKind,
)


_BASE = 0x4000
_ELF_IMAGE = bytes(
    (0x00, 0x10, 0x00, 0x20, 0x09, 0x40, 0x00, 0x00, 0x41, 0x42, 0x00, 0x44, 0x45, 0x46, 0x47, 0x48)
)


def _hex_record(address: int, data: bytes) -> str:
    payload = bytes((len(data),)) + address.to_bytes(2, "big") + b"\x00" + data
    return ":" + (payload + bytes([(-sum(payload)) & 0xFF])).hex().upper()


def _write_hex(path: Path, chunks: list[tuple[int, bytes]]) -> None:
    path.write_text(
        "\n".join([*(_hex_record(address, data) for address, data in chunks), ":00000001FF"]),
        encoding="ascii",
    )


def _parsed_elf(
    _path: Path, image: bytes = _ELF_IMAGE
) -> tuple[dict[str, int], tuple[LoadableSegment, ...], int, dict[int, int]]:
    load_range = AddressRange(_BASE, _BASE + len(image))
    return (
        {"__app_partition_start": _BASE, "__app_partition_end": _BASE + len(image)},
        (LoadableSegment(0, load_range, load_range, len(image), len(image), True, False, True),),
        _BASE + 9,
        dict(zip(range(_BASE, _BASE + len(image)), image)),
    )


class ConnectedDeploymentHexSpecTests(unittest.TestCase):
    def _extract(self, chunks: list[tuple[int, bytes]], *, elf_image: bytes = _ELF_IMAGE):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            elf_path = root / "deployment.elf"
            hex_path = root / "deployment.hex"
            elf_path.write_bytes(b"parsed through patched reader")
            _write_hex(hex_path, chunks)
            with patch(
                "pyocd_debug_mcp.safety.linker._read_elf",
                side_effect=lambda path: _parsed_elf(path, elf_image),
            ):
                return extract_build_evidence(
                    BuildArtifactSelection("a23", BuildRole.APPLICATION, elf_path, None, hex_path)
                )

    def test_connected_prefix_suffix_and_cross_record_wrapper_are_preserved_exactly(self) -> None:
        prefix = b"\xaa\xbb"
        suffix = b"\xcc\xdd\xee"
        evidence = self._extract(
            [
                (_BASE - len(prefix), prefix),
                (_BASE, _ELF_IMAGE[:7]),
                (_BASE + 7, _ELF_IMAGE[7:]),
                (_BASE + len(_ELF_IMAGE), suffix),
            ]
        )

        self.assertEqual(
            evidence.hex_ranges,
            (AddressRange(_BASE - len(prefix), _BASE + len(_ELF_IMAGE) + len(suffix)),),
        )

    def test_connected_prefix_and_suffix_each_pass_without_an_elf_subset(self) -> None:
        for wrapper in (
            [(_BASE - 1, b"\xa5"), (_BASE, _ELF_IMAGE)],
            [(_BASE, _ELF_IMAGE), (_BASE + len(_ELF_IMAGE), b"\x5a")],
        ):
            with self.subTest(wrapper=wrapper):
                self._extract(wrapper)

    def test_fill_representation_is_symmetric_but_no_other_overlap_difference_is_allowed(
        self,
    ) -> None:
        fill_changed = bytearray(_ELF_IMAGE)
        fill_changed[10] = 0xFF
        self._extract([(_BASE, bytes(fill_changed))])
        self._extract([(_BASE, _ELF_IMAGE)], elf_image=bytes(fill_changed))

        for replacement in (0x01,):
            conflict = bytearray(_ELF_IMAGE)
            conflict[10] = replacement
            with self.subTest(elf_value=0, hex_value=replacement):
                with self.assertRaises(LinkerEvidenceError) as raised:
                    self._extract([(_BASE, bytes(conflict))])
                self.assertEqual(raised.exception.code, "build/hex-content-conflict")

            conflict[10] = 0x00
            conflict[11] = replacement
            with self.subTest(elf_value=0x44, hex_value=replacement):
                with self.assertRaises(LinkerEvidenceError) as raised:
                    self._extract([(_BASE, bytes(conflict))])
                self.assertEqual(raised.exception.code, "build/hex-content-conflict")

    def test_meaningful_omission_and_one_byte_disconnected_component_refuse_precisely(self) -> None:
        with self.assertRaises(LinkerEvidenceError) as incomplete:
            self._extract([(_BASE, _ELF_IMAGE[:8]), (_BASE + 9, _ELF_IMAGE[9:])])
        self.assertEqual(incomplete.exception.code, "build/hex-incomplete")
        self.assertIn(f"0x{_BASE + 8:x}", str(incomplete.exception))

        with self.assertRaises(LinkerEvidenceError) as disconnected:
            self._extract([(_BASE, _ELF_IMAGE), (_BASE + len(_ELF_IMAGE) + 1, b"\x99")])
        self.assertEqual(disconnected.exception.code, "build/hex-disconnected-content")
        self.assertIn(f"0x{_BASE + len(_ELF_IMAGE) + 1:x}", str(disconnected.exception))


class _PolicyMap:
    def __init__(self, application: AddressRange) -> None:
        self.application = application

    def check(self, action: ActionCategory, ranges: tuple[AddressRange, ...]):
        requested = ranges[0]
        if action is ActionCategory.FLASH_APPLICATION and not self.application.contains(requested):
            return Refusal(
                action,
                "safety/unknown",
                "controlled test map refuses content outside application authority",
                requested,
                RegionKind.UNKNOWN,
                "refresh",
            )
        return Allowed(action, ranges, ())


class WrapperPolicyAuthoritySpecTests(ConnectedDeploymentHexSpecTests):
    def _wrapper_evidence(self):
        return self._extract(
            [(_BASE - 1, b"\xa5"), (_BASE, _ELF_IMAGE), (_BASE + len(_ELF_IMAGE), b"\x5a")]
        )

    def test_reviewed_partition_refuses_connected_wrapper_outside_its_authority(self) -> None:
        evidence = self._wrapper_evidence()
        application = AddressRange(_BASE, _BASE + len(_ELF_IMAGE))
        policy = SafetyPolicy(SimpleNamespace())
        document = SimpleNamespace(
            identity=SimpleNamespace(pyocd_target="test-target"),
            partitions=SimpleNamespace(application=application, bootloader=None),
            geometry=SimpleNamespace(
                erase_available=True,
                erase_sectors=(),
                erase_origin=application.start,
                erase_size=application.size,
            ),
        )
        loaded = LoadedSafetyMap(document, _PolicyMap(application))

        with (
            patch.object(policy, "load", return_value=loaded),
            patch.object(policy, "_extract_runtime_evidence", return_value=evidence),
            self.assertRaises(SafetyPolicyError) as refused,
        ):
            policy.check_flash(
                "test-board",
                BuildRole.APPLICATION,
                Path("deployment.hex"),
                current_target="test-target",
            )

        self.assertEqual(refused.exception.code, "safety/flash-outside-partition")
        self.assertEqual(
            evidence.hex_ranges, (AddressRange(_BASE - 1, _BASE + len(_ELF_IMAGE) + 1),)
        )

    def test_generic_physical_flash_refuses_connected_wrapper_outside_verified_geometry(
        self,
    ) -> None:
        evidence = self._wrapper_evidence()
        application = AddressRange(_BASE, _BASE + len(_ELF_IMAGE))
        geometry = GenericMapGeometry(
            (application,),
            (AddressRange(0x20000000, 0x20002000),),
            (EraseSector(application, "test-bank"),),
        )
        document = object.__new__(GenericSafetyMapDocument)
        object.__setattr__(document, "identity", SimpleNamespace(pyocd_target="test-target"))
        object.__setattr__(document, "geometry", geometry)
        policy = SafetyPolicy(SimpleNamespace())
        loaded = LoadedSafetyMap(document, _PolicyMap(application))

        with (
            patch.object(policy, "load", return_value=loaded),
            patch.object(policy, "_extract_runtime_evidence", return_value=evidence),
            self.assertRaises(SafetyPolicyError) as refused,
        ):
            policy.check_generic_application_candidate(
                "test-board", Path("deployment.hex"), current_target="TEST-TARGET"
            )

        self.assertEqual(refused.exception.code, "safety/flash-outside-physical-device")

    def test_generic_physical_flash_accepts_connected_wrapper_and_derives_its_allocation(
        self,
    ) -> None:
        evidence = self._wrapper_evidence()
        wrapper = evidence.hex_ranges[0]
        split = _BASE + len(_ELF_IMAGE) // 2
        sectors = (
            EraseSector(AddressRange(wrapper.start, split), "test-bank"),
            EraseSector(AddressRange(split, wrapper.end), "test-bank"),
        )
        geometry = GenericMapGeometry(
            (wrapper,),
            (AddressRange(0x20000000, 0x20002000),),
            sectors,
        )
        document = object.__new__(GenericSafetyMapDocument)
        object.__setattr__(document, "identity", SimpleNamespace(pyocd_target="test-target"))
        object.__setattr__(document, "geometry", geometry)
        policy = SafetyPolicy(SimpleNamespace())
        loaded = LoadedSafetyMap(document, _PolicyMap(wrapper))

        with (
            patch.object(policy, "load", return_value=loaded),
            patch.object(policy, "_extract_runtime_evidence", return_value=evidence),
        ):
            accepted, allocation = policy.check_generic_application_candidate(
                "test-board", Path("deployment.hex"), current_target="TEST-TARGET"
            )

        self.assertIs(accepted, evidence)
        self.assertEqual(allocation, tuple(item.address_range for item in sectors))

    def test_same_stem_hex_relationship_failure_keeps_existing_runtime_remedy(self) -> None:
        policy = SafetyPolicy(SimpleNamespace())
        with tempfile.TemporaryDirectory() as directory:
            hex_path = Path(directory) / "deployment.hex"
            hex_path.write_text(":00000001FF\n", encoding="ascii")
            hex_path.with_suffix(".elf").write_bytes(b"placeholder")
            with (
                patch(
                    "pyocd_debug_mcp.safety.enforce.extract_build_evidence",
                    side_effect=LinkerEvidenceError(
                        "build/hex-disconnected-content", "disconnected"
                    ),
                ),
                self.assertRaises(SafetyPolicyError) as refused,
            ):
                policy._extract_runtime_evidence(BuildRole.APPLICATION, hex_path)

        self.assertEqual(refused.exception.code, "build/hex-disconnected-content")
        self.assertEqual(refused.exception.remedy, ("select_valid_build_artifact",))


if __name__ == "__main__":
    unittest.main()
