"""Single-file schema-v2/v3 safety-map models, construction, and persistence.

``memory_map.yaml`` is intentionally the only durable safety authority.  Build
artifacts, live gates, plans, permissions, and aggregate fingerprints do not
belong in this module's persisted model.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, cast

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
_GENERIC_GENERATOR_SCHEMA_DOCUMENT: Final[dict[str, object]] = {
    **_GENERATOR_SCHEMA_DOCUMENT,
    "schema_version": 3,
    "map_schema_version": 3,
    "authority_kind": "resolved_pack",
    "nullable_partitions": True,
    "artifact_application_allocation": 1,
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
    erase_available: bool = True

    def __post_init__(self) -> None:
        if self.physical_flash.overlaps(self.physical_ram):
            raise SafetyMapError("physical flash and RAM geometry must not overlap")
        uniform = self.erase_origin is not None or self.erase_size is not None
        explicit = bool(self.erase_sectors)
        if not self.erase_available:
            if uniform or explicit:
                raise SafetyMapError("unavailable erase geometry must not contain erase sectors")
            return
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
        if not self.erase_available:
            erase = {"kind": "unavailable"}
        elif self.erase_sectors:
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
        if erase.get("kind") == "unavailable":
            unavailable = _exact_mapping(erase, {"kind"}, "geometry.erase")
            del unavailable
            return cls(flash, ram, erase_available=False)
        raise SafetyMapError("geometry.erase.kind must be 'uniform', 'explicit', or 'unavailable'")


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
class GenericMapIdentity:
    """Schema-v3 identity for a generic resolved-pack device authority."""

    mcu_part_number: str
    pyocd_target: str
    support_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "mcu_part_number", _required_string(self.mcu_part_number, "identity.mcu_part_number")
        )
        object.__setattr__(
            self, "pyocd_target", _required_string(self.pyocd_target, "identity.pyocd_target")
        )
        if not isinstance(self.support_id, str) or _DIGEST.fullmatch(self.support_id) is None:
            raise SafetyMapError("identity.support_id must be a lowercase SHA-256")

    def to_document(self) -> dict[str, str]:
        return {
            "mcu_part_number": self.mcu_part_number,
            "pyocd_target": self.pyocd_target,
            "authority_kind": "resolved_pack",
            "support_id": self.support_id,
        }

    @classmethod
    def from_document(cls, value: object) -> "GenericMapIdentity":
        raw = _exact_mapping(
            value,
            {"mcu_part_number", "pyocd_target", "authority_kind", "support_id"},
            "identity",
        )
        if raw["authority_kind"] != "resolved_pack":
            raise SafetyMapError("generic memory-map authority_kind must be 'resolved_pack'")
        return cls(
            _required_string(raw["mcu_part_number"], "identity.mcu_part_number"),
            _required_string(raw["pyocd_target"], "identity.pyocd_target"),
            _required_string(raw["support_id"], "identity.support_id"),
        )


@dataclass(frozen=True, slots=True)
class GenericSourceDigests:
    """Schema-v3 source digests without misleading reviewed-catalog labels."""

    semantic_profile: str
    device_support: str
    datasheet_evidence: str
    deployment_policy: str
    map_generator_schema: str

    def __post_init__(self) -> None:
        for name in (
            "semantic_profile",
            "device_support",
            "datasheet_evidence",
            "deployment_policy",
            "map_generator_schema",
        ):
            if not isinstance(getattr(self, name), str) or _DIGEST.fullmatch(getattr(self, name)) is None:
                raise SafetyMapError(f"source_digests.{name} must be a lowercase SHA-256")

    @classmethod
    def build(
        cls,
        *,
        profile: Mapping[str, object],
        device_support: object,
        datasheet_evidence: object,
        deployment_policy: object,
    ) -> "GenericSourceDigests":
        return cls(
            semantic_profile_digest(profile),
            _domain_digest("generic-device-support", device_support),
            _domain_digest("generic-datasheet-evidence", datasheet_evidence),
            _domain_digest("generic-deployment-policy", deployment_policy),
            _domain_digest("map-generator-schema", _GENERIC_GENERATOR_SCHEMA_DOCUMENT),
        )

    def to_document(self) -> dict[str, str]:
        return {
            "semantic_profile": self.semantic_profile,
            "device_support": self.device_support,
            "datasheet_evidence": self.datasheet_evidence,
            "deployment_policy": self.deployment_policy,
            "map_generator_schema": self.map_generator_schema,
        }

    @classmethod
    def from_document(cls, value: object) -> "GenericSourceDigests":
        raw = _exact_mapping(
            value,
            {
                "semantic_profile",
                "device_support",
                "datasheet_evidence",
                "deployment_policy",
                "map_generator_schema",
            },
            "source_digests",
        )
        return cls(*( _required_string(raw[name], f"source_digests.{name}") for name in (
            "semantic_profile", "device_support", "datasheet_evidence", "deployment_policy", "map_generator_schema"
        )))


def _covered_by_ranges(requested: AddressRange, ranges: Sequence[AddressRange]) -> bool:
    cursor = requested.start
    for item in sorted(set(ranges)):
        if item.end <= cursor:
            continue
        if item.start > cursor:
            return False
        cursor = max(cursor, item.end)
        if cursor >= requested.end:
            return True
    return False


@dataclass(frozen=True, slots=True)
class GenericMapGeometry:
    """Schema-v3 physical geometry without fabricated gap-spanning envelopes."""

    physical_flash: tuple[AddressRange, ...]
    physical_ram: tuple[AddressRange, ...]
    erase_sectors: tuple[EraseSector, ...] = ()
    erase_available: bool = True

    def __post_init__(self) -> None:
        flash = tuple(sorted(set(self.physical_flash)))
        ram = tuple(sorted(set(self.physical_ram)))
        if not flash or not ram:
            raise SafetyMapError("generic geometry requires physical flash and RAM regions")
        for rows, label in ((flash, "flash"), (ram, "RAM")):
            if any(left.overlaps(right) for left, right in zip(rows, rows[1:])):
                raise SafetyMapError(f"generic physical {label} regions must not overlap")
        if any(left.overlaps(right) for left in flash for right in ram):
            raise SafetyMapError("generic physical flash and RAM must not overlap")
        sectors = tuple(sorted(set(self.erase_sectors)))
        if not self.erase_available and sectors:
            raise SafetyMapError("unavailable generic erase geometry must have no sectors")
        if self.erase_available and not sectors:
            raise SafetyMapError("available generic erase geometry requires explicit sectors")
        if any(
            not _covered_by_ranges(sector.address_range, flash) for sector in sectors
        ):
            raise SafetyMapError("generic erase sectors must stay inside physical flash")
        if any(
            left.address_range.overlaps(right.address_range)
            for left, right in zip(sectors, sectors[1:])
        ):
            raise SafetyMapError("generic erase sectors must not overlap")
        object.__setattr__(self, "physical_flash", flash)
        object.__setattr__(self, "physical_ram", ram)
        object.__setattr__(self, "erase_sectors", sectors)

    def contains_flash(self, requested: AddressRange) -> bool:
        return _covered_by_ranges(requested, self.physical_flash)

    def contains_ram(self, requested: AddressRange) -> bool:
        return _covered_by_ranges(requested, self.physical_ram)

    def to_document(self) -> dict[str, object]:
        erase: dict[str, object] = (
            {
                "kind": "explicit",
                "sectors": [sector.to_document() for sector in self.erase_sectors],
            }
            if self.erase_available
            else {"kind": "unavailable"}
        )
        return {
            "physical_flash": [item.to_document() for item in self.physical_flash],
            "physical_ram": [item.to_document() for item in self.physical_ram],
            "erase": erase,
        }

    @classmethod
    def from_document(cls, value: object) -> "GenericMapGeometry":
        raw = _exact_mapping(value, {"physical_flash", "physical_ram", "erase"}, "geometry")

        def ranges(name: str) -> tuple[AddressRange, ...]:
            rows = raw[name]
            if not isinstance(rows, list) or not rows:
                raise SafetyMapError(f"geometry.{name} must be a non-empty list")
            return tuple(
                _range_from_document(row, f"geometry.{name}[{index}]")
                for index, row in enumerate(rows)
            )

        erase = raw["erase"]
        if not isinstance(erase, Mapping):
            raise SafetyMapError("geometry.erase must be an object")
        if erase.get("kind") == "unavailable":
            _exact_mapping(erase, {"kind"}, "geometry.erase")
            return cls(ranges("physical_flash"), ranges("physical_ram"), erase_available=False)
        explicit = _exact_mapping(erase, {"kind", "sectors"}, "geometry.erase")
        if explicit["kind"] != "explicit":
            raise SafetyMapError("generic geometry erase kind must be explicit or unavailable")
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
                    _required_string(sector["bank"], "erase bank"),
                )
            )
        return cls(ranges("physical_flash"), ranges("physical_ram"), tuple(sectors))


def build_artifact_application_allocation(
    sectors: Sequence[AddressRange],
    *,
    driver_proof_digest: str,
    creation_map_digest: str,
    creation_artifact_digest: str,
    parent_allocation_digest: str | None = None,
) -> dict[str, object]:
    """Build a server-derived application allocation for one approved artifact."""

    ordered = tuple(sorted(set(sectors)))
    if not ordered or any(
        _DIGEST.fullmatch(value) is None
        for value in (
            driver_proof_digest,
            creation_map_digest,
            creation_artifact_digest,
        )
    ):
        raise SafetyMapError("artifact allocation requires sectors and three SHA-256 proofs")
    if parent_allocation_digest is not None and _DIGEST.fullmatch(parent_allocation_digest) is None:
        raise SafetyMapError("parent allocation digest must be null or a lowercase SHA-256")
    for left, right in zip(ordered, ordered[1:]):
        if left.end != right.start:
            raise SafetyMapError("artifact allocation sectors must be contiguous")
    material: dict[str, object] = {
        "kind": "artifact_application_allocation",
        "sectors": [item.to_document() for item in ordered],
        "driver_proof_digest": driver_proof_digest,
        "creation_map_digest": creation_map_digest,
        "creation_artifact_digest": creation_artifact_digest,
        "parent_allocation_digest": parent_allocation_digest,
    }
    return {
        **material,
        "allocation_digest": _domain_digest("artifact-application-allocation", material),
    }


def _validated_generic_deployment_policy(value: Mapping[str, object]) -> dict[str, object]:
    policy = dict(value)
    if policy == {"kind": "none"}:
        return policy
    raw = _exact_mapping(
        policy,
        {
            "kind",
            "sectors",
            "driver_proof_digest",
            "creation_map_digest",
            "creation_artifact_digest",
            "parent_allocation_digest",
            "allocation_digest",
        },
        "generic deployment_policy",
    )
    if raw["kind"] != "artifact_application_allocation":
        raise SafetyMapError("unsupported generic deployment policy kind")
    rows = raw["sectors"]
    if not isinstance(rows, list) or not rows:
        raise SafetyMapError("artifact allocation requires a non-empty sector list")
    sectors = tuple(
        _range_from_document(row, f"deployment_policy.sectors[{index}]")
        for index, row in enumerate(rows)
    )
    parent = raw["parent_allocation_digest"]
    if parent is not None and not isinstance(parent, str):
        raise SafetyMapError("deployment_policy.parent_allocation_digest must be null or text")
    rebuilt = build_artifact_application_allocation(
        sectors,
        driver_proof_digest=_required_string(
            raw["driver_proof_digest"], "deployment_policy.driver_proof_digest"
        ),
        creation_map_digest=_required_string(
            raw["creation_map_digest"], "deployment_policy.creation_map_digest"
        ),
        creation_artifact_digest=_required_string(
            raw["creation_artifact_digest"], "deployment_policy.creation_artifact_digest"
        ),
        parent_allocation_digest=parent,
    )
    if raw["allocation_digest"] != rebuilt["allocation_digest"]:
        raise SafetyMapError("artifact allocation digest mismatch")
    return rebuilt


@dataclass(frozen=True, slots=True)
class GenericSafetyMapDocument:
    """A schema-v3 generic map with physical and optional application ownership."""

    board_id: str
    identity: GenericMapIdentity
    authority_source: Mapping[str, str]
    source_digests: GenericSourceDigests
    geometry: GenericMapGeometry
    partitions: MapPartitions
    deployment_policy: Mapping[str, object]
    regions: tuple[SafetyRegion, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "board_id", _require_board_id(self.board_id))
        policy = _validated_generic_deployment_policy(self.deployment_policy)
        object.__setattr__(self, "deployment_policy", policy)
        if self.partitions.bootloader is not None:
            raise SafetyMapError("generic application policy cannot create a bootloader partition")
        if policy["kind"] == "none":
            if self.partitions.application is not None:
                raise SafetyMapError(
                    "generic map cannot claim a deployment partition without allocation"
                )
        else:
            sectors = tuple(
                _range_from_document(item, "deployment_policy.sectors")
                for item in cast(list[object], policy["sectors"])
            )
            allocation = AddressRange(sectors[0].start, sectors[-1].end)
            if self.partitions.application != allocation:
                raise SafetyMapError("application partition must equal the allocated sector envelope")
            geometry_sectors = (
                tuple(item.address_range for item in self.geometry.erase_sectors)
                if self.geometry.erase_sectors
                else ()
            )
            if not geometry_sectors or any(item not in geometry_sectors for item in sectors):
                raise SafetyMapError("allocated sectors must be exact verified erase sectors")
        source = dict(self.authority_source)
        required = {
            "kind",
            "support_id",
            "pack_id",
            "pack_filename",
            "pack_sha256",
            "pdsc_device",
            "pyocd_target",
        }
        if set(source) != required or source.get("kind") != "resolved_pack":
            raise SafetyMapError("generic authority_source is incomplete")
        if any(not isinstance(value, str) or not value for value in source.values()):
            raise SafetyMapError("generic authority_source values must be non-empty strings")
        if source["support_id"] != self.identity.support_id:
            raise SafetyMapError("generic authority_source support_id does not match identity")
        if source["pyocd_target"] != self.identity.pyocd_target:
            raise SafetyMapError("generic authority_source target does not match identity")
        if _DIGEST.fullmatch(source["pack_sha256"]) is None:
            raise SafetyMapError("generic authority_source pack_sha256 must be a lowercase SHA-256")
        canonical_regions = tuple(sorted(set(self.regions), key=_region_sort_key))
        deployment_regions = tuple(
            region for region in canonical_regions if region.kind is RegionKind.APPLICATION_FLASH
        )
        if any(region.kind is RegionKind.BOOTLOADER_FLASH for region in canonical_regions):
            raise SafetyMapError("generic application policy cannot persist a bootloader region")
        if policy["kind"] == "none" and deployment_regions:
            raise SafetyMapError("generic map without allocation cannot persist application regions")
        if policy["kind"] != "none" and tuple(
            region.address_range for region in deployment_regions
        ) != (self.partitions.application,):
            raise SafetyMapError("generic allocation requires exactly its application region")
        if not canonical_regions:
            raise SafetyMapError("a generic map requires physical regions")
        flash_ranges = tuple(
            region.address_range
            for region in canonical_regions
            if region.kind is RegionKind.PHYSICAL_FLASH
        )
        ram_ranges = tuple(
            region.address_range
            for region in canonical_regions
            if region.kind is RegionKind.PHYSICAL_RAM
        )
        if tuple(sorted(flash_ranges)) != self.geometry.physical_flash:
            raise SafetyMapError("generic map physical flash regions must match geometry exactly")
        if tuple(sorted(ram_ranges)) != self.geometry.physical_ram:
            raise SafetyMapError("generic map physical RAM regions must match geometry exactly")
        object.__setattr__(self, "authority_source", source)
        object.__setattr__(self, "regions", canonical_regions)

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": 3,
            "board_id": self.board_id,
            "identity": self.identity.to_document(),
            "authority_source": dict(sorted(self.authority_source.items())),
            "source_digests": self.source_digests.to_document(),
            "geometry": self.geometry.to_document(),
            "partitions": self.partitions.to_document(),
            "deployment_policy": dict(self.deployment_policy),
            "regions": [region.to_document() for region in self.regions],
        }

    @property
    def canonical_digest(self) -> str:
        return _domain_digest("canonical-generic-map", self.to_document())

    @property
    def safety_map(self) -> SafetyMap:
        return SafetyMap(list(self.regions))

    def to_safety_map(self) -> SafetyMap:
        return self.safety_map

    @classmethod
    def from_document(cls, value: object) -> "GenericSafetyMapDocument":
        raw = _exact_mapping(
            value,
            {
                "schema_version", "board_id", "identity", "authority_source", "source_digests",
                "geometry", "partitions", "deployment_policy", "regions",
            },
            "generic memory map",
        )
        if raw["schema_version"] != 3:
            raise SafetyMapError("unsupported generic memory-map schema version")
        if not isinstance(raw["authority_source"], Mapping):
            raise SafetyMapError("generic authority_source must be an object")
        if not isinstance(raw["deployment_policy"], Mapping):
            raise SafetyMapError("generic deployment_policy must be an object")
        rows = raw["regions"]
        if not isinstance(rows, list) or not rows:
            raise SafetyMapError("generic memory map regions must be a non-empty list")
        authority_source = _exact_mapping(
            raw["authority_source"],
            {
                "kind",
                "support_id",
                "pack_id",
                "pack_filename",
                "pack_sha256",
                "pdsc_device",
                "pyocd_target",
            },
            "generic authority_source",
        )
        if any(not isinstance(item, str) for item in authority_source.values()):
            raise SafetyMapError("generic authority_source values must be strings")
        return cls(
            _require_board_id(raw["board_id"]),
            GenericMapIdentity.from_document(raw["identity"]),
            cast(dict[str, str], authority_source),
            GenericSourceDigests.from_document(raw["source_digests"]),
            GenericMapGeometry.from_document(raw["geometry"]),
            MapPartitions.from_document(raw["partitions"]),
            dict(raw["deployment_policy"]),
            tuple(_region_from_document(row, f"regions[{index}]") for index, row in enumerate(rows)),
        )


def generic_map_with_allocation(
    document: GenericSafetyMapDocument,
    deployment_policy: Mapping[str, object],
) -> GenericSafetyMapDocument:
    """Return a first or monotonically expanded artifact-derived allocation."""

    policy = _validated_generic_deployment_policy(deployment_policy)
    if policy["kind"] != "artifact_application_allocation":
        raise SafetyMapError("allocation successor requires artifact_application_allocation policy")
    prior_digest = (
        None
        if document.deployment_policy == {"kind": "none"}
        else cast(str, document.deployment_policy["allocation_digest"])
    )
    if policy["parent_allocation_digest"] != prior_digest:
        raise SafetyMapError("allocation parent does not match the current generic map")
    sectors = tuple(
        _range_from_document(item, "deployment_policy.sectors")
        for item in cast(list[object], policy["sectors"])
    )
    application = AddressRange(sectors[0].start, sectors[-1].end)
    if (
        document.partitions.application is not None
        and not application.contains(document.partitions.application)
    ):
        raise SafetyMapError("generic application allocation cannot shrink")
    allocation_digest = cast(str, policy["allocation_digest"])
    region = SafetyRegion(
        "artifact-defined application allocation",
        RegionKind.APPLICATION_FLASH,
        application,
        (
            Provenance(
                SourceAuthority.DERIVED,
                allocation_digest,
                "server-derived artifact erase-sector allocation",
            ),
        ),
    )
    digests = GenericSourceDigests(
        document.source_digests.semantic_profile,
        document.source_digests.device_support,
        document.source_digests.datasheet_evidence,
        _domain_digest("generic-deployment-policy", policy),
        document.source_digests.map_generator_schema,
    )
    return GenericSafetyMapDocument(
        document.board_id,
        document.identity,
        document.authority_source,
        digests,
        document.geometry,
        MapPartitions(application),
        policy,
        (
            *(item for item in document.regions if item.kind is not RegionKind.APPLICATION_FLASH),
            region,
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
        self._guard = threading.RLock()

    def path(self, board_id: str) -> Path:
        return self.store.layout.safety_board(_require_board_id(board_id)) / "memory_map.yaml"

    def _legacy_paths(self, board_id: str) -> tuple[Path, Path]:
        root = self.store.layout.safety_board(_require_board_id(board_id))
        return root / "source_manifest.json", root / "safety_report.json"

    def _cleanup_legacy(self, board_id: str) -> None:
        for path in self._legacy_paths(board_id):
            self.store.remove_artifact(path)

    def commit(self, board_id: str, document: SafetyMapDocument | GenericSafetyMapDocument) -> Path:
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
        with self._guard:
            result = self.store.atomic_write_text(self.path(identity), text)
            self._cleanup_legacy(identity)
            return result

    def commit_if_current(
        self,
        board_id: str,
        expected_digest: str,
        document: SafetyMapDocument | GenericSafetyMapDocument,
    ) -> Path:
        """Compare-and-swap the sole map authority under the repository lock."""

        with self._guard:
            current = self.load_current(board_id)
            if current.canonical_digest != expected_digest:
                raise SafetyMapError("memory map changed before deployment allocation commit")
            return self.commit(board_id, document)

    def load_current(self, board_id: str) -> SafetyMapDocument | GenericSafetyMapDocument:
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
        if isinstance(raw, Mapping) and raw.get("schema_version") == 3:
            document = GenericSafetyMapDocument.from_document(raw)
        else:
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


def require_reconciled_authority(
    document: SafetyMapDocument | GenericSafetyMapDocument,
    *,
    generic_support_resolver: Callable[[str], Any] | None = None,
) -> None:
    """Fail closed unless the map identity and partitions match reviewed policy."""

    if isinstance(document, GenericSafetyMapDocument):
        _validated_generic_deployment_policy(document.deployment_policy)
        try:
            from pyocd_debug_mcp.setup_flow.device_support import resolve_registered_pack_support

            candidate = (
                resolve_registered_pack_support(document.identity.mcu_part_number)
                if generic_support_resolver is None
                else generic_support_resolver(document.identity.mcu_part_number)
            )
        except Exception as exc:  # noqa: BLE001 - authority resolution must fail closed
            raise SafetyMapError(f"generic map support authority is unavailable: {exc}") from exc
        expected = candidate.to_authority_document()
        source = dict(document.authority_source)
        if (
            document.identity.pyocd_target != candidate.pyocd_target
            or document.identity.support_id != candidate.support_id
            or any(source[key] != expected[key] for key in source)
        ):
            raise SafetyMapError("generic map identity does not match registered support authority")
        return

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
