"""Runtime stable-map and per-artifact containment enforcement for Layer 2."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pyocd_debug_mcp.safety.artifact_evidence import extract_artifact_evidence
from pyocd_debug_mcp.safety.linker import (
    BuildEvidence,
    BuildRole,
    LinkerEvidenceError,
)
from pyocd_debug_mcp.safety.map_build import (
    SafetyArtifactError,
    SafetyArtifactRepository,
    SafetyArtifacts,
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
    artifacts: SafetyArtifacts
    safety_map: SafetyMap


AuthorityVerifier = Callable[[SafetyArtifacts], None]


class SafetyPolicy:
    def __init__(
        self,
        repository: SafetyArtifactRepository,
        *,
        live_inputs: object | None = None,
        authority_verifier: AuthorityVerifier = require_reconciled_authority,
    ) -> None:
        self.repository = repository
        # Kept as a source-compatible constructor argument while v1 callers are removed. V2
        # currentness is the canonical single-map digest, not tracked build files.
        self.live_inputs = live_inputs
        self.authority_verifier = authority_verifier

    def load(self, board_id: str) -> LoadedSafetyMap:
        try:
            artifacts = self.repository.load_current(board_id)
        except (SafetyArtifactError, ValueError) as exc:
            raise SafetyPolicyError(
                "safety/refresh-required",
                f"Board '{board_id}' has no complete consistent safety map: {exc}",
                remedy=("board_safety_refresh",),
            ) from exc
        try:
            self.authority_verifier(artifacts)
        except SafetyArtifactError as exc:
            raise SafetyPolicyError(
                "safety/refresh-required",
                f"Board '{board_id}' safety map is obsolete or incomplete: {exc}",
                remedy=("board_safety_refresh",),
            ) from exc
        return LoadedSafetyMap(
            artifacts,
            SafetyMap([item.region for item in artifacts.regions]),
        )

    def current_aggregate(self, board_id: str) -> str:
        loaded = self.load(board_id)
        return loaded.artifacts.map_digest

    def check_range(
        self,
        board_id: str,
        action: ActionCategory,
        requested: AddressRange,
    ) -> Allowed:
        result = self.load(board_id).safety_map.check(action, (requested,))
        if isinstance(result, Refusal):
            if result.classification is RegionKind.UNKNOWN:
                remedy = ("board_safety_refresh",)
            elif result.classification is RegionKind.PROHIBITED:
                remedy = ("choose a mapped, non-prohibited address",)
            else:
                remedy = ("choose an address appropriate for this action",)
            raise SafetyPolicyError(
                result.code,
                result.reason,
                remedy=remedy,
            )
        return result

    def check_memory_write(self, board_id: str, address: int, width_bits: int) -> Allowed:
        return self.check_range(
            board_id,
            ActionCategory.MEMORY_WRITE,
            AddressRange.from_start_size(address, width_bits // 8),
        )

    def check_memory_read(self, board_id: str, address: int, size_bytes: int) -> Allowed:
        """Require the exact bytes read to be mapped and non-prohibited."""

        requested = AddressRange.from_start_size(address, size_bytes)
        result = self.load(board_id).safety_map.check(
            ActionCategory.MEMORY_READ,
            (requested,),
        )
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

    def check_register_write(
        self, board_id: str, address: int, size_bytes: int = 4
    ) -> Allowed:
        return self.check_range(
            board_id,
            ActionCategory.REGISTER_WRITE,
            AddressRange.from_start_size(address, size_bytes),
        )

    def check_breakpoint(
        self,
        board_id: str,
        address: int,
        artifact_path: Path,
        memory_span_bytes: int,
    ) -> Allowed:
        """Authorize a breakpoint only from the current ELF's executable segments."""

        loaded = self.load(board_id)
        requested = AddressRange.from_start_size(address, memory_span_bytes)
        classification = loaded.safety_map.classify(requested)
        if classification is RegionKind.PROHIBITED:
            raise SafetyPolicyError(
                "safety/prohibited",
                "The breakpoint touches a prohibited security or provisioning region.",
                remedy=("choose_another_breakpoint",),
            )
        if classification not in {
            RegionKind.APPLICATION_FLASH,
            RegionKind.BOOTLOADER_FLASH,
            RegionKind.ROM,
        }:
            raise SafetyPolicyError(
                "safety/breakpoint-wrong-region",
                f"The breakpoint is in mapped region kind '{classification.value}', not code space.",
                remedy=("choose_another_breakpoint",),
            )
        artifact = artifact_path.expanduser().resolve()
        try:
            evidence = extract_artifact_evidence(
                artifact, BuildRole.APPLICATION, require_vector_table=False
            )
        except LinkerEvidenceError as exc:
            raise SafetyPolicyError(
                exc.code,
                str(exc),
                remedy=("select_current_executable_artifact",),
            ) from exc
        executable_ranges = tuple(
            segment.runtime_range
            for segment in evidence.loadable_segments
            if segment.executable
        )
        if not any(item.contains(requested) for item in executable_ranges):
            raise SafetyPolicyError(
                "safety/not-executable",
                "The breakpoint is outside every executable range in the current artifact.",
                remedy=("choose_another_breakpoint",),
            )
        return Allowed(ActionCategory.BREAKPOINT, (requested,), (classification,))

    def check_flash(
        self,
        board_id: str,
        role: BuildRole,
        artifact_path: Path,
        *,
        current_target: str,
    ) -> BuildEvidence:
        loaded = self.load(board_id)
        expected_target = self._expected_target(loaded.artifacts)
        if current_target != expected_target:
            raise SafetyPolicyError(
                "safety/target-mismatch",
                f"Connected target '{current_target}' does not exactly match '{expected_target}'.",
                remedy=("correct_board_assignment", "board_validate"),
            )
        artifact = artifact_path.expanduser().resolve()
        try:
            evidence = extract_artifact_evidence(artifact, role)
        except LinkerEvidenceError as exc:
            raise SafetyPolicyError(
                exc.code,
                str(exc),
                remedy=("select_valid_build_artifact",),
            ) from exc
        action = (
            ActionCategory.FLASH_APPLICATION
            if role is BuildRole.APPLICATION
            else ActionCategory.FLASH_BOOTLOADER
        )
        content_ranges = [
            segment.load_range
            for segment in evidence.loadable_segments
            if segment.load_range is not None
        ]
        content_ranges.extend(evidence.hex_ranges)
        if not content_ranges:
            raise SafetyPolicyError(
                "safety/flash-content-missing",
                "The selected artifact contains no loadable flash content.",
                remedy=("select_valid_build_artifact",),
            )
        ranges = [evidence.flash_partition] if evidence.flash_partition is not None else []
        ranges.extend(content_ranges)
        if evidence.entry_point is not None:
            ranges.append(AddressRange.from_start_size(evidence.entry_point, 1))
        if evidence.vector_table is not None:
            ranges.append(AddressRange.from_start_size(evidence.vector_table, 1))
        for requested in ranges:
            result = loaded.safety_map.check(action, (requested,))
            if isinstance(result, Refusal):
                raise SafetyPolicyError(
                    "safety/flash-outside-partition",
                    f"Flash artifact range {requested.to_document()} is not fully contained: "
                    f"{result.reason}",
                    remedy=("select_correct_build",),
                )
        for sector in self._erase_sectors(loaded.artifacts, content_ranges):
            result = loaded.safety_map.check(action, (sector,))
            if isinstance(result, Refusal):
                raise SafetyPolicyError(
                    "safety/erase-sector-outside-partition",
                    f"Required erase sector {sector.to_document()} exits the mapped partition.",
                    remedy=("select_correct_build",),
                )
        return evidence

    def _expected_target(self, artifacts: SafetyArtifacts) -> str:
        target = artifacts.identity.get("target")
        if not isinstance(target, str) or not target.strip():
            raise SafetyPolicyError(
                "safety/target-evidence-missing",
                "The memory map has no exact target identity.",
                remedy=("board_safety_refresh",),
            )
        return target.strip()

    def _erase_sectors(
        self,
        artifacts: SafetyArtifacts,
        ranges: Sequence[AddressRange],
    ) -> tuple[AddressRange, ...]:
        geometry = artifacts.geometry
        explicit = geometry.get("sectors")
        if isinstance(explicit, list):
            sectors: list[AddressRange] = []
            for row in explicit:
                if not isinstance(row, Mapping):
                    raise SafetyPolicyError(
                        "safety/geometry-invalid",
                        "Erase-sector entries must be objects.",
                        remedy=("board_safety_refresh",),
                    )
                try:
                    sectors.append(AddressRange(row["start"], row["end"]))  # type: ignore[arg-type]
                except (KeyError, TypeError, ValueError) as exc:
                    raise SafetyPolicyError(
                        "safety/geometry-invalid",
                        f"Invalid erase-sector entry: {exc}",
                        remedy=("board_safety_refresh",),
                    ) from exc
            required = {
                sector for sector in sectors for requested in ranges if sector.overlaps(requested)
            }
            if any(not _fully_covered(requested, sectors) for requested in ranges):
                raise SafetyPolicyError(
                    "safety/geometry-incomplete",
                    "Erase geometry does not cover every flash range.",
                    remedy=("board_safety_refresh",),
                )
            return tuple(sorted(required))
        erase_size = geometry.get("erase_size")
        origin = geometry.get("erase_origin", 0)
        if (
            isinstance(erase_size, bool)
            or not isinstance(erase_size, int)
            or erase_size <= 0
            or isinstance(origin, bool)
            or not isinstance(origin, int)
            or origin < 0
        ):
            raise SafetyPolicyError(
                "safety/geometry-missing",
                "Uniform geometry requires positive erase_size and non-negative erase_origin.",
                remedy=("board_safety_refresh",),
            )
        uniform_sectors: set[AddressRange] = set()
        for requested in ranges:
            if requested.start < origin:
                raise SafetyPolicyError(
                    "safety/geometry-incomplete",
                    "A flash range precedes the erase geometry origin.",
                    remedy=("board_safety_refresh",),
                )
            first = origin + ((requested.start - origin) // erase_size) * erase_size
            cursor = first
            while cursor < requested.end:
                uniform_sectors.add(AddressRange.from_start_size(cursor, erase_size))
                cursor += erase_size
        return tuple(sorted(uniform_sectors))

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
