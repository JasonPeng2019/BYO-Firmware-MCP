"""Strict deterministic double verification for hardware memory facts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final, Literal, Mapping

from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionError,
    RegionKind,
    SafetyRegion,
    SourceAuthority,
)

EVIDENCE_SCHEMA_VERSION: Final = 1
_TOP_FIELDS: Final = frozenset({"schema_version", "role", "device", "sources", "regions"})
_DEVICE_FIELDS: Final = frozenset({"mcu_part_number", "target"})
_SOURCE_FIELDS: Final = frozenset({"kind", "identifier", "version", "revision"})
_REGION_FIELDS: Final = frozenset(
    {
        "fact_id",
        "name",
        "name_aliases",
        "kind",
        "start",
        "end",
        "range_convention",
        "address_aliases",
        "bank",
        "block",
    }
)
_ALIAS_FIELDS: Final = frozenset({"start", "end", "range_convention"})
_TOKEN = re.compile(r"[^a-z0-9]+")


class EvidenceError(ValueError):
    """Evidence does not satisfy the strict comparison schema."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvidenceRole(str, Enum):
    DEVICE_SUPPORT = "device_support"
    OFFICIAL_DOCUMENT = "official_document"


class SourceKind(str, Enum):
    PACK = "pack"
    CMSIS = "cmsis"
    SVD = "svd"
    TARGET = "target"
    DATASHEET = "datasheet"
    REFERENCE_MANUAL = "reference_manual"


_DEVICE_SUPPORT_KINDS = frozenset(
    {SourceKind.PACK, SourceKind.CMSIS, SourceKind.SVD, SourceKind.TARGET}
)
_OFFICIAL_KINDS = frozenset({SourceKind.DATASHEET, SourceKind.REFERENCE_MANUAL})


def _exact_fields(raw: object, expected: frozenset[str], location: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise EvidenceError("evidence/type", f"{location} must be an object")
    if any(not isinstance(key, str) for key in raw):
        raise EvidenceError("evidence/field-set", f"{location} field names must be strings")
    actual = {str(key) for key in raw}
    if actual != expected:
        raise EvidenceError(
            "evidence/field-set",
            f"{location} fields must match exactly; missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}",
        )
    return raw


def _text(value: object, location: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError("evidence/text", f"{location} must be a non-empty string")
    return value.strip()


def _integer(value: object, location: str) -> int:
    if isinstance(value, bool):
        raise EvidenceError("evidence/integer", f"{location} must be an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = int(value, 0)
        except ValueError as exc:
            raise EvidenceError(
                "evidence/integer", f"{location} must be decimal or 0x-prefixed hexadecimal"
            ) from exc
    else:
        raise EvidenceError(
            "evidence/integer", f"{location} must be decimal or 0x-prefixed hexadecimal"
        )
    if result < 0:
        raise EvidenceError("evidence/integer", f"{location} must be non-negative")
    return result


def _range(raw: Mapping[str, Any], location: str) -> AddressRange:
    convention = raw["range_convention"]
    if convention not in {"half_open", "inclusive_end"}:
        raise EvidenceError(
            "evidence/range-convention",
            f"{location}.range_convention must be half_open or inclusive_end",
        )
    start = _integer(raw["start"], f"{location}.start")
    end = _integer(raw["end"], f"{location}.end")
    if convention == "inclusive_end":
        end += 1
    try:
        return AddressRange(start, end)
    except RegionError as exc:
        raise EvidenceError("evidence/range", f"Invalid {location}: {exc}") from exc


def _normalized(value: str) -> str:
    return _TOKEN.sub("", value.casefold())


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    kind: SourceKind
    identifier: str
    version: str
    revision: str


@dataclass(frozen=True, slots=True)
class EvidenceRegion:
    fact_id: str
    name: str
    name_aliases: tuple[str, ...]
    kind: RegionKind
    address_range: AddressRange
    address_aliases: tuple[AddressRange, ...]
    bank: str
    block: str

    @property
    def name_keys(self) -> frozenset[str]:
        return frozenset(_normalized(value) for value in (self.name, *self.name_aliases))

    @property
    def all_ranges(self) -> tuple[AddressRange, ...]:
        return (self.address_range, *self.address_aliases)


@dataclass(frozen=True, slots=True)
class HardwareEvidence:
    role: EvidenceRole
    mcu_part_number: str
    target: str | None
    sources: tuple[EvidenceSource, ...]
    regions: tuple[EvidenceRegion, ...]

    @classmethod
    def from_document(cls, document: object) -> HardwareEvidence:
        raw = _exact_fields(document, _TOP_FIELDS, "evidence")
        if raw["schema_version"] != EVIDENCE_SCHEMA_VERSION:
            raise EvidenceError(
                "evidence/schema-version",
                f"schema_version must be {EVIDENCE_SCHEMA_VERSION}",
            )
        try:
            role = EvidenceRole(raw["role"])
        except (TypeError, ValueError) as exc:
            raise EvidenceError(
                "evidence/role", "role must be device_support or official_document"
            ) from exc
        device = _exact_fields(raw["device"], _DEVICE_FIELDS, "evidence.device")
        part = _text(device["mcu_part_number"], "evidence.device.mcu_part_number")
        target = _text(device["target"], "evidence.device.target", nullable=True)
        assert part is not None

        source_rows = raw["sources"]
        if not isinstance(source_rows, list) or not source_rows:
            raise EvidenceError("evidence/sources", "sources must be a non-empty list")
        sources: list[EvidenceSource] = []
        for index, item in enumerate(source_rows):
            source = _exact_fields(item, _SOURCE_FIELDS, f"evidence.sources[{index}]")
            try:
                kind = SourceKind(source["kind"])
            except (TypeError, ValueError) as exc:
                raise EvidenceError(
                    "evidence/source-kind", f"Unsupported source kind at index {index}"
                ) from exc
            identifier = _text(source["identifier"], f"evidence.sources[{index}].identifier")
            version = _text(source["version"], f"evidence.sources[{index}].version")
            revision = _text(source["revision"], f"evidence.sources[{index}].revision")
            assert identifier is not None and version is not None and revision is not None
            sources.append(EvidenceSource(kind, identifier, version, revision))
        allowed_source_kinds = (
            _DEVICE_SUPPORT_KINDS if role is EvidenceRole.DEVICE_SUPPORT else _OFFICIAL_KINDS
        )
        if any(source.kind not in allowed_source_kinds for source in sources):
            raise EvidenceError(
                "evidence/source-authority",
                f"{role.value} evidence contains a source from the wrong authority",
            )
        if role is EvidenceRole.DEVICE_SUPPORT and target is None:
            raise EvidenceError(
                "evidence/target", "device_support evidence requires an exact target"
            )
        sources.sort(key=lambda item: (item.kind.value, item.identifier, item.version, item.revision))
        if len(set(sources)) != len(sources):
            raise EvidenceError("evidence/sources", "sources must not contain duplicates")

        region_rows = raw["regions"]
        if not isinstance(region_rows, list) or not region_rows:
            raise EvidenceError("evidence/regions", "regions must be a non-empty list")
        regions: list[EvidenceRegion] = []
        for index, item in enumerate(region_rows):
            region = _exact_fields(item, _REGION_FIELDS, f"evidence.regions[{index}]")
            fact_id = _text(region["fact_id"], f"evidence.regions[{index}].fact_id")
            name = _text(region["name"], f"evidence.regions[{index}].name")
            aliases_raw = region["name_aliases"]
            if not isinstance(aliases_raw, list) or any(
                not isinstance(alias, str) or not alias.strip() for alias in aliases_raw
            ):
                raise EvidenceError(
                    "evidence/name-aliases", "name_aliases must be a list of non-empty strings"
                )
            name_aliases = tuple(
                sorted((alias.strip() for alias in aliases_raw), key=lambda item: (_normalized(item), item))
            )
            if len({_normalized(alias) for alias in name_aliases}) != len(name_aliases):
                raise EvidenceError(
                    "evidence/name-aliases", "name_aliases must be unique after normalization"
                )
            try:
                kind = RegionKind(region["kind"])
            except (TypeError, ValueError) as exc:
                raise EvidenceError(
                    "evidence/region-kind", f"Unsupported region kind at index {index}"
                ) from exc
            if kind is RegionKind.UNKNOWN:
                raise EvidenceError(
                    "evidence/region-kind", "UNKNOWN cannot be supplied as evidence"
                )
            primary = _range(region, f"evidence.regions[{index}]")
            address_alias_rows = region["address_aliases"]
            if not isinstance(address_alias_rows, list):
                raise EvidenceError("evidence/address-aliases", "address_aliases must be a list")
            address_aliases = tuple(
                sorted(
                    (
                        _range(
                            _exact_fields(
                                alias, _ALIAS_FIELDS, f"evidence.regions[{index}].address_aliases"
                            ),
                            f"evidence.regions[{index}].address_aliases",
                        )
                        for alias in address_alias_rows
                    )
                )
            )
            if len(set(address_aliases)) != len(address_aliases):
                raise EvidenceError("evidence/address-aliases", "address aliases must be unique")
            if primary in address_aliases:
                raise EvidenceError(
                    "evidence/address-aliases",
                    "address aliases must not repeat the primary range",
                )
            bank = _text(region["bank"], f"evidence.regions[{index}].bank")
            block = _text(region["block"], f"evidence.regions[{index}].block")
            assert (
                fact_id is not None and name is not None and bank is not None and block is not None
            )
            regions.append(
                EvidenceRegion(
                    fact_id,
                    name,
                    name_aliases,
                    kind,
                    primary,
                    address_aliases,
                    bank,
                    block,
                )
            )
        if len({region.fact_id for region in regions}) != len(regions):
            raise EvidenceError("evidence/fact-id", "region fact_id values must be unique")
        regions.sort(
            key=lambda item: (
                item.fact_id,
                _normalized(item.name),
                item.kind.value,
                item.address_range,
                _normalized(item.bank),
                _normalized(item.block),
            )
        )
        return cls(role, part, target, tuple(sources), tuple(regions))


@dataclass(frozen=True, slots=True)
class VerificationConflict:
    code: str
    fact_id: str | None
    message: str
    device_support_value: object
    official_document_value: object


@dataclass(frozen=True, slots=True)
class ReconciledRegion:
    fact_id: str
    name: str
    kind: RegionKind
    address_range: AddressRange
    address_aliases: tuple[AddressRange, ...]
    bank: str
    block: str
    reconciliations: tuple[str, ...]
    source_ids: tuple[str, ...]

    def to_safety_region(self) -> SafetyRegion:
        return SafetyRegion(
            self.name,
            self.kind,
            self.address_range,
            (
                Provenance(
                    SourceAuthority.RECONCILED,
                    "+".join(self.source_ids),
                    ", ".join(self.reconciliations) or "exact two-source agreement",
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    status: Literal["agreement", "conflict"]
    regions: tuple[ReconciledRegion, ...]
    conflicts: tuple[VerificationConflict, ...]
    device_support_sources: tuple[EvidenceSource, ...]
    official_document_sources: tuple[EvidenceSource, ...]

    @property
    def accepted(self) -> bool:
        return self.status == "agreement" and not self.conflicts


def _source_ids(evidence: HardwareEvidence) -> tuple[str, ...]:
    return tuple(
        sorted(
            f"{source.kind.value}:{source.identifier}@{source.version}/{source.revision}"
            for source in evidence.sources
        )
    )


def _matching_range(
    support: EvidenceRegion, official: EvidenceRegion
) -> tuple[AddressRange | None, bool]:
    if support.address_range == official.address_range:
        return support.address_range, False
    for support_range in support.all_ranges:
        for official_range in official.all_ranges:
            if support_range == official_range:
                return support_range, True
    return None, False


def reconcile_hardware_evidence(
    *,
    expected_mcu_part_number: str,
    expected_target: str,
    device_support: HardwareEvidence,
    official_document: HardwareEvidence,
) -> ReconciliationResult:
    """Reconcile two independent authorities; any ambiguity or conflict fails closed."""

    if device_support.role is not EvidenceRole.DEVICE_SUPPORT:
        raise EvidenceError("verify/role", "device_support has the wrong evidence role")
    if official_document.role is not EvidenceRole.OFFICIAL_DOCUMENT:
        raise EvidenceError("verify/role", "official_document has the wrong evidence role")
    if not expected_mcu_part_number.strip() or not expected_target.strip():
        raise EvidenceError("verify/anchor", "expected part number and target are required")

    conflicts: list[VerificationConflict] = []
    for label, actual in (
        ("device_support", device_support.mcu_part_number),
        ("official_document", official_document.mcu_part_number),
    ):
        if actual != expected_mcu_part_number:
            conflicts.append(
                VerificationConflict(
                    "verify/device-variant",
                    None,
                    f"{label} exact MCU variant does not match the profile",
                    device_support.mcu_part_number,
                    official_document.mcu_part_number,
                )
            )
    if device_support.target != expected_target or (
        official_document.target is not None and official_document.target != expected_target
    ):
        conflicts.append(
            VerificationConflict(
                "verify/target",
                None,
                "Target identity does not agree with the expected profile target",
                device_support.target,
                official_document.target,
            )
        )

    reconciled: list[ReconciledRegion] = []
    used_official: set[str] = set()
    support_source_ids = _source_ids(device_support)
    official_source_ids = _source_ids(official_document)
    for support in device_support.regions:
        candidates = sorted(
            [
            official
            for official in official_document.regions
            if official.fact_id == support.fact_id
            or bool(official.name_keys.intersection(support.name_keys))
            ],
            key=lambda item: item.fact_id,
        )
        if len(candidates) != 1:
            conflicts.append(
                VerificationConflict(
                    "verify/missing-or-ambiguous-fact",
                    support.fact_id,
                    "A device-support fact must match exactly one official-document fact",
                    support.fact_id,
                    [candidate.fact_id for candidate in candidates],
                )
            )
            continue
        official = candidates[0]
        if official.fact_id in used_official:
            conflicts.append(
                VerificationConflict(
                    "verify/duplicate-match",
                    support.fact_id,
                    "One official fact cannot verify more than one device-support fact",
                    support.fact_id,
                    official.fact_id,
                )
            )
            continue
        used_official.add(official.fact_id)
        fact_conflict = False
        if not support.name_keys.intersection(official.name_keys):
            conflicts.append(
                VerificationConflict(
                    "verify/name-alias",
                    support.fact_id,
                    "Region names differ without an explicit shared alias",
                    [support.name, *support.name_aliases],
                    [official.name, *official.name_aliases],
                )
            )
            fact_conflict = True
        if support.kind is not official.kind:
            conflicts.append(
                VerificationConflict(
                    "verify/kind",
                    support.fact_id,
                    "Region kinds disagree",
                    support.kind.value,
                    official.kind.value,
                )
            )
            fact_conflict = True
        agreed_range, used_address_alias = _matching_range(support, official)
        if agreed_range is None:
            conflicts.append(
                VerificationConflict(
                    "verify/address",
                    support.fact_id,
                    "No primary or explicitly declared alias range agrees",
                    [item.to_document() for item in support.all_ranges],
                    [item.to_document() for item in official.all_ranges],
                )
            )
            fact_conflict = True
        if _normalized(support.bank) != _normalized(official.bank):
            conflicts.append(
                VerificationConflict(
                    "verify/bank",
                    support.fact_id,
                    "Bank boundaries or identities disagree",
                    support.bank,
                    official.bank,
                )
            )
            fact_conflict = True
        if _normalized(support.block) != _normalized(official.block):
            conflicts.append(
                VerificationConflict(
                    "verify/block",
                    support.fact_id,
                    "Register-block identities disagree",
                    support.block,
                    official.block,
                )
            )
            fact_conflict = True
        if fact_conflict:
            continue
        assert agreed_range is not None
        reconciliation: list[str] = []
        if support.fact_id != official.fact_id or support.name != official.name:
            reconciliation.append("explicit_name_alias")
        if used_address_alias:
            reconciliation.append("explicit_address_alias")
        if support.address_range != official.address_range and not used_address_alias:
            reconciliation.append("range_convention")
        reconciled.append(
            ReconciledRegion(
                support.fact_id,
                support.name,
                support.kind,
                agreed_range,
                tuple(
                    sorted(
                        item
                        for item in set((*support.all_ranges, *official.all_ranges))
                        if item != agreed_range
                    )
                ),
                support.bank,
                support.block,
                tuple(reconciliation),
                (*support_source_ids, *official_source_ids),
            )
        )

    for official in official_document.regions:
        if official.fact_id not in used_official:
            conflicts.append(
                VerificationConflict(
                    "verify/unmatched-official-fact",
                    official.fact_id,
                    "Official-document fact has no unique device-support counterpart",
                    None,
                    official.fact_id,
                )
            )
    return ReconciliationResult(
        "conflict" if conflicts else "agreement",
        tuple(reconciled) if not conflicts else (),
        tuple(conflicts),
        device_support.sources,
        official_document.sources,
    )
