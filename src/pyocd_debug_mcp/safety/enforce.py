"""Runtime enforcement for the single-file Safety Layer v2 authority."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pyocd_debug_mcp.safety.linker import (
    BuildArtifactSelection,
    BuildEvidence,
    BuildRole,
    LinkerEvidenceError,
    extract_build_evidence,
)
from pyocd_debug_mcp.safety.map_build import (
    SafetyMapDocument,
    SafetyMapError,
    SafetyMapRepository,
    require_reconciled_authority,
)
from pyocd_debug_mcp.safety.regions import (
    ActionCategory,
    AddressRange,
    Allowed,
    Refusal,
    RegionKind,
    SafetyMap,
)


class SafetyPolicyError(RuntimeError):
    """A Layer-2 request failed before any target mutation began."""

    def __init__(self, code: str, message: str, *, remedy: tuple[str, ...]) -> None:
        self.code = code
        self.remedy = remedy
        super().__init__(f"{message} Required remedy: {' then '.join(remedy)}.")


@dataclass(frozen=True, slots=True)
class LoadedSafetyMap:
    document: SafetyMapDocument
    safety_map: SafetyMap

    # Transitional spelling used by a few callers while the v2 migration is in
    # flight.  It returns the one map document, never a legacy artifact bundle.
    @property
    def artifacts(self) -> SafetyMapDocument:
        return self.document


class SafetyPolicy:
    def __init__(
        self,
        repository: SafetyMapRepository,
        *,
        live_inputs: object | None = None,
        authority_verifier: object | None = None,
    ) -> None:
        # ``live_inputs`` is accepted only to keep old composition roots importable
        # during the migration. Build artifacts are deliberately not currentness.
        del live_inputs
        self.repository = repository
        self.authority_verifier = authority_verifier or require_reconciled_authority

    def load(self, board_id: str) -> LoadedSafetyMap:
        try:
            document = self.repository.load_current(board_id)
            self.authority_verifier(document)  # type: ignore[operator]
        except (SafetyMapError, ValueError) as exc:
            raise SafetyPolicyError(
                "safety/map-refresh-required",
                f"Board '{board_id}' has no complete current safety map: {exc}",
                remedy=("board_safety_refresh",),
            ) from exc
        return LoadedSafetyMap(document, document.safety_map)

    def current_aggregate(self, board_id: str) -> str:
        """Return the in-memory canonical map digest used by the run-scoped gate."""

        return self.load(board_id).document.canonical_digest

    def check_range(
        self,
        board_id: str,
        action: ActionCategory,
        requested: AddressRange,
    ) -> Allowed:
        result = self.load(board_id).safety_map.check(action, (requested,))
        if isinstance(result, Refusal):
            raise SafetyPolicyError(
                result.code,
                result.reason,
                remedy=("board_safety_refresh",),
            )
        return result

    def check_memory_write(self, board_id: str, address: int, width_bits: int) -> Allowed:
        return self.check_range(
            board_id,
            ActionCategory.MEMORY_WRITE,
            AddressRange.from_start_size(address, width_bits // 8),
        )

    def check_memory_read(self, board_id: str, address: int, size_bytes: int) -> Allowed:
        requested = AddressRange.from_start_size(address, size_bytes)
        result = self.load(board_id).safety_map.check(ActionCategory.MEMORY_READ, (requested,))
        if isinstance(result, Refusal):
            remedy = (
                ("choose a mapped, non-prohibited address",)
                if result.classification is RegionKind.PROHIBITED
                else ("board_safety_refresh",)
            )
            raise SafetyPolicyError(
                result.code,
                f"Memory-read range {requested.to_document()} has region kind "
                f"'{result.classification.value}': {result.reason}",
                remedy=remedy,
            )
        return result

    def check_register_write(self, board_id: str, address: int) -> Allowed:
        return self.check_range(
            board_id,
            ActionCategory.REGISTER_WRITE,
            AddressRange.from_start_size(address, 4),
        )

    def check_breakpoint(self, board_id: str, address: int, elf_path: Path) -> Allowed:
        """Require both stable partition authority and current-ELF executable evidence."""

        loaded = self.load(board_id)
        requested = AddressRange.from_start_size(address, 2)
        partition_result = loaded.safety_map.check(ActionCategory.FLASH_APPLICATION, (requested,))
        if isinstance(partition_result, Refusal):
            # A bootloader breakpoint is also valid when the reviewed map exposes
            # that partition. It is still checked against this call's ELF below.
            partition_result = loaded.safety_map.check(
                ActionCategory.FLASH_BOOTLOADER, (requested,)
            )
        if isinstance(partition_result, Refusal):
            raise SafetyPolicyError(
                "safety/breakpoint-outside-partition",
                "The breakpoint is outside every reviewed executable deployment partition.",
                remedy=("board_safety_refresh",),
            )
        evidence = self._extract_runtime_evidence(BuildRole.APPLICATION, elf_path)
        executable = tuple(
            segment.runtime_range for segment in evidence.loadable_segments if segment.executable
        )
        if not any(region.contains(requested) for region in executable):
            raise SafetyPolicyError(
                "safety/not-executable",
                "The breakpoint is not inside a loadable executable section of the current ELF.",
                remedy=("select_current_elf",),
            )
        return Allowed(
            ActionCategory.BREAKPOINT,
            (requested,),
            partition_result.classifications,
        )

    def check_flash(
        self,
        board_id: str,
        role: BuildRole,
        artifact_path: Path,
        *,
        current_target: str,
    ) -> BuildEvidence:
        loaded = self.load(board_id)
        expected_target = loaded.document.identity.pyocd_target
        if current_target != expected_target:
            raise SafetyPolicyError(
                "safety/target-mismatch",
                f"Live target '{current_target}' does not exactly match reviewed map target "
                f"'{expected_target}'.",
                remedy=("correct_board_assignment", "board_validate"),
            )
        partition = (
            loaded.document.partitions.application
            if role is BuildRole.APPLICATION
            else loaded.document.partitions.bootloader
        )
        if partition is None:
            raise SafetyPolicyError(
                "safety/partition-authority-unavailable",
                f"The reviewed map has no authoritative {role.value} partition.",
                remedy=("board_safety_refresh",),
            )

        artifact = artifact_path.expanduser().resolve()
        evidence = self._extract_runtime_evidence(role, artifact)
        action = (
            ActionCategory.FLASH_APPLICATION
            if role is BuildRole.APPLICATION
            else ActionCategory.FLASH_BOOTLOADER
        )
        if evidence.initial_stack_pointer is None or evidence.initial_stack_pointer < 4:
            raise SafetyPolicyError(
                "safety/vector-stack-invalid",
                "The ELF has no usable Cortex-M initial stack pointer.",
                remedy=("select_valid_build_artifact",),
            )
        stack_word = AddressRange.from_start_size(evidence.initial_stack_pointer - 4, 4)
        stack_result = loaded.safety_map.check(ActionCategory.MEMORY_WRITE, (stack_word,))
        if isinstance(stack_result, Refusal):
            raise SafetyPolicyError(
                "safety/vector-stack-outside-ram",
                "The vector-table initial stack pointer is outside reviewed writable RAM.",
                remedy=("select_correct_build",),
            )
        if evidence.reset_handler is None:
            raise SafetyPolicyError(
                "safety/vector-reset-invalid",
                "The ELF has no usable Cortex-M reset handler.",
                remedy=("select_valid_build_artifact",),
            )
        reset_range = AddressRange.from_start_size(evidence.reset_handler, 2)
        reset_result = loaded.safety_map.check(action, (reset_range,))
        if isinstance(reset_result, Refusal):
            raise SafetyPolicyError(
                "safety/vector-reset-outside-partition",
                "The vector-table reset handler is outside the reviewed deployment partition.",
                remedy=("select_correct_build",),
            )
        content_ranges = tuple(
            segment.load_range
            for segment in evidence.loadable_segments
            if segment.load_range is not None
        ) + tuple(evidence.hex_ranges)
        if not content_ranges:
            raise SafetyPolicyError(
                "safety/flash-content-missing",
                "The selected artifact contains no loadable flash content.",
                remedy=("select_valid_build_artifact",),
            )
        ranges: list[AddressRange] = list(content_ranges)
        if evidence.flash_partition is not None:
            ranges.append(evidence.flash_partition)
        if evidence.entry_point is not None:
            ranges.append(AddressRange.from_start_size(evidence.entry_point, 1))
        if evidence.vector_table is not None:
            ranges.append(AddressRange.from_start_size(evidence.vector_table, 8))
        for requested in ranges:
            result = loaded.safety_map.check(action, (requested,))
            if isinstance(result, Refusal):
                raise SafetyPolicyError(
                    "safety/flash-outside-partition",
                    f"Flash artifact range {requested.to_document()} is not fully inside the "
                    f"reviewed {role.value} partition: {result.reason}",
                    remedy=("select_correct_build",),
                )
        for sector in self._erase_sectors(loaded.document, content_ranges):
            result = loaded.safety_map.check(action, (sector,))
            if isinstance(result, Refusal):
                raise SafetyPolicyError(
                    "safety/erase-sector-outside-partition",
                    f"Required erase sector {sector.to_document()} exits the reviewed partition.",
                    remedy=("select_correct_build",),
                )
        return evidence

    def _extract_runtime_evidence(self, role: BuildRole, artifact: Path) -> BuildEvidence:
        if artifact.suffix.casefold() == ".elf":
            elf_path, hex_path = artifact, None
        elif artifact.suffix.casefold() == ".hex":
            elf_path, hex_path = artifact.with_suffix(".elf"), artifact
            if not elf_path.is_file():
                raise SafetyPolicyError(
                    "safety/hex-elf-companion-required",
                    "HEX flashing requires a same-build, same-stem ELF companion.",
                    remedy=("collect_matching_elf_and_hex",),
                )
        else:
            raise SafetyPolicyError(
                "safety/flash-artifact-type",
                "Safety extraction requires an ELF or HEX artifact.",
                remedy=("select_valid_build_artifact",),
            )
        try:
            return extract_build_evidence(
                BuildArtifactSelection(f"runtime_{role.value}", role, elf_path, None, hex_path)
            )
        except LinkerEvidenceError as exc:
            raise SafetyPolicyError(
                exc.code,
                str(exc),
                remedy=("select_valid_build_artifact",),
            ) from exc

    def _erase_sectors(
        self,
        document: SafetyMapDocument,
        ranges: Sequence[AddressRange],
    ) -> tuple[AddressRange, ...]:
        geometry = document.geometry
        if geometry.erase_sectors:
            explicit_sectors = tuple(item.address_range for item in geometry.erase_sectors)
            required = {
                sector
                for sector in explicit_sectors
                for requested in ranges
                if sector.overlaps(requested)
            }
            if any(not _fully_covered(requested, explicit_sectors) for requested in ranges):
                raise SafetyPolicyError(
                    "safety/geometry-incomplete",
                    "Reviewed erase geometry does not cover every flash range.",
                    remedy=("board_safety_refresh",),
                )
            return tuple(sorted(required))
        assert geometry.erase_origin is not None and geometry.erase_size is not None
        sectors: set[AddressRange] = set()
        for requested in ranges:
            if requested.start < geometry.erase_origin:
                raise SafetyPolicyError(
                    "safety/geometry-incomplete",
                    "A flash range precedes the reviewed erase geometry origin.",
                    remedy=("board_safety_refresh",),
                )
            first = geometry.erase_origin + (
                (requested.start - geometry.erase_origin) // geometry.erase_size
            ) * geometry.erase_size
            cursor = first
            while cursor < requested.end:
                sectors.add(AddressRange.from_start_size(cursor, geometry.erase_size))
                cursor += geometry.erase_size
        return tuple(sorted(sectors))


def _fully_covered(requested: AddressRange, sectors: Sequence[AddressRange]) -> bool:
    cursor = requested.start
    for sector in sorted(sectors):
        if sector.end <= cursor or sector.start >= requested.end:
            continue
        if sector.start > cursor:
            return False
        cursor = max(cursor, sector.end)
        if cursor >= requested.end:
            return True
    return False
