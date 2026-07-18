"""Runtime safety-map containment and freshness enforcement for Layer 2."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from pyocd_debug_mcp.safety.fingerprints import FingerprintInputs, FingerprintSet, FingerprintSource
from pyocd_debug_mcp.safety.linker import (
    BuildArtifactSelection,
    BuildEvidence,
    BuildRole,
    LinkerEvidenceError,
    extract_build_evidence,
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


LiveInputsProvider = Callable[[str, SafetyArtifacts], FingerprintInputs]
AuthorityVerifier = Callable[[SafetyArtifacts], None]


class SafetyPolicy:
    def __init__(
        self,
        repository: SafetyArtifactRepository,
        *,
        live_inputs: LiveInputsProvider | None = None,
        authority_verifier: AuthorityVerifier = require_reconciled_authority,
    ) -> None:
        self.repository = repository
        self.live_inputs = live_inputs
        self.authority_verifier = authority_verifier

    def load(self, board_id: str) -> LoadedSafetyMap:
        try:
            artifacts = self.repository.load_current(board_id)
        except (SafetyArtifactError, ValueError) as exc:
            raise SafetyPolicyError(
                "safety/setup-required",
                f"Board '{board_id}' has no complete consistent safety map: {exc}",
                remedy=("board_safety_setup", "board_validate"),
            ) from exc
        try:
            self.authority_verifier(artifacts)
        except SafetyArtifactError as exc:
            raise SafetyPolicyError(
                "safety/authority-migration-required",
                f"Board '{board_id}' safety authority is obsolete or incomplete: {exc}",
                remedy=("board_setup", "board_safety_setup", "board_validate"),
            ) from exc
        return LoadedSafetyMap(
            artifacts,
            SafetyMap([item.region for item in artifacts.regions]),
        )

    def current_aggregate(self, board_id: str) -> str:
        loaded = self.load(board_id)
        self._verify_declared_artifacts(loaded.artifacts)
        if self.live_inputs is not None:
            candidate = FingerprintSet.build(self.live_inputs(board_id, loaded.artifacts))
            changed = loaded.artifacts.fingerprints.changed_sources(candidate)
            if changed:
                anchor = FingerprintSource.PART_TARGET in changed
                structural = bool(
                    set(changed).intersection(
                        {FingerprintSource.GEOMETRY, FingerprintSource.SCHEMA}
                    )
                )
                remedy = (
                    ("board_safety_setup", "board_validate")
                    if anchor or structural
                    else ("board_safety_setup",)
                    if FingerprintSource.PROFILE in changed
                    else ("board_safety_refresh",)
                )
                raise SafetyPolicyError(
                    "safety/fingerprint-stale",
                    "Current safety inputs differ from the persisted aggregate for source groups "
                    + ", ".join(item.value for item in changed),
                    remedy=remedy,
                )
        return loaded.artifacts.fingerprints.aggregate

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
                remedy=("board_safety_setup",),
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
                else ("board_safety_setup",)
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

    def check_breakpoint(self, board_id: str, address: int) -> Allowed:
        return self.check_range(
            board_id,
            ActionCategory.BREAKPOINT,
            AddressRange.from_start_size(address, 2),
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
        expected_target = self._expected_target(loaded.artifacts)
        if current_target != expected_target:
            raise SafetyPolicyError(
                "safety/target-mismatch",
                f"Connected target '{current_target}' does not exactly match '{expected_target}'.",
                remedy=("correct_board_assignment", "board_validate"),
            )
        artifact = artifact_path.expanduser().resolve()
        self._require_fingerprinted_flash_artifact(loaded.artifacts, role, artifact)
        if artifact.suffix.casefold() == ".elf":
            elf_path = artifact
            hex_path = None
        elif artifact.suffix.casefold() == ".hex":
            elf_path = artifact.with_suffix(".elf")
            hex_path = artifact
        else:
            raise SafetyPolicyError(
                "safety/flash-artifact-type",
                "Safety extraction requires an ELF or HEX artifact.",
                remedy=("select_valid_build_artifact",),
            )
        # Do not silently add an adjacent linker map that was not selected and
        # fingerprinted during safety refresh.  The ELF is authoritative for
        # this call's partitions, segments, entry point, and vector table; an
        # unrelated or dialect-incompatible sibling map must not alter the
        # meaning of an already reviewed artifact.
        map_path = None
        try:
            evidence = extract_build_evidence(
                BuildArtifactSelection(
                    f"runtime_{role.value}",
                    role,
                    elf_path,
                    map_path,
                    hex_path,
                )
            )
        except LinkerEvidenceError as exc:
            raise SafetyPolicyError(
                exc.code,
                str(exc),
                remedy=("select_valid_build_artifact", "board_safety_refresh"),
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
                    remedy=("select_correct_build", "board_safety_refresh"),
                )
        for sector in self._erase_sectors(loaded.artifacts, content_ranges):
            result = loaded.safety_map.check(action, (sector,))
            if isinstance(result, Refusal):
                raise SafetyPolicyError(
                    "safety/erase-sector-outside-partition",
                    f"Required erase sector {sector.to_document()} exits the mapped partition.",
                    remedy=("select_correct_build", "board_safety_refresh"),
                )
        return evidence

    def _source_evidence(self, artifacts: SafetyArtifacts, source: FingerprintSource) -> object:
        sources = artifacts.source_manifest.get("sources")
        if not isinstance(sources, Mapping):
            raise SafetyPolicyError(
                "safety/source-manifest-invalid",
                "Safety source manifest is missing its source table.",
                remedy=("board_safety_setup",),
            )
        row = sources.get(source.value)
        if not isinstance(row, Mapping) or "evidence" not in row:
            raise SafetyPolicyError(
                "safety/source-manifest-invalid",
                f"Safety source manifest is missing {source.value} evidence.",
                remedy=("board_safety_setup",),
            )
        return row["evidence"]

    def _expected_target(self, artifacts: SafetyArtifacts) -> str:
        evidence = self._source_evidence(artifacts, FingerprintSource.PART_TARGET)
        if not isinstance(evidence, Mapping) or not isinstance(evidence.get("target"), str):
            raise SafetyPolicyError(
                "safety/target-evidence-missing",
                "Safety evidence has no exact target identity.",
                remedy=("board_safety_setup",),
            )
        target = str(evidence["target"]).strip()
        if not target:
            raise SafetyPolicyError(
                "safety/target-evidence-missing",
                "Safety evidence has an empty target identity.",
                remedy=("board_safety_setup",),
            )
        return target

    def _erase_sectors(
        self,
        artifacts: SafetyArtifacts,
        ranges: Sequence[AddressRange],
    ) -> tuple[AddressRange, ...]:
        geometry = self._source_evidence(artifacts, FingerprintSource.GEOMETRY)
        if not isinstance(geometry, Mapping):
            raise SafetyPolicyError(
                "safety/geometry-missing",
                "Flash erase geometry is not an object.",
                remedy=("board_safety_setup",),
            )
        explicit = geometry.get("sectors")
        if isinstance(explicit, list):
            sectors: list[AddressRange] = []
            for row in explicit:
                if not isinstance(row, Mapping):
                    raise SafetyPolicyError(
                        "safety/geometry-invalid",
                        "Erase-sector entries must be objects.",
                        remedy=("board_safety_setup",),
                    )
                try:
                    sectors.append(AddressRange(row["start"], row["end"]))  # type: ignore[arg-type]
                except (KeyError, TypeError, ValueError) as exc:
                    raise SafetyPolicyError(
                        "safety/geometry-invalid",
                        f"Invalid erase-sector entry: {exc}",
                        remedy=("board_safety_setup",),
                    ) from exc
            required = {
                sector for sector in sectors for requested in ranges if sector.overlaps(requested)
            }
            if any(not _fully_covered(requested, sectors) for requested in ranges):
                raise SafetyPolicyError(
                    "safety/geometry-incomplete",
                    "Erase geometry does not cover every flash range.",
                    remedy=("board_safety_setup",),
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
                remedy=("board_safety_setup",),
            )
        uniform_sectors: set[AddressRange] = set()
        for requested in ranges:
            if requested.start < origin:
                raise SafetyPolicyError(
                    "safety/geometry-incomplete",
                    "A flash range precedes the erase geometry origin.",
                    remedy=("board_safety_setup",),
                )
            first = origin + ((requested.start - origin) // erase_size) * erase_size
            cursor = first
            while cursor < requested.end:
                uniform_sectors.add(AddressRange.from_start_size(cursor, erase_size))
                cursor += erase_size
        return tuple(sorted(uniform_sectors))

    def _verify_declared_artifacts(self, artifacts: SafetyArtifacts) -> None:
        for source in FingerprintSource:
            evidence = self._source_evidence(artifacts, source)
            for path_value, digest in _artifact_records(evidence):
                path = Path(path_value)
                if not path.is_absolute():
                    path = self.repository.store.layout.project_root / path
                if not path.is_file() or sha256(path.read_bytes()).hexdigest() != digest:
                    raise SafetyPolicyError(
                        "safety/artifact-stale",
                        f"{source.value} artifact '{path}' changed or disappeared.",
                        remedy=("board_safety_refresh",),
                    )

    def _require_fingerprinted_flash_artifact(
        self,
        artifacts: SafetyArtifacts,
        role: BuildRole,
        artifact: Path,
    ) -> None:
        source = (
            FingerprintSource.APPLICATION_ARTIFACTS
            if role is BuildRole.APPLICATION
            else FingerprintSource.BOOTLOADER_ARTIFACTS
        )
        records = _artifact_records(self._source_evidence(artifacts, source))
        resolved = {
            (
                Path(path_value)
                if Path(path_value).is_absolute()
                else self.repository.store.layout.project_root / path_value
            ).resolve(): digest
            for path_value, digest in records
        }
        expected_digest = resolved.get(artifact)
        if expected_digest is None:
            raise SafetyPolicyError(
                "safety/flash-artifact-unfingerprinted",
                f"Selected {role.value} artifact '{artifact}' is not in the current aggregate.",
                remedy=("board_safety_refresh",),
            )
        if not artifact.is_file() or sha256(artifact.read_bytes()).hexdigest() != expected_digest:
            raise SafetyPolicyError(
                "safety/artifact-stale",
                f"Selected {role.value} artifact '{artifact}' changed or disappeared.",
                remedy=("board_safety_refresh",),
            )


def _artifact_records(value: object) -> tuple[tuple[str, str], ...]:
    records: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        path = value.get("path")
        digest = value.get("sha256")
        if isinstance(path, str) and isinstance(digest, str):
            records.append((path, digest))
        for item in value.values():
            records.extend(_artifact_records(item))
    elif isinstance(value, list):
        for item in value:
            records.extend(_artifact_records(item))
    return tuple(records)


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
