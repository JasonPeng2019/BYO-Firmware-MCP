"""Single-file, stable safety-map construction and persistence."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]

from pyocd_debug_mcp.firmstore.store import FirmStore, ensure_no_persisted_authority
from pyocd_debug_mcp.safety.fingerprints import (
    FingerprintInputs,
    FingerprintSource,
    canonical_bytes,
    canonicalize,
)
from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionKind,
    SafetyMap,
    SafetyRegion,
    SourceAuthority,
)

SAFETY_MAP_SCHEMA_VERSION = 2
NO_INTERNALS = "Relay this guidance conversationally and do not expose structured internals."
_BOARD_ID = re.compile(r"[a-z0-9_]{1,64}")
_REGION_SOURCE_GROUPS = frozenset(FingerprintSource)
_PROFILE_SAFETY_FIELDS = ("board_id", "mcu_part_number", "mcu_family", "pyocd_target")

SafetySetupStatus = Literal[
    "safety_setup_completed",
    "safety_setup_needs_user_input",
    "safety_setup_research_required",
    "safety_setup_incomplete",
    "safety_setup_conflict",
    "safety_setup_blocked",
    "safety_setup_unsupported_board",
]
IncompleteSafetyStatus = Literal[
    "safety_setup_needs_user_input",
    "safety_setup_research_required",
    "safety_setup_incomplete",
    "safety_setup_conflict",
    "safety_setup_blocked",
    "safety_setup_unsupported_board",
]


class SafetyArtifactError(RuntimeError):
    """The single persisted safety map is missing, malformed, or inconsistent."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_board_id(value: str) -> str:
    if _BOARD_ID.fullmatch(value) is None:
        raise SafetyArtifactError(
            "board_id must be 1-64 lowercase letters, numbers, or underscores"
        )
    return value


def _digest(value: object) -> str:
    return sha256(canonical_bytes(value)).hexdigest()


def _semantic_profile(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SafetyArtifactError("semantic profile evidence must be an object")
    document = {
        key: canonicalize(value[key])
        for key in _PROFILE_SAFETY_FIELDS
        if key in value and value[key] is not None
    }
    if not isinstance(document.get("board_id"), str) or not isinstance(
        document.get("mcu_part_number"), str
    ):
        raise SafetyArtifactError(
            "semantic profile evidence requires board_id and exact mcu_part_number"
        )
    return document


@dataclass(frozen=True, slots=True)
class RegionContribution:
    region: SafetyRegion
    source_groups: tuple[FingerprintSource, ...]

    def __post_init__(self) -> None:
        groups = tuple(sorted(set(self.source_groups), key=lambda item: item.value))
        if not groups or any(group not in _REGION_SOURCE_GROUPS for group in groups):
            raise SafetyArtifactError("a region requires an authoritative source group")
        object.__setattr__(self, "source_groups", groups)

    def to_document(self) -> dict[str, object]:
        return {
            **self.region.to_document(),
            "source_groups": [group.value for group in self.source_groups],
        }


@dataclass(frozen=True, slots=True)
class SafetyIssue:
    status: IncompleteSafetyStatus
    code: str
    message: str
    choices: tuple[Mapping[str, str], ...] = ()
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SafetySetupRequest:
    board_id: str
    continuation_id: str
    inputs: FingerprintInputs
    regions: tuple[RegionContribution, ...]
    issues: tuple[SafetyIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class SafetySetupResult:
    status: SafetySetupStatus
    board_id: str
    continuation_id: str
    agent_prompt: str
    choices: tuple[Mapping[str, str], ...]
    observed: Mapping[str, object]
    report_path: Path | None
    aggregate_fingerprint: str | None

    @property
    def map_digest(self) -> str | None:
        return self.aggregate_fingerprint

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "agent_prompt": self.agent_prompt,
            "choices": [dict(choice) for choice in self.choices],
            "observed": dict(self.observed),
            "constraints": [
                "Only server-owned reviewed facts define memory authority.",
                "Safety map construction never opens a hardware gate.",
                NO_INTERNALS,
            ],
            "rejected_candidates": [],
            "accepted_response": None,
            "validation_plan": [
                "load current reviewed sources",
                "rederive the complete stable map",
                "check conflicts and prohibited overlap",
                "atomically persist memory_map.yaml",
            ],
        }


@dataclass(frozen=True, slots=True)
class SafetyArtifacts:
    board_id: str
    map_digest: str
    regions: tuple[RegionContribution, ...]
    memory_map: Mapping[str, object]

    @property
    def identity(self) -> Mapping[str, object]:
        value = self.memory_map.get("identity")
        if not isinstance(value, Mapping):  # pragma: no cover - load invariant
            raise SafetyArtifactError("memory map identity is missing")
        return value

    @property
    def source_digests(self) -> Mapping[str, object]:
        value = self.memory_map.get("source_digests")
        if not isinstance(value, Mapping):  # pragma: no cover - load invariant
            raise SafetyArtifactError("memory map source digests are missing")
        return value

    @property
    def geometry(self) -> Mapping[str, object]:
        value = self.memory_map.get("geometry")
        if not isinstance(value, Mapping):  # pragma: no cover - load invariant
            raise SafetyArtifactError("memory map geometry is missing")
        return value

    @property
    def partitions(self) -> Mapping[str, object]:
        value = self.memory_map.get("partitions")
        if not isinstance(value, Mapping):  # pragma: no cover - load invariant
            raise SafetyArtifactError("memory map partitions are missing")
        return value


def _region_from_document(raw: object) -> RegionContribution:
    expected = {
        "name",
        "kind",
        "start",
        "end",
        "executable",
        "provenance",
        "source_groups",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise SafetyArtifactError("persisted safety region fields do not match schema v2")
    try:
        kind = RegionKind(raw["kind"])
        address_range = AddressRange(raw["start"], raw["end"])  # type: ignore[arg-type]
        groups = tuple(FingerprintSource(item) for item in raw["source_groups"])  # type: ignore[union-attr]
    except (TypeError, ValueError) as exc:
        raise SafetyArtifactError(f"invalid persisted safety region: {exc}") from exc
    rows = raw["provenance"]
    if not isinstance(rows, list) or not rows:
        raise SafetyArtifactError("persisted region provenance must be a non-empty list")
    provenance: list[Provenance] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"authority", "source_id", "detail"}:
            raise SafetyArtifactError("persisted provenance fields do not match schema v2")
        try:
            provenance.append(
                Provenance(
                    SourceAuthority(row["authority"]),
                    str(row["source_id"]),
                    str(row["detail"]),
                )
            )
        except (TypeError, ValueError) as exc:
            raise SafetyArtifactError(f"invalid persisted provenance: {exc}") from exc
    if not isinstance(raw["name"], str) or not isinstance(raw["executable"], bool):
        raise SafetyArtifactError("persisted region name/executable fields are invalid")
    return RegionContribution(
        SafetyRegion(
            raw["name"],
            kind,
            address_range,
            tuple(provenance),
            raw["executable"],
        ),
        groups,
    )


def _ambiguous_overlap(first: RegionContribution, second: RegionContribution) -> bool:
    if not first.region.address_range.overlaps(second.region.address_range):
        return False
    left = first.region.kind
    right = second.region.kind
    if left is right or RegionKind.PROHIBITED in {left, right}:
        return False
    allowed = {
        frozenset({RegionKind.PHYSICAL_FLASH, RegionKind.APPLICATION_FLASH}),
        frozenset({RegionKind.PHYSICAL_FLASH, RegionKind.BOOTLOADER_FLASH}),
        frozenset({RegionKind.PHYSICAL_RAM, RegionKind.RAM}),
        frozenset({RegionKind.ROM, RegionKind.ROM_BOOTLOADER}),
    }
    return frozenset({left, right}) not in allowed


def region_conflicts(
    regions: tuple[RegionContribution, ...],
) -> tuple[dict[str, object], ...]:
    safety = SafetyMap([item.region for item in regions])
    conflicts: list[dict[str, object]] = []
    for partition, prohibited in safety.partition_prohibited_conflicts():
        intersection = partition.address_range.intersection(prohibited.address_range)
        assert intersection is not None
        conflicts.append(
            {
                "code": "partition_prohibited_overlap",
                "regions": [partition.name, prohibited.name],
                "range": intersection.to_document(),
            }
        )
    for index, first in enumerate(regions):
        for second in regions[index + 1 :]:
            if _ambiguous_overlap(first, second):
                intersection = first.region.address_range.intersection(second.region.address_range)
                assert intersection is not None
                conflicts.append(
                    {
                        "code": "ambiguous_region_overlap",
                        "regions": [first.region.name, second.region.name],
                        "range": intersection.to_document(),
                    }
                )
    return tuple(sorted(conflicts, key=lambda item: (str(item["code"]), str(item["regions"]))))


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SafetyArtifactError(f"{label} must be an object")
    return value


def _stable_regions(request: SafetySetupRequest) -> tuple[RegionContribution, ...]:
    values = request.inputs.values()
    evidence = _require_mapping(values[FingerprintSource.EVIDENCE], "reviewed evidence")
    deployment = _require_mapping(evidence.get("deployment_policy"), "deployment policy")
    if deployment.get("application_authoritative") is not True:
        raise SafetyArtifactError(
            "reviewed application partition authority is unavailable; flashing remains closed"
        )
    try:
        application = AddressRange(
            deployment["application_start"], deployment["application_end"]  # type: ignore[arg-type]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SafetyArtifactError(f"reviewed application partition is invalid: {exc}") from exc
    provenance = (
        Provenance(
            SourceAuthority.RECONCILED,
            "reviewed_deployment_policy",
            "server-owned stable application partition",
        ),
    )
    stable = [
        item
        for item in request.regions
        if not set(item.source_groups).intersection(
            {
                FingerprintSource.APPLICATION_ARTIFACTS,
                FingerprintSource.BOOTLOADER_ARTIFACTS,
            }
        )
        and item.region.kind
        not in {RegionKind.APPLICATION_FLASH, RegionKind.BOOTLOADER_FLASH}
    ]
    stable.append(
        RegionContribution(
            SafetyRegion(
                "reviewed application partition",
                RegionKind.APPLICATION_FLASH,
                application,
                provenance,
                executable=False,
            ),
            (FingerprintSource.EVIDENCE, FingerprintSource.GEOMETRY),
        )
    )
    if deployment.get("bootloader_authoritative") is True:
        try:
            bootloader = AddressRange(
                deployment["bootloader_start"], deployment["bootloader_end"]  # type: ignore[arg-type]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SafetyArtifactError(f"reviewed bootloader partition is invalid: {exc}") from exc
        stable.append(
            RegionContribution(
                SafetyRegion(
                    "reviewed bootloader partition",
                    RegionKind.BOOTLOADER_FLASH,
                    bootloader,
                    provenance,
                    executable=False,
                ),
                (FingerprintSource.EVIDENCE, FingerprintSource.GEOMETRY),
            )
        )
    return tuple(
        sorted(
            stable,
            key=lambda item: (
                item.region.address_range.start,
                item.region.address_range.end,
                item.region.kind.value,
                item.region.name,
            ),
        )
    )


def build_memory_map(request: SafetySetupRequest) -> dict[str, object]:
    _require_board_id(request.board_id)
    values = request.inputs.values()
    profile = _semantic_profile(values[FingerprintSource.PROFILE])
    part_target = _require_mapping(values[FingerprintSource.PART_TARGET], "part/target evidence")
    identity = {
        key: part_target[key]
        for key in ("board_type", "mcu_part_number", "target")
        if key in part_target
    }
    if set(identity) != {"board_type", "mcu_part_number", "target"} or any(
        not isinstance(value, str) or not value.strip() for value in identity.values()
    ):
        raise SafetyArtifactError("map identity requires exact board type, MCU part, and target")
    if profile["board_id"] != request.board_id:
        raise SafetyArtifactError("semantic profile board_id does not match the requested board")
    if profile["mcu_part_number"] != identity["mcu_part_number"]:
        raise SafetyArtifactError("semantic profile MCU part does not match reviewed map identity")
    profile_target = profile.get("pyocd_target")
    if profile_target is not None and profile_target != identity["target"]:
        raise SafetyArtifactError("semantic profile target does not match reviewed map identity")
    geometry = dict(_require_mapping(values[FingerprintSource.GEOMETRY], "erase geometry"))
    for key in ("flash_start", "flash_end", "ram_start", "ram_end", "erase_size"):
        value = geometry.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise SafetyArtifactError(f"geometry field {key} must be an integer")
    evidence = _require_mapping(values[FingerprintSource.EVIDENCE], "reviewed evidence")
    deployment = _require_mapping(evidence.get("deployment_policy"), "deployment policy")
    regions = _stable_regions(request)
    conflicts = region_conflicts(regions)
    if conflicts:
        raise SafetyArtifactError(f"authoritative safety regions conflict: {conflicts}")
    source_digests = {
        "semantic_profile": _digest(profile),
        "device_support": _digest(values[FingerprintSource.PACK]),
        "official_evidence": _digest(
            {key: value for key, value in evidence.items() if key != "deployment_policy"}
        ),
        "generator_schema": _digest(values[FingerprintSource.SCHEMA]),
    }
    partitions: dict[str, object] = {
        "application": {
            "start": deployment["application_start"],
            "end": deployment["application_end"],
        },
        "bootloader": None,
    }
    if deployment.get("bootloader_authoritative") is True:
        partitions["bootloader"] = {
            "start": deployment["bootloader_start"],
            "end": deployment["bootloader_end"],
        }
    document: dict[str, object] = {
        "schema_version": SAFETY_MAP_SCHEMA_VERSION,
        "board_id": request.board_id,
        "identity": identity,
        "source_digests": source_digests,
        "geometry": canonicalize(geometry),
        "partitions": canonicalize(partitions),
        "regions": [item.to_document() for item in regions],
    }
    ensure_no_persisted_authority(document, location="memory map")
    return document


def build_documents(
    request: SafetySetupRequest,
    _legacy_fingerprints: object | None = None,
    *,
    status: str = "safety_refresh_preflight",
    prompt: str = "",
    conflicts: tuple[Mapping[str, object], ...] = (),
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    """Temporary source-compatible helper while v1 call sites are removed.

    Only the first document is authority and only it may be persisted. The two empty compatibility
    documents prevent old, unreachable preflight helpers from making a manifest or report.
    """

    del status, prompt, conflicts
    return build_memory_map(request), {}, {}


class SafetyArtifactRepository:
    """FirmStore adapter for the single authoritative memory map."""

    def __init__(self, store: FirmStore) -> None:
        self.store = store

    def paths(self, board_id: str) -> dict[str, Path]:
        board = _require_board_id(board_id)
        root = self.store.layout.safety_board(board)
        return {"memory_map": root / "memory_map.yaml"}

    def _remove_legacy_siblings(self, board_id: str) -> None:
        root = self.paths(board_id)["memory_map"].parent
        for name in ("source_manifest.json", "safety_report.json"):
            try:
                (root / name).unlink(missing_ok=True)
            except OSError as exc:
                raise SafetyArtifactError(f"cannot remove legacy safety file {name}: {exc}") from exc

    def commit(self, board_id: str, *, memory_map: Mapping[str, Any]) -> dict[str, Path]:
        ensure_no_persisted_authority(memory_map, location="memory map")
        paths = self.paths(board_id)
        payload = yaml.safe_dump(
            dict(memory_map), allow_unicode=True, default_flow_style=False, sort_keys=False
        ).encode("utf-8")
        self.store.atomic_write_bytes(paths["memory_map"], payload)
        self._remove_legacy_siblings(board_id)
        return paths

    def load_current(self, board_id: str) -> SafetyArtifacts:
        # V1 siblings are never authority. Remove only the two exact legacy filenames; do not
        # recursively delete or touch setup/validation reports elsewhere in .firm.
        self._remove_legacy_siblings(board_id)
        path = self.paths(board_id)["memory_map"]
        if not path.is_file():
            raise SafetyArtifactError("current memory_map.yaml is missing; run board_safety_refresh")
        try:
            memory = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise SafetyArtifactError(f"cannot load current memory map: {exc}") from exc
        expected = {
            "schema_version",
            "board_id",
            "identity",
            "source_digests",
            "geometry",
            "partitions",
            "regions",
        }
        if not isinstance(memory, Mapping) or set(memory) != expected:
            raise SafetyArtifactError(
                "memory map fields do not match schema v2; run board_safety_refresh"
            )
        if memory.get("schema_version") != SAFETY_MAP_SCHEMA_VERSION:
            raise SafetyArtifactError(
                "unsupported memory-map schema; run board_safety_refresh"
            )
        if memory.get("board_id") != board_id:
            raise SafetyArtifactError("memory map does not match the requested board")
        for name in ("identity", "source_digests", "geometry", "partitions"):
            if not isinstance(memory.get(name), Mapping):
                raise SafetyArtifactError(f"memory map {name} must be an object")
        rows = memory.get("regions")
        if not isinstance(rows, list) or not rows:
            raise SafetyArtifactError("memory map requires at least one region")
        regions = tuple(_region_from_document(row) for row in rows)
        SafetyMap([item.region for item in regions])
        if region_conflicts(regions):
            raise SafetyArtifactError("memory map contains conflicting authoritative regions")
        document = dict(memory)
        return SafetyArtifacts(board_id, _digest(document), regions, document)


def require_reconciled_authority(artifacts: SafetyArtifacts) -> None:
    """Validate the self-contained structural authority required for guarded I/O."""

    if set(artifacts.identity) != {"board_type", "mcu_part_number", "target"}:
        raise SafetyArtifactError("memory map has incomplete board identity")
    required_digests = {
        "semantic_profile",
        "device_support",
        "official_evidence",
        "generator_schema",
    }
    if set(artifacts.source_digests) != required_digests or any(
        not isinstance(value, str) or len(value) != 64
        for value in artifacts.source_digests.values()
    ):
        raise SafetyArtifactError("memory map has incomplete semantic source digests")
    for key in ("flash_start", "flash_end", "ram_start", "ram_end", "erase_size"):
        value = artifacts.geometry.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise SafetyArtifactError(f"memory map geometry field {key} is invalid")
    application = artifacts.partitions.get("application")
    if not isinstance(application, Mapping):
        raise SafetyArtifactError("memory map has no authoritative application partition")
    try:
        AddressRange(application["start"], application["end"])  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError) as exc:
        raise SafetyArtifactError(f"memory map application partition is invalid: {exc}") from exc
    required_kinds = {
        RegionKind.PHYSICAL_FLASH,
        RegionKind.PHYSICAL_RAM,
        RegionKind.RAM,
        RegionKind.APPLICATION_FLASH,
    }
    present = {item.region.kind for item in artifacts.regions}
    if not required_kinds.issubset(present):
        missing = sorted(kind.value for kind in required_kinds - present)
        raise SafetyArtifactError(f"memory map lacks required regions: {missing}")


class SafetyMapBuilder:
    def __init__(self, store: FirmStore) -> None:
        self.repository = SafetyArtifactRepository(store)

    def candidate(self, request: SafetySetupRequest) -> SafetyArtifacts:
        memory = build_memory_map(request)
        rows = memory["regions"]
        assert isinstance(rows, list)
        regions = tuple(_region_from_document(row) for row in rows)
        return SafetyArtifacts(request.board_id, _digest(memory), regions, memory)

    def build(self, request: SafetySetupRequest) -> SafetySetupResult:
        _require_board_id(request.board_id)
        if not request.continuation_id.strip():
            raise SafetyArtifactError("continuation_id must be non-empty")
        if request.issues:
            issue = request.issues[0]
            prompt = f"{issue.message.strip()} {NO_INTERNALS}"
            return SafetySetupResult(
                issue.status,
                request.board_id,
                request.continuation_id,
                prompt,
                issue.choices,
                {"code": issue.code, **dict(issue.details)},
                None,
                None,
            )
        try:
            candidate = self.candidate(request)
        except SafetyArtifactError as exc:
            prompt = f"Safety map construction failed: {exc}. {NO_INTERNALS}"
            return SafetySetupResult(
                "safety_setup_blocked",
                request.board_id,
                request.continuation_id,
                prompt,
                (),
                {"code": "safety/map-invalid", "reason": str(exc)},
                None,
                None,
            )
        try:
            current = self.repository.load_current(request.board_id)
        except SafetyArtifactError:
            current = None
        unchanged = current is not None and current.map_digest == candidate.map_digest
        paths = self.repository.commit(request.board_id, memory_map=candidate.memory_map)
        prompt = (
            "Safety map is current. Run board_validate only when this server run has no valid "
            f"live identity proof. {NO_INTERNALS}"
        )
        return SafetySetupResult(
            "safety_setup_completed",
            request.board_id,
            request.continuation_id,
            prompt,
            (),
            {
                "memory_map": str(paths["memory_map"]),
                "region_count": len(candidate.regions),
                "unchanged_rebuild": unchanged,
            },
            None,
            candidate.map_digest,
        )
