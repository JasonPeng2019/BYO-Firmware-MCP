"""Schema-v2 single-file safety-map model, construction, and persistence.

``memory_map.yaml`` is intentionally the only durable safety authority.  Build
artifacts, live gates, plans, permissions, and aggregate fingerprints do not
belong in this module's persisted model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Final

import yaml  # type: ignore[import-untyped]

from pyocd_debug_mcp.firmstore.store import FirmStore, ensure_no_persisted_authority
from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionKind,
    SafetyMap,
    SafetyRegion,
    SourceAuthority,
)

SAFETY_MAP_SCHEMA_VERSION: Final = 2
MAP_GENERATOR_SCHEMA_VERSION: Final = 2
NO_INTERNALS: Final = "Relay this guidance conversationally and do not expose structured internals."

_BOARD_ID = re.compile(r"[a-z0-9_]{1,64}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_SEMANTIC_PROFILE_FIELDS: Final = frozenset(
    {
        "board_id",
        "board_type",
        "mcu_part_number",
        "mcu_family",
        "probe_family",
        "probe_type",
        "pyocd_target",
        "requires_recover_validation",
        "recover_mode",
    }
)
_GENERATOR_SCHEMA_DOCUMENT: Final[dict[str, object]] = {
    "schema_version": MAP_GENERATOR_SCHEMA_VERSION,
    "map_schema_version": SAFETY_MAP_SCHEMA_VERSION,
    "range_semantics": "unsigned-64-bit-half-open",
    "partition_executable_authority": "per-operation-elf-only",
    "prohibited_precedence": True,
}


class SafetyMapError(RuntimeError):
    """A safety map is missing, malformed, inconsistent, or cannot be promoted."""


def _canonicalize(value: object, *, location: str = "value") -> object:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise SafetyMapError(f"{location} mapping keys must be strings")
        return {
            key: _canonicalize(value[key], location=f"{location}.{key}") for key in sorted(value)
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _canonicalize(item, location=f"{location}[{index}]") for index, item in enumerate(value)
        ]
    raise SafetyMapError(f"{location} contains a value that cannot be canonically hashed")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _require_board_id(value: object) -> str:
    if not isinstance(value, str) or _BOARD_ID.fullmatch(value) is None:
        raise SafetyMapError("board_id must be 1-64 lowercase letters, numbers, or underscores")
    return value


def _required_string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SafetyMapError(f"{location} must be a non-empty string")
    return value.strip()


def _required_int(value: object, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SafetyMapError(f"{location} must be an integer")
    return value


def _exact_mapping(
    value: object, fields: set[str] | frozenset[str], location: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise SafetyMapError(f"{location} fields do not match safety-map schema v2")
    if any(not isinstance(key, str) for key in value):
        raise SafetyMapError(f"{location} keys must be strings")
    return value  # type: ignore[return-value]


def _domain_digest(domain: str, value: object) -> str:
    return sha256(
        f"firm-safety-v2:{domain}\0".encode("ascii") + _canonical_bytes(value)
    ).hexdigest()


def semantic_profile_document(profile: Mapping[str, object]) -> dict[str, object]:
    """Select only stable safety identity from a schema-v2 profile.

    Display/UART fields, timestamps, paths, probe labels, and ``safety_ref`` are
    deliberately absent so bookkeeping and ordinary serial changes cannot stale
    the stable safety map.
    """

    selected = {key: profile[key] for key in sorted(_SEMANTIC_PROFILE_FIELDS & set(profile))}
    return _canonicalize(selected, location="semantic_profile")  # type: ignore[return-value]


def semantic_profile_digest(profile: Mapping[str, object]) -> str:
    return _domain_digest("semantic-profile", semantic_profile_document(profile))


@dataclass(frozen=True, slots=True)
class MapIdentity:
    mcu_part_number: str
    pyocd_target: str
    reviewed_board_type: str

    def __post_init__(self) -> None:
        for field_name in ("mcu_part_number", "pyocd_target", "reviewed_board_type"):
            object.__setattr__(
                self,
                field_name,
                _required_string(getattr(self, field_name), f"identity.{field_name}"),
            )

    def to_document(self) -> dict[str, str]:
        return {
            "mcu_part_number": self.mcu_part_number,
            "pyocd_target": self.pyocd_target,
            "reviewed_board_type": self.reviewed_board_type,
        }

    @classmethod
    def from_document(cls, value: object) -> MapIdentity:
        raw = _exact_mapping(
            value,
            {"mcu_part_number", "pyocd_target", "reviewed_board_type"},
            "identity",
        )
        return cls(
            _required_string(raw["mcu_part_number"], "identity.mcu_part_number"),
            _required_string(raw["pyocd_target"], "identity.pyocd_target"),
            _required_string(raw["reviewed_board_type"], "identity.reviewed_board_type"),
        )


@dataclass(frozen=True, slots=True)
class SourceDigests:
    semantic_profile: str
    reviewed_device_support: str
    reviewed_official_evidence: str
    map_generator_schema: str

    def __post_init__(self) -> None:
        for field_name in (
            "semantic_profile",
            "reviewed_device_support",
            "reviewed_official_evidence",
            "map_generator_schema",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
                raise SafetyMapError(f"source_digests.{field_name} must be a lowercase SHA-256")

    @classmethod
    def build(
        cls,
        *,
        profile: Mapping[str, object],
        reviewed_device_support: object,
        reviewed_official_evidence: object,
    ) -> SourceDigests:
        return cls(
            semantic_profile_digest(profile),
            _domain_digest("reviewed-device-support", reviewed_device_support),
            _domain_digest("reviewed-official-evidence", reviewed_official_evidence),
            _domain_digest("map-generator-schema", _GENERATOR_SCHEMA_DOCUMENT),
        )

    def to_document(self) -> dict[str, str]:
        return {
            "semantic_profile": self.semantic_profile,
            "reviewed_device_support": self.reviewed_device_support,
            "reviewed_official_evidence": self.reviewed_official_evidence,
            "map_generator_schema": self.map_generator_schema,
        }

    @classmethod
    def from_document(cls, value: object) -> SourceDigests:
        fields = {
            "semantic_profile",
            "reviewed_device_support",
            "reviewed_official_evidence",
            "map_generator_schema",
        }
        raw = _exact_mapping(value, fields, "source_digests")
        return cls(
            _required_string(raw["semantic_profile"], "source_digests.semantic_profile"),
            _required_string(
                raw["reviewed_device_support"],
                "source_digests.reviewed_device_support",
            ),
            _required_string(
                raw["reviewed_official_evidence"],
                "source_digests.reviewed_official_evidence",
            ),
            _required_string(
                raw["map_generator_schema"],
                "source_digests.map_generator_schema",
            ),
        )


@dataclass(frozen=True, slots=True, order=True)
class EraseSector:
    address_range: AddressRange
    bank: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "bank", _required_string(self.bank, "erase sector bank"))

    def to_document(self) -> dict[str, object]:
        return {**self.address_range.to_document(), "bank": self.bank}


@dataclass(frozen=True, slots=True)
class MapGeometry:
    physical_flash: AddressRange
    physical_ram: AddressRange
    erase_origin: int | None = None
    erase_size: int | None = None
    erase_sectors: tuple[EraseSector, ...] = ()

    def __post_init__(self) -> None:
        if self.physical_flash.overlaps(self.physical_ram):
            raise SafetyMapError("physical flash and RAM geometry must not overlap")
        uniform = self.erase_origin is not None or self.erase_size is not None
        explicit = bool(self.erase_sectors)
        if uniform == explicit:
            raise SafetyMapError("geometry requires exactly one uniform or explicit erase model")
        if uniform:
            origin = _required_int(self.erase_origin, "geometry.erase.origin")
            size = _required_int(self.erase_size, "geometry.erase.size")
            if origin < 0 or size <= 0:
                raise SafetyMapError("uniform erase origin/size must be non-negative/positive")
            if self.physical_flash.start < origin or (
                (self.physical_flash.start - origin) % size
                or (self.physical_flash.end - origin) % size
            ):
                raise SafetyMapError("uniform erase geometry must align the physical flash bounds")
        else:
            sectors = tuple(sorted(set(self.erase_sectors)))
            object.__setattr__(self, "erase_sectors", sectors)
            cursor = self.physical_flash.start
            for sector in sectors:
                if sector.address_range.start != cursor:
                    raise SafetyMapError("explicit erase sectors must cover flash contiguously")
                if not self.physical_flash.contains(sector.address_range):
                    raise SafetyMapError("explicit erase sectors must stay inside physical flash")
                cursor = sector.address_range.end
            if cursor != self.physical_flash.end:
                raise SafetyMapError("explicit erase sectors must cover all physical flash")

    def to_document(self) -> dict[str, object]:
        erase: dict[str, object]
        if self.erase_sectors:
            erase = {
                "kind": "explicit",
                "sectors": [sector.to_document() for sector in self.erase_sectors],
            }
        else:
            erase = {"kind": "uniform", "origin": self.erase_origin, "size": self.erase_size}
        return {
            "physical_flash": self.physical_flash.to_document(),
            "physical_ram": self.physical_ram.to_document(),
            "erase": erase,
        }

    @classmethod
    def from_document(cls, value: object) -> MapGeometry:
        raw = _exact_mapping(value, {"physical_flash", "physical_ram", "erase"}, "geometry")
        flash = _range_from_document(raw["physical_flash"], "geometry.physical_flash")
        ram = _range_from_document(raw["physical_ram"], "geometry.physical_ram")
        erase = raw["erase"]
        if not isinstance(erase, Mapping):
            raise SafetyMapError("geometry.erase must be an object")
        if erase.get("kind") == "uniform":
            uniform = _exact_mapping(erase, {"kind", "origin", "size"}, "geometry.erase")
            return cls(
                flash,
                ram,
                _required_int(uniform["origin"], "geometry.erase.origin"),
                _required_int(uniform["size"], "geometry.erase.size"),
            )
        if erase.get("kind") == "explicit":
            explicit = _exact_mapping(erase, {"kind", "sectors"}, "geometry.erase")
            rows = explicit["sectors"]
            if not isinstance(rows, list) or not rows:
                raise SafetyMapError("geometry.erase.sectors must be a non-empty list")
            sectors: list[EraseSector] = []
            for index, row in enumerate(rows):
                sector = _exact_mapping(
                    row, {"start", "end", "bank"}, f"geometry.erase.sectors[{index}]"
                )
                sectors.append(
                    EraseSector(
                        AddressRange(
                            _required_int(sector["start"], "erase sector start"),
                            _required_int(sector["end"], "erase sector end"),
                        ),
                        _required_string(sector["bank"], "erase sector bank"),
                    )
                )
            return cls(flash, ram, erase_sectors=tuple(sectors))
        raise SafetyMapError("geometry.erase.kind must be 'uniform' or 'explicit'")


@dataclass(frozen=True, slots=True)
class MapPartitions:
    application: AddressRange | None
    bootloader: AddressRange | None = None

    def __post_init__(self) -> None:
        if (
            self.application is not None
            and self.bootloader is not None
            and self.application.overlaps(self.bootloader)
        ):
            raise SafetyMapError("application and bootloader partitions must not overlap")

    def to_document(self) -> dict[str, object]:
        return {
            "application": self.application.to_document() if self.application else None,
            "bootloader": self.bootloader.to_document() if self.bootloader else None,
        }

    @classmethod
    def from_document(cls, value: object) -> MapPartitions:
        raw = _exact_mapping(value, {"application", "bootloader"}, "partitions")
        return cls(
            _optional_range_from_document(raw["application"], "partitions.application"),
            _optional_range_from_document(raw["bootloader"], "partitions.bootloader"),
        )


class RegionSource(str, Enum):
    REVIEWED_DEVICE_SUPPORT = "reviewed_device_support"
    REVIEWED_OFFICIAL_EVIDENCE = "reviewed_official_evidence"
    REVIEWED_PARTITION_POLICY = "reviewed_partition_policy"
    GEOMETRY = "geometry"
    DERIVED = "derived"


@dataclass(frozen=True, slots=True)
class RegionContribution:
    """Typed internal derivation record; source groups are never persisted."""

    region: SafetyRegion
    source_groups: tuple[RegionSource, ...]

    def __post_init__(self) -> None:
        try:
            groups = tuple(
                sorted(
                    {
                        item if isinstance(item, RegionSource) else RegionSource(item)
                        for item in self.source_groups
                    },
                    key=lambda item: item.value,
                )
            )
        except (TypeError, ValueError) as exc:
            raise SafetyMapError("region source groups must be stable schema-v2 sources") from exc
        if not groups:
            raise SafetyMapError("a region requires authoritative internal source groups")
        object.__setattr__(self, "source_groups", groups)


def _range_from_document(value: object, location: str) -> AddressRange:
    raw = _exact_mapping(value, {"start", "end"}, location)
    try:
        return AddressRange(
            _required_int(raw["start"], f"{location}.start"),
            _required_int(raw["end"], f"{location}.end"),
        )
    except ValueError as exc:
        raise SafetyMapError(f"invalid {location}: {exc}") from exc


def _optional_range_from_document(value: object, location: str) -> AddressRange | None:
    return None if value is None else _range_from_document(value, location)


def _region_from_document(value: object, location: str = "region") -> SafetyRegion:
    fields = {"name", "kind", "start", "end", "executable", "provenance"}
    raw = _exact_mapping(value, fields, location)
    provenance_rows = raw["provenance"]
    if not isinstance(provenance_rows, list) or not provenance_rows:
        raise SafetyMapError(f"{location}.provenance must be a non-empty list")
    provenance: list[Provenance] = []
    for index, row in enumerate(provenance_rows):
        item = _exact_mapping(
            row,
            {"authority", "source_id", "detail"},
            f"{location}.provenance[{index}]",
        )
        try:
            provenance.append(
                Provenance(
                    SourceAuthority(item["authority"]),  # type: ignore[arg-type]
                    _required_string(item["source_id"], "provenance.source_id"),
                    _required_string(item["detail"], "provenance.detail"),
                )
            )
        except ValueError as exc:
            raise SafetyMapError(f"invalid {location} provenance: {exc}") from exc
    if not isinstance(raw["executable"], bool):
        raise SafetyMapError(f"{location}.executable must be a boolean")
    try:
        return SafetyRegion(
            _required_string(raw["name"], f"{location}.name"),
            RegionKind(raw["kind"]),  # type: ignore[arg-type]
            AddressRange(
                _required_int(raw["start"], f"{location}.start"),
                _required_int(raw["end"], f"{location}.end"),
            ),
            tuple(provenance),
            raw["executable"],
        )
    except ValueError as exc:
        raise SafetyMapError(f"invalid {location}: {exc}") from exc


def _region_sort_key(region: SafetyRegion) -> tuple[object, ...]:
    return (
        region.address_range.start,
        region.address_range.end,
        region.kind.value,
        region.name,
        region.executable,
        tuple((item.authority.value, item.source_id, item.detail) for item in region.provenance),
    )


def _partition_regions(
    identity: MapIdentity, partitions: MapPartitions
) -> tuple[SafetyRegion, ...]:
    provenance = (
        Provenance(
            SourceAuthority.RECONCILED,
            f"reviewed catalog:{identity.reviewed_board_type}",
            "authoritative deployment partition policy",
        ),
    )
    result: list[SafetyRegion] = []
    if partitions.application:
        result.append(
            SafetyRegion(
                "application partition",
                RegionKind.APPLICATION_FLASH,
                partitions.application,
                provenance,
            )
        )
    if partitions.bootloader:
        result.append(
            SafetyRegion(
                "bootloader partition",
                RegionKind.BOOTLOADER_FLASH,
                partitions.bootloader,
                provenance,
            )
        )
    return tuple(result)


def _ambiguous_overlap(first: SafetyRegion, second: SafetyRegion) -> bool:
    if not first.address_range.overlaps(second.address_range):
        return False
    left, right = first.kind, second.kind
    if left is right or RegionKind.PROHIBITED in {left, right}:
        return False
    allowed_nesting = {
        frozenset({RegionKind.PHYSICAL_FLASH, RegionKind.APPLICATION_FLASH}),
        frozenset({RegionKind.PHYSICAL_FLASH, RegionKind.BOOTLOADER_FLASH}),
        frozenset({RegionKind.PHYSICAL_RAM, RegionKind.RAM}),
        frozenset({RegionKind.ROM, RegionKind.ROM_BOOTLOADER}),
    }
    return frozenset({left, right}) not in allowed_nesting


def region_conflicts(
    regions: Iterable[RegionContribution | SafetyRegion],
) -> tuple[dict[str, object], ...]:
    typed = tuple(item.region if isinstance(item, RegionContribution) else item for item in regions)
    if not typed:
        return ()
    safety = SafetyMap(list(typed))
    conflicts: list[dict[str, object]] = []
    for partition, prohibited in safety.partition_prohibited_conflicts():
        overlap = partition.address_range.intersection(prohibited.address_range)
        assert overlap is not None
        conflicts.append(
            {
                "code": "partition_prohibited_overlap",
                "regions": [partition.name, prohibited.name],
                "range": overlap.to_document(),
            }
        )
    for index, first in enumerate(typed):
        for second in typed[index + 1 :]:
            if _ambiguous_overlap(first, second):
                overlap = first.address_range.intersection(second.address_range)
                assert overlap is not None
                conflicts.append(
                    {
                        "code": "ambiguous_region_overlap",
                        "regions": [first.name, second.name],
                        "range": overlap.to_document(),
                    }
                )
    return tuple(sorted(conflicts, key=lambda item: (str(item["code"]), str(item["regions"]))))


@dataclass(frozen=True, slots=True)
class SafetyMapDocument:
    board_id: str
    identity: MapIdentity
    source_digests: SourceDigests
    geometry: MapGeometry
    partitions: MapPartitions
    regions: tuple[SafetyRegion, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "board_id", _require_board_id(self.board_id))
        canonical_regions = tuple(sorted(set(self.regions), key=_region_sort_key))
        if not canonical_regions:
            raise SafetyMapError("a safety map requires at least one region")
        if any(
            region.kind in {RegionKind.APPLICATION_FLASH, RegionKind.BOOTLOADER_FLASH}
            for region in canonical_regions
        ):
            raise SafetyMapError(
                "deployment partitions belong only in the authoritative partitions field"
            )
        if any(
            provenance.authority is SourceAuthority.BUILD
            for region in canonical_regions
            for provenance in region.provenance
        ):
            raise SafetyMapError("build-derived authority must not be persisted in a safety map")
        physical_flash = tuple(
            region.address_range
            for region in canonical_regions
            if region.kind is RegionKind.PHYSICAL_FLASH
        )
        physical_ram = tuple(
            region.address_range
            for region in canonical_regions
            if region.kind is RegionKind.PHYSICAL_RAM
        )
        if physical_flash != (self.geometry.physical_flash,):
            raise SafetyMapError("regions must contain exactly the physical flash geometry")
        if physical_ram != (self.geometry.physical_ram,):
            raise SafetyMapError("regions must contain exactly the physical RAM geometry")
        object.__setattr__(self, "regions", canonical_regions)
        for label, partition in (
            ("application", self.partitions.application),
            ("bootloader", self.partitions.bootloader),
        ):
            if partition is not None and not self.geometry.physical_flash.contains(partition):
                raise SafetyMapError(f"{label} partition must be inside physical flash")
        conflicts = region_conflicts(
            (*canonical_regions, *_partition_regions(self.identity, self.partitions))
        )
        if conflicts:
            raise SafetyMapError(f"safety map contains region conflicts: {conflicts}")

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": SAFETY_MAP_SCHEMA_VERSION,
            "board_id": self.board_id,
            "identity": self.identity.to_document(),
            "source_digests": self.source_digests.to_document(),
            "geometry": self.geometry.to_document(),
            "partitions": self.partitions.to_document(),
            "regions": [region.to_document() for region in self.regions],
        }

    @property
    def canonical_digest(self) -> str:
        return _domain_digest("canonical-map", self.to_document())

    @property
    def safety_map(self) -> SafetyMap:
        return SafetyMap([*self.regions, *_partition_regions(self.identity, self.partitions)])

    def to_safety_map(self) -> SafetyMap:
        return self.safety_map

    @classmethod
    def from_document(cls, value: object) -> SafetyMapDocument:
        fields = {
            "schema_version",
            "board_id",
            "identity",
            "source_digests",
            "geometry",
            "partitions",
            "regions",
        }
        raw = _exact_mapping(value, fields, "memory map")
        if raw["schema_version"] != SAFETY_MAP_SCHEMA_VERSION:
            raise SafetyMapError("unsupported memory-map schema version; run board_safety_refresh")
        rows = raw["regions"]
        if not isinstance(rows, list) or not rows:
            raise SafetyMapError("memory map regions must be a non-empty list")
        return cls(
            _require_board_id(raw["board_id"]),
            MapIdentity.from_document(raw["identity"]),
            SourceDigests.from_document(raw["source_digests"]),
            MapGeometry.from_document(raw["geometry"]),
            MapPartitions.from_document(raw["partitions"]),
            tuple(
                _region_from_document(row, f"regions[{index}]") for index, row in enumerate(rows)
            ),
        )


@dataclass(frozen=True, slots=True)
class SafetyMapBuildRequest:
    board_id: str
    identity: MapIdentity
    profile: Mapping[str, object]
    reviewed_device_support: object
    reviewed_official_evidence: object
    geometry: MapGeometry
    partitions: MapPartitions
    regions: tuple[RegionContribution, ...]


class SafetyMapRepository:
    """Read and atomically replace the one persisted safety-authority file."""

    def __init__(self, store: FirmStore) -> None:
        self.store = store

    def path(self, board_id: str) -> Path:
        return self.store.layout.safety_board(_require_board_id(board_id)) / "memory_map.yaml"

    def _legacy_paths(self, board_id: str) -> tuple[Path, Path]:
        root = self.store.layout.safety_board(_require_board_id(board_id))
        return root / "source_manifest.json", root / "safety_report.json"

    def _cleanup_legacy(self, board_id: str) -> None:
        for path in self._legacy_paths(board_id):
            self.store.remove_artifact(path)

    def commit(self, board_id: str, document: SafetyMapDocument) -> Path:
        identity = _require_board_id(board_id)
        if document.board_id != identity:
            raise SafetyMapError("memory map does not match the requested board")
        payload = document.to_document()
        ensure_no_persisted_authority(payload, location="memory map")
        text = yaml.safe_dump(
            payload,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        result = self.store.atomic_write_text(self.path(identity), text)
        self._cleanup_legacy(identity)
        return result

    def load_current(self, board_id: str) -> SafetyMapDocument:
        identity = _require_board_id(board_id)
        self._cleanup_legacy(identity)
        path = self.path(identity)
        if not path.is_file():
            raise SafetyMapError("current memory map is missing; run board_safety_refresh")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise SafetyMapError(
                f"current memory map is malformed; run board_safety_refresh: {exc}"
            ) from exc
        document = SafetyMapDocument.from_document(raw)
        if document.board_id != identity:
            raise SafetyMapError("memory map does not match the requested board")
        return document


class SafetyMapBuilder:
    """Deterministically derive complete candidate maps from server-owned inputs."""

    def __init__(self, repository: SafetyMapRepository | FirmStore) -> None:
        self.repository = (
            repository
            if isinstance(repository, SafetyMapRepository)
            else SafetyMapRepository(repository)
        )

    def derive(self, request: SafetyMapBuildRequest) -> SafetyMapDocument:
        _require_board_id(request.board_id)
        conflicts = region_conflicts(request.regions)
        if conflicts:
            raise SafetyMapError(f"authoritative safety regions conflict: {conflicts}")
        return SafetyMapDocument(
            request.board_id,
            request.identity,
            SourceDigests.build(
                profile=request.profile,
                reviewed_device_support=request.reviewed_device_support,
                reviewed_official_evidence=request.reviewed_official_evidence,
            ),
            request.geometry,
            request.partitions,
            tuple(item.region for item in request.regions),
        )

    def build(self, request: SafetyMapBuildRequest) -> SafetyMapDocument:
        candidate = self.derive(request)
        self.repository.commit(request.board_id, candidate)
        return candidate


def reviewed_map_source_documents(
    catalog: object,
    evidence_bundle: object,
) -> tuple[object, object]:
    """Compose the exact reviewed documents covered by schema-v2 source digests."""

    source_record_method = getattr(evidence_bundle, "source_record", None)
    policy_method = getattr(catalog, "deployment_partition_policy_document", None)
    if not callable(source_record_method) or not callable(policy_method):
        raise SafetyMapError("reviewed map sources do not expose required source records")
    record = source_record_method()
    if not isinstance(record, Mapping):
        raise SafetyMapError("reviewed evidence source record must be an object")
    device_support = record.get("device_support")
    official = record.get("official_document")
    reconciliation = record.get("reconciliation")
    if not all(isinstance(item, Mapping) for item in (device_support, official, reconciliation)):
        raise SafetyMapError("reviewed evidence source record is incomplete")
    official_and_policy = {
        "official_document": official,
        "reconciliation": reconciliation,
        "deployment_partition_policy": policy_method(),
    }
    return device_support, official_and_policy


def require_reconciled_authority(document: SafetyMapDocument) -> None:
    """Fail closed unless the map identity and partitions match reviewed policy."""

    try:
        from pyocd_debug_mcp.setup_flow.board_catalog import catalog_board
        from pyocd_debug_mcp.setup_flow.reviewed_evidence import load_pinned_reviewed_evidence

        catalog = catalog_board(document.identity.reviewed_board_type)
    except Exception as exc:  # noqa: BLE001 - authority resolution must fail closed
        raise SafetyMapError(f"reviewed map identity is unavailable: {exc}") from exc
    if (
        document.identity.mcu_part_number != catalog.package_part_number
        or document.identity.pyocd_target != catalog.pyocd_target
    ):
        raise SafetyMapError("memory-map identity does not match reviewed catalog authority")
    if not catalog.automatic_setup_reviewed:
        raise SafetyMapError("catalog entry lacks complete reviewed automatic safety authority")
    if (
        document.geometry.physical_flash != AddressRange(catalog.flash_start, catalog.flash_end)
        or document.geometry.physical_ram != AddressRange(catalog.ram_start, catalog.ram_end)
        or document.geometry.erase_sectors
        or document.geometry.erase_origin != catalog.flash_start
        or document.geometry.erase_size != catalog.erase_size
    ):
        raise SafetyMapError("memory-map geometry does not match reviewed catalog authority")
    expected_generator = _domain_digest("map-generator-schema", _GENERATOR_SCHEMA_DOCUMENT)
    if document.source_digests.map_generator_schema != expected_generator:
        raise SafetyMapError("memory map was produced by an unsupported generator schema")
    if document.partitions.application != catalog.application_partition:
        raise SafetyMapError("application partition does not match reviewed catalog authority")
    if document.partitions.bootloader != catalog.bootloader_partition:
        raise SafetyMapError("bootloader partition does not match reviewed catalog authority")
    matched_sources = False
    source_failures: list[str] = []
    for datasheet_digest in catalog.datasheet_sha256:
        try:
            bundle = load_pinned_reviewed_evidence(catalog, datasheet_digest)
            device_support, official = reviewed_map_source_documents(catalog, bundle)
            matched_sources = document.source_digests.reviewed_device_support == _domain_digest(
                "reviewed-device-support", device_support
            ) and document.source_digests.reviewed_official_evidence == _domain_digest(
                "reviewed-official-evidence", official
            )
        except Exception as exc:  # noqa: BLE001 - each reviewed anchor fails closed
            source_failures.append(str(exc))
        if matched_sources:
            break
    if not matched_sources:
        details = "; ".join(source_failures) or "no reviewed datasheet anchor"
        raise SafetyMapError(
            f"memory-map source digests do not match current reviewed evidence: {details}"
        )


def canonical_map_digest(document: SafetyMapDocument) -> str:
    return document.canonical_digest
