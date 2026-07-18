"""Safety-map construction, status payloads, and FirmStore-owned persistence."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]

from pyocd_debug_mcp.firmstore.store import FirmStore, ensure_no_persisted_authority
from pyocd_debug_mcp.safety.fingerprints import (
    FingerprintInputs,
    FingerprintSet,
    FingerprintSource,
)
from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionKind,
    SafetyMap,
    SafetyRegion,
    SourceAuthority,
)

SAFETY_MAP_SCHEMA_VERSION = 1
NO_INTERNALS = "Relay this guidance conversationally and do not expose structured internals."
_BOARD_ID = re.compile(r"[a-z0-9_]{1,64}")
_REGION_SOURCE_GROUPS = frozenset(
    {
        FingerprintSource.PACK,
        FingerprintSource.EVIDENCE,
        FingerprintSource.APPLICATION_ARTIFACTS,
        FingerprintSource.BOOTLOADER_ARTIFACTS,
        FingerprintSource.GEOMETRY,
    }
)

SafetySetupStatus = Literal[
    "safety_setup_completed",
    "safety_setup_needs_user_input",
    "safety_setup_research_required",
    "safety_setup_incomplete",
    "safety_setup_conflict",
    "safety_setup_blocked",
]
IncompleteSafetyStatus = Literal[
    "safety_setup_needs_user_input",
    "safety_setup_research_required",
    "safety_setup_incomplete",
    "safety_setup_conflict",
    "safety_setup_blocked",
]


class SafetyArtifactError(RuntimeError):
    """Persisted safety artifacts are missing, stale, malformed, or inconsistent."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_board_id(value: str) -> str:
    if _BOARD_ID.fullmatch(value) is None:
        raise SafetyArtifactError("board_id must be 1-64 lowercase letters, numbers, or underscores")
    return value


@dataclass(frozen=True, slots=True)
class RegionContribution:
    region: SafetyRegion
    source_groups: tuple[FingerprintSource, ...]

    def __post_init__(self) -> None:
        groups = tuple(sorted(set(self.source_groups), key=lambda item: item.value))
        if not groups or any(group not in _REGION_SOURCE_GROUPS for group in groups):
            raise SafetyArtifactError("a region requires one or more authoritative source groups")
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
    report_path: Path
    aggregate_fingerprint: str | None

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "continuation_id": self.continuation_id,
            "agent_prompt": self.agent_prompt,
            "choices": [dict(choice) for choice in self.choices],
            "observed": dict(self.observed),
            "constraints": [
                "Only server-loaded build and doubly verified hardware facts define regions.",
                "Safety setup never opens a gate; successful board validation owns gate opening.",
                NO_INTERNALS,
            ],
            "rejected_candidates": [],
            "accepted_response": None,
            "validation_plan": [
                "resolve authoritative sources",
                "verify and classify regions",
                "check partition/prohibited overlaps",
                "fingerprint and atomically persist the map",
            ],
        }


@dataclass(frozen=True, slots=True)
class SafetyArtifacts:
    board_id: str
    fingerprints: FingerprintSet
    regions: tuple[RegionContribution, ...]
    memory_map: Mapping[str, object]
    source_manifest: Mapping[str, object]


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
        raise SafetyArtifactError("persisted safety region fields do not match schema v1")
    try:
        kind = RegionKind(raw["kind"])
        address_range = AddressRange(raw["start"], raw["end"])  # type: ignore[arg-type]
        groups = tuple(FingerprintSource(item) for item in raw["source_groups"])  # type: ignore[union-attr]
    except (TypeError, ValueError) as exc:
        raise SafetyArtifactError(f"invalid persisted safety region: {exc}") from exc
    provenance_rows = raw["provenance"]
    if not isinstance(provenance_rows, list):
        raise SafetyArtifactError("persisted region provenance must be a list")
    provenance: list[Provenance] = []
    for row in provenance_rows:
        if not isinstance(row, Mapping) or set(row) != {"authority", "source_id", "detail"}:
            raise SafetyArtifactError("persisted provenance fields do not match schema v1")
        try:
            provenance.append(
                Provenance(
                    SourceAuthority(row["authority"]),
                    str(row["source_id"]),
                    str(row["detail"]),
                )
            )
        except ValueError as exc:
            raise SafetyArtifactError(f"invalid persisted provenance: {exc}") from exc
    if not isinstance(raw["name"], str) or not isinstance(raw["executable"], bool):
        raise SafetyArtifactError("persisted region name/executable values have invalid types")
    return RegionContribution(
        SafetyRegion(raw["name"], kind, address_range, tuple(provenance), raw["executable"]),
        groups,
    )


class SafetyArtifactRepository:
    """The sole Task 13 adapter for current safety artifacts below FirmStore."""

    def __init__(self, store: FirmStore) -> None:
        self.store = store

    def paths(self, board_id: str) -> dict[str, Path]:
        root = self.store.layout.safety_board(_require_board_id(board_id))
        return {
            "memory_map": root / "memory_map.yaml",
            "source_manifest": root / "source_manifest.json",
            "safety_report": root / "safety_report.json",
        }

    def write_report(self, board_id: str, report: Mapping[str, Any]) -> Path:
        ensure_no_persisted_authority(report, location="safety report")
        path = self.paths(board_id)["safety_report"]
        return self.store.atomic_write_json(path, report)

    def commit(
        self,
        board_id: str,
        *,
        memory_map: Mapping[str, Any],
        source_manifest: Mapping[str, Any],
        safety_report: Mapping[str, Any],
    ) -> dict[str, Path]:
        for label, document in (
            ("memory map", memory_map),
            ("source manifest", source_manifest),
            ("safety report", safety_report),
        ):
            ensure_no_persisted_authority(document, location=label)
        paths = self.paths(board_id)
        yaml_payload = yaml.safe_dump(
            dict(memory_map), allow_unicode=True, default_flow_style=False, sort_keys=False
        ).encode("utf-8")
        json_payloads = {
            paths["source_manifest"]: (
                json.dumps(source_manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            ).encode("utf-8"),
            paths["safety_report"]: (
                json.dumps(safety_report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            ).encode("utf-8"),
        }
        self.store.atomic_write_bundle({paths["memory_map"]: yaml_payload, **json_payloads})
        return paths

    def load_current(self, board_id: str) -> SafetyArtifacts:
        paths = self.paths(board_id)
        if not paths["memory_map"].is_file() or not paths["source_manifest"].is_file():
            raise SafetyArtifactError("current safety map and source manifest are both required")
        try:
            memory = yaml.safe_load(paths["memory_map"].read_text(encoding="utf-8"))
            manifest = json.loads(paths["source_manifest"].read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            raise SafetyArtifactError(f"cannot load current safety artifacts: {exc}") from exc
        if not isinstance(memory, Mapping) or not isinstance(manifest, Mapping):
            raise SafetyArtifactError("current safety artifacts must be objects")
        if memory.get("schema_version") != SAFETY_MAP_SCHEMA_VERSION:
            raise SafetyArtifactError("unsupported memory-map schema version")
        if memory.get("board_id") != board_id or manifest.get("board_id") != board_id:
            raise SafetyArtifactError("safety artifacts do not match the requested board")
        fingerprints = FingerprintSet.from_document(memory.get("fingerprints"))
        manifest_fingerprints = FingerprintSet.from_document(manifest.get("fingerprints"))
        if fingerprints != manifest_fingerprints:
            raise SafetyArtifactError("memory map and source manifest fingerprints disagree")
        source_rows = manifest.get("sources")
        if not isinstance(source_rows, Mapping) or set(source_rows) != {
            source.value for source in FingerprintSource
        }:
            raise SafetyArtifactError("source manifest must contain every exact source group")
        source_documents: dict[FingerprintSource, object] = {}
        expected_sub = fingerprints.as_mapping()
        for source in FingerprintSource:
            row = source_rows[source.value]
            if not isinstance(row, Mapping) or set(row) != {"fingerprint", "evidence"}:
                raise SafetyArtifactError(
                    f"source manifest entry for {source.value} is malformed"
                )
            if row["fingerprint"] != expected_sub[source]:
                raise SafetyArtifactError(
                    f"source manifest entry for {source.value} is stale"
                )
            source_documents[source] = row["evidence"]
        recomputed = FingerprintSet.build(
            FingerprintInputs(
                source_documents[FingerprintSource.PROFILE],
                source_documents[FingerprintSource.PART_TARGET],
                source_documents[FingerprintSource.PACK],
                source_documents[FingerprintSource.EVIDENCE],
                source_documents[FingerprintSource.APPLICATION_ARTIFACTS],
                source_documents[FingerprintSource.BOOTLOADER_ARTIFACTS],
                source_documents[FingerprintSource.GEOMETRY],
                source_documents[FingerprintSource.SCHEMA],
            )
        )
        if recomputed != fingerprints:
            raise SafetyArtifactError("source manifest evidence is stale or fingerprint-mismatched")
        rows = memory.get("regions")
        if not isinstance(rows, list) or not rows:
            raise SafetyArtifactError("current safety map requires at least one region")
        regions = tuple(_region_from_document(row) for row in rows)
        SafetyMap([item.region for item in regions])
        return SafetyArtifacts(board_id, fingerprints, regions, memory, manifest)


def require_reconciled_authority(artifacts: SafetyArtifacts) -> None:
    """Reject legacy/synthetic maps before they can validate, refresh, or authorize I/O."""

    sources = artifacts.source_manifest.get("sources")
    if not isinstance(sources, Mapping):
        raise SafetyArtifactError("source manifest has no authoritative source records")

    def source_evidence(source: FingerprintSource) -> Mapping[str, object]:
        row = sources.get(source.value)
        evidence = row.get("evidence") if isinstance(row, Mapping) else None
        if not isinstance(evidence, Mapping):
            raise SafetyArtifactError(f"{source.value} authority evidence is missing")
        return evidence

    schema = source_evidence(FingerprintSource.SCHEMA)
    if schema.get("evidence") != 2 or schema.get("catalog") != 2:
        raise SafetyArtifactError(
            "legacy safety authority schema; rerun full board setup and safety setup"
        )
    evidence = source_evidence(FingerprintSource.EVIDENCE)
    pack = source_evidence(FingerprintSource.PACK)
    reconciliation = evidence.get("reconciliation")
    official = evidence.get("official_document")
    support = pack.get("document")
    if (
        not isinstance(reconciliation, Mapping)
        or reconciliation.get("status") != "agreement"
        or not isinstance(reconciliation.get("erase_geometry"), Mapping)
        or not isinstance(official, Mapping)
        or not isinstance(official.get("document"), Mapping)
        or official["document"].get("schema_version") != 2  # type: ignore[union-attr]
        or not isinstance(support, Mapping)
        or support.get("schema_version") != 2
    ):
        raise SafetyArtifactError(
            "safety sources lack strict two-source region and erase-geometry reconciliation"
        )
    part_target = source_evidence(FingerprintSource.PART_TARGET)
    board_type = part_target.get("board_type")
    part_number = part_target.get("mcu_part_number")
    target = part_target.get("target")
    if not all(isinstance(value, str) and value for value in (board_type, part_number, target)):
        raise SafetyArtifactError("safety authority has no exact board, part, and target anchors")
    try:
        from pyocd_debug_mcp.setup_flow.board_catalog import (
            BoardCatalogError,
            catalog_board,
        )
        from pyocd_debug_mcp.setup_flow.reviewed_evidence import (
            verify_persisted_reviewed_evidence,
        )

        catalog = catalog_board(str(board_type))
        if part_number != catalog.package_part_number or target != catalog.pyocd_target:
            raise BoardCatalogError("persisted part/target anchors do not match the catalog")
        bundle = verify_persisted_reviewed_evidence(catalog, pack, evidence)
    except Exception as exc:  # noqa: BLE001 - every authority-resolution failure closes the gate
        raise SafetyArtifactError(
            f"safety authority cannot be reverified from server-owned sources: {exc}"
        ) from exc
    expected_record = bundle.source_record()
    if pack != expected_record["device_support"]:
        raise SafetyArtifactError("persisted device-support authority record is not exact")
    if official != expected_record["official_document"]:
        raise SafetyArtifactError("persisted official authority record is not exact")
    if reconciliation != expected_record["reconciliation"]:
        raise SafetyArtifactError("persisted reconciliation record cannot be reproduced")
    geometry = source_evidence(FingerprintSource.GEOMETRY)
    reconciled_geometry = reconciliation["erase_geometry"]
    assert isinstance(reconciled_geometry, Mapping)
    if (
        geometry.get("erase_origin") != reconciled_geometry.get("erase_origin")
        or geometry.get("erase_size") != reconciled_geometry.get("erase_size")
    ):
        raise SafetyArtifactError("persisted erase geometry is not the reconciled geometry")

    authority_groups = {
        FingerprintSource.PACK,
        FingerprintSource.EVIDENCE,
        FingerprintSource.GEOMETRY,
    }
    hardware_regions = [
        contribution
        for contribution in artifacts.regions
        if authority_groups.intersection(contribution.source_groups)
    ]
    if not hardware_regions:
        raise SafetyArtifactError("safety map has no reconciled hardware regions")
    for contribution in hardware_regions:
        provenance = contribution.region.provenance
        if not provenance or any(
            item.authority is not SourceAuthority.RECONCILED for item in provenance
        ):
            raise SafetyArtifactError(
                f"hardware region '{contribution.region.name}' is not reconciled"
            )

    def region_signature(region: SafetyRegion) -> tuple[object, ...]:
        return (
            region.name,
            region.kind.value,
            region.address_range.start,
            region.address_range.end,
            region.executable,
            tuple(
                (item.authority.value, item.source_id, item.detail)
                for item in region.provenance
            ),
        )

    observed_regions = sorted(region_signature(item.region) for item in hardware_regions)
    expected_regions = sorted(
        region_signature(item.to_safety_region()) for item in bundle.reconciliation.regions
    )
    if observed_regions != expected_regions:
        raise SafetyArtifactError(
            "persisted hardware regions do not exactly match recomputed reconciliation"
        )


def _ambiguous_overlap(
    first: RegionContribution, second: RegionContribution
) -> bool:
    if not first.region.address_range.overlaps(second.region.address_range):
        return False
    left = first.region.kind
    right = second.region.kind
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
    return tuple(
        sorted(conflicts, key=lambda item: (str(item["code"]), str(item["regions"])))
    )


def build_documents(
    request: SafetySetupRequest,
    fingerprints: FingerprintSet,
    *,
    status: str,
    prompt: str,
    conflicts: tuple[Mapping[str, object], ...] = (),
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    created_at = _timestamp()
    region_documents = [item.to_document() for item in sorted(
        request.regions,
        key=lambda item: (
            item.region.address_range.start,
            item.region.address_range.end,
            item.region.kind.value,
            item.region.name,
        ),
    )]
    fingerprint_document = fingerprints.to_document()
    memory_map: dict[str, object] = {
        "schema_version": SAFETY_MAP_SCHEMA_VERSION,
        "board_id": request.board_id,
        "created_at": created_at,
        "fingerprints": fingerprint_document,
        "regions": region_documents,
    }
    canonical_sources = request.inputs.canonical_documents()
    source_manifest: dict[str, object] = {
        "schema_version": SAFETY_MAP_SCHEMA_VERSION,
        "board_id": request.board_id,
        "created_at": created_at,
        "fingerprints": fingerprint_document,
        "sources": {
            source.value: {
                "fingerprint": fingerprints.as_mapping()[source],
                "evidence": canonical_sources[source.value],
            }
            for source in FingerprintSource
        },
    }
    report: dict[str, object] = {
        "schema_version": SAFETY_MAP_SCHEMA_VERSION,
        "report_type": "safety",
        "board_id": request.board_id,
        "continuation_id": request.continuation_id,
        "created_at": created_at,
        "status": status,
        "agent_prompt": prompt,
        "aggregate_fingerprint": fingerprints.aggregate,
        "conflicts": [dict(item) for item in conflicts],
    }
    return memory_map, source_manifest, report


class SafetyMapBuilder:
    def __init__(self, store: FirmStore) -> None:
        self.repository = SafetyArtifactRepository(store)

    def build(self, request: SafetySetupRequest) -> SafetySetupResult:
        _require_board_id(request.board_id)
        if not request.continuation_id.strip():
            raise SafetyArtifactError("continuation_id must be non-empty")
        fingerprints = FingerprintSet.build(request.inputs)
        if request.issues:
            issue = request.issues[0]
            prompt = f"{issue.message.strip()} {NO_INTERNALS}"
            report = {
                "schema_version": SAFETY_MAP_SCHEMA_VERSION,
                "report_type": "safety",
                "board_id": request.board_id,
                "continuation_id": request.continuation_id,
                "created_at": _timestamp(),
                "status": issue.status,
                "code": issue.code,
                "agent_prompt": prompt,
                "details": dict(issue.details),
            }
            report_path = self.repository.write_report(request.board_id, report)
            return SafetySetupResult(
                issue.status,
                request.board_id,
                request.continuation_id,
                prompt,
                issue.choices,
                {"code": issue.code, "report": str(report_path)},
                report_path,
                None,
            )
        if not request.regions:
            issue = SafetyIssue(
                "safety_setup_incomplete",
                "safety/no-regions",
                "No authoritative safety regions are available; write-capable actions remain closed.",
            )
            return self.build(
                SafetySetupRequest(
                    request.board_id,
                    request.continuation_id,
                    request.inputs,
                    (),
                    (issue,),
                )
            )
        conflicts = region_conflicts(request.regions)
        if conflicts:
            issue = SafetyIssue(
                "safety_setup_conflict",
                "safety/region-conflict",
                "Authoritative safety regions conflict; resolve the sources and rerun safety setup.",
                details={"conflicts": conflicts},
            )
            return self.build(
                SafetySetupRequest(
                    request.board_id,
                    request.continuation_id,
                    request.inputs,
                    request.regions,
                    (issue,),
                )
            )
        canonical_regions = tuple(
            sorted(
                request.regions,
                key=lambda item: (
                    item.region.address_range.start,
                    item.region.address_range.end,
                    item.region.kind.value,
                    item.region.name,
                ),
            )
        )
        prompt = f"Safety setup completed. Run board_validate before any gate may open. {NO_INTERNALS}"
        try:
            current = self.repository.load_current(request.board_id)
        except (SafetyArtifactError, ValueError):
            current = None
        if (
            current is not None
            and current.fingerprints == fingerprints
            and current.regions == canonical_regions
        ):
            report = {
                "schema_version": SAFETY_MAP_SCHEMA_VERSION,
                "report_type": "safety",
                "board_id": request.board_id,
                "continuation_id": request.continuation_id,
                "created_at": _timestamp(),
                "status": "safety_setup_completed",
                "agent_prompt": prompt,
                "aggregate_fingerprint": fingerprints.aggregate,
                "conflicts": [],
                "unchanged_rebuild": True,
            }
            report_path = self.repository.write_report(request.board_id, report)
            paths = self.repository.paths(request.board_id)
            return SafetySetupResult(
                "safety_setup_completed",
                request.board_id,
                request.continuation_id,
                prompt,
                (),
                {
                    "memory_map": str(paths["memory_map"]),
                    "source_manifest": str(paths["source_manifest"]),
                    "report": str(report_path),
                    "region_count": len(request.regions),
                    "unchanged_rebuild": True,
                },
                report_path,
                fingerprints.aggregate,
            )
        memory, manifest, report = build_documents(
            request, fingerprints, status="safety_setup_completed", prompt=prompt
        )
        paths = self.repository.commit(
            request.board_id,
            memory_map=memory,
            source_manifest=manifest,
            safety_report=report,
        )
        return SafetySetupResult(
            "safety_setup_completed",
            request.board_id,
            request.continuation_id,
            prompt,
            (),
            {
                "memory_map": str(paths["memory_map"]),
                "source_manifest": str(paths["source_manifest"]),
                "report": str(paths["safety_report"]),
                "region_count": len(request.regions),
            },
            paths["safety_report"],
            fingerprints.aggregate,
        )
