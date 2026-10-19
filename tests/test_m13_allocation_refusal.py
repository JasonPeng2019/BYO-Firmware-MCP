"""M13 branch-level regressions for missing-allocation breakpoint refusals.

These exercise the real ``SafetyPolicy.check_breakpoint`` against real schema-v2
and schema-v3 safety documents.  They cover the branch-level distinction the
module owns (missing deployment allocation vs. genuine outside-existing-
allocation vs. a map a refresh can repair) and the zero-side-effect property of
the refusal.  The full public no-allocation -> authorized deployment -> same
breakpoint succeeds workflow, the refresh route, and both-boards physical proof
belong to the shared B02-T independent validator.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.safety.enforce import SafetyPolicy, SafetyPolicyError
from pyocd_debug_mcp.safety.map_build import (
    GenericMapGeometry,
    GenericMapIdentity,
    GenericSafetyMapDocument,
    GenericSourceDigests,
    MapGeometry,
    EraseSector,
    MapIdentity,
    MapPartitions,
    SafetyMapDocument,
    SourceDigests,
    build_artifact_application_allocation,
    generic_map_with_allocation,
)
from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionKind,
    SafetyRegion,
    SourceAuthority,
)

_FLASH = AddressRange(0, 0x4000)
_RAM = AddressRange(0x20000000, 0x20001000)
_SECTORS = (
    EraseSector(AddressRange(0x0000, 0x1000), "bank0"),
    EraseSector(AddressRange(0x1000, 0x2000), "bank0"),
    EraseSector(AddressRange(0x2000, 0x3000), "bank1"),
    EraseSector(AddressRange(0x3000, 0x4000), "bank1"),
)
_SUPPORT_DIGEST = "a" * 64
_PROVENANCE = (Provenance(SourceAuthority.DEVICE_SUPPORT, "fixture", "m13 regression"),)


class _RecordingRepository:
    """Record every safety-map access so a refusal cannot write authority."""

    def __init__(self) -> None:
        self.loads: list[str] = []
        self.writes: list[Any] = []

    def load_current(self, board_id: str) -> Any:  # pragma: no cover - never reached
        self.loads.append(board_id)
        raise AssertionError("a refusal must not need a repository read")

    def commit(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        self.writes.append((args, kwargs))
        raise AssertionError("a refusal must never commit safety authority")

    def commit_if_current(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        self.writes.append((args, kwargs))
        raise AssertionError("a refusal must never commit safety authority")


def _generic_document(
    *,
    board_id: str = "generic_board",
    deployment_policy: dict[str, object] | None = None,
    application: AddressRange | None = None,
) -> GenericSafetyMapDocument:
    """Build one real schema-v3 generic map with an explicit deployment policy."""

    policy: dict[str, object] = dict(deployment_policy or {"kind": "none"})
    regions = [
        SafetyRegion("physical flash", RegionKind.PHYSICAL_FLASH, _FLASH, _PROVENANCE),
        SafetyRegion("physical RAM", RegionKind.PHYSICAL_RAM, _RAM, _PROVENANCE),
    ]
    if application is not None:
        regions.append(
            SafetyRegion(
                "artifact-defined application allocation",
                RegionKind.APPLICATION_FLASH,
                application,
                (
                    Provenance(
                        SourceAuthority.DERIVED,
                        "derived-allocation",
                        "server-derived artifact erase-sector allocation",
                    ),
                ),
            )
        )
    return GenericSafetyMapDocument(
        board_id,
        GenericMapIdentity("MOCK1234", "mock_target", _SUPPORT_DIGEST),
        {
            "kind": "resolved_pack",
            "support_id": _SUPPORT_DIGEST,
            "pyocd_target": "mock_target",
            "pack_id": "Mock.Pack",
            "pack_filename": "Mock.Pack.pack",
            "pack_sha256": "b" * 64,
            "pdsc_device": "MOCK1234",
        },
        GenericSourceDigests("c" * 64, "d" * 64, "e" * 64, "f" * 64, "0" * 64),
        GenericMapGeometry((_FLASH,), (_RAM,), _SECTORS),
        MapPartitions(application),
        policy,
        tuple(regions),
    )


def _reviewed_document(*, board_id: str = "reviewed_board") -> SafetyMapDocument:
    """Build one real schema-v2 reviewed map that grants no deployment partition."""

    return SafetyMapDocument(
        board_id,
        MapIdentity("MOCK1234", "mock_target", "mock_board"),
        SourceDigests("c" * 64, "d" * 64, "e" * 64, "0" * 64),
        MapGeometry(_FLASH, _RAM, erase_origin=0, erase_size=0x1000),
        MapPartitions(None),
        (
            SafetyRegion("physical flash", RegionKind.PHYSICAL_FLASH, _FLASH, _PROVENANCE),
            SafetyRegion("physical RAM", RegionKind.PHYSICAL_RAM, _RAM, _PROVENANCE),
        ),
    )


class MissingAllocationRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = _RecordingRepository()
        self.policy = SafetyPolicy(self.repository)  # type: ignore[arg-type]
        # The partition decision precedes ELF extraction, so the artifact is
        # deliberately absent: reaching it would be a different failure.
        self.elf = Path("definitely-not-present.elf")

    def _refusal(self, document: Any, address: int) -> SafetyPolicyError:
        with self.assertRaises(SafetyPolicyError) as raised:
            self.policy.check_breakpoint(
                document.board_id, address, self.elf, document=document
            )
        return raised.exception

    def test_generic_map_without_allocation_names_the_deployment_route(self) -> None:
        """M13: no allocation must point at the eligible guarded deployment route."""

        document = _generic_document()
        self.assertIsNone(document.partitions.application)
        self.assertIsNone(document.partitions.bootloader)
        self.assertEqual(document.deployment_policy, {"kind": "none"})

        error = self._refusal(document, 0x3A4)

        self.assertEqual(error.code, "safety/no-deployment-allocation")
        self.assertEqual(
            error.remedy, ("flash_application-plan", "flash_application"), error.remedy
        )
        self.assertNotIn("board_safety_refresh", error.remedy)
        self.assertIn("flash_application-plan", str(error))
        self.assertIn("flash_application", str(error))
        # The refusal must state the prerequisites the product actually enforces.
        self.assertIn("gate", str(error))

    def test_refusal_neither_allocates_nor_mutates_map_authority(self) -> None:
        """M13: a refusal performs zero authority-visible work and cannot allocate."""

        document = _generic_document()
        before = document.canonical_digest

        first = self._refusal(document, 0x3A4)
        # Replaying the unchanged map is what a refresh of unchanged sources
        # yields; it must repeat the refusal instead of granting authority.
        second = self._refusal(document, 0x3A4)

        self.assertEqual(first.code, second.code)
        self.assertEqual(first.remedy, second.remedy)
        self.assertEqual(document.canonical_digest, before)
        self.assertIsNone(document.partitions.application)
        self.assertEqual(document.deployment_policy, {"kind": "none"})
        self.assertEqual(self.repository.writes, [])
        self.assertEqual(self.repository.loads, [])

    def test_generic_map_with_allocation_keeps_the_outside_partition_refusal(self) -> None:
        """M13: an existing allocation out of range keeps the stable-map refusal."""

        base = _generic_document()
        allocation = build_artifact_application_allocation(
            (_SECTORS[1].address_range,),
            driver_proof_digest="1" * 64,
            creation_map_digest=base.canonical_digest,
            creation_artifact_digest="2" * 64,
        )
        allocated = generic_map_with_allocation(base, allocation)
        self.assertEqual(allocated.partitions.application, AddressRange(0x1000, 0x2000))

        error = self._refusal(allocated, 0x2000)

        self.assertEqual(error.code, "safety/breakpoint-outside-partition")
        self.assertEqual(error.remedy, ("board_safety_refresh",), error.remedy)

    def test_reviewed_map_without_partition_keeps_the_refresh_remedy(self) -> None:
        """M13: a non-generic map keeps refresh, which can still replay authority.

        Only a replayable generic map can obtain an artifact-derived allocation;
        pointing a reviewed map at flash_application would be false guidance.
        """

        document = _reviewed_document()
        self.assertIsNone(document.partitions.application)

        error = self._refusal(document, 0x400)

        self.assertEqual(error.code, "safety/breakpoint-outside-partition")
        self.assertEqual(error.remedy, ("board_safety_refresh",), error.remedy)


if __name__ == "__main__":
    unittest.main()
