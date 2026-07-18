"""Fingerprint drift routing and fail-closed scoped safety-map refresh."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.safety.fingerprints import (
    FingerprintInputs,
    FingerprintSet,
    FingerprintSource,
)
from pyocd_debug_mcp.safety.map_build import (
    NO_INTERNALS,
    SAFETY_MAP_SCHEMA_VERSION,
    RegionContribution,
    SafetyArtifactError,
    SafetyArtifactRepository,
    SafetySetupRequest,
    _require_board_id,
    _timestamp,
    build_documents,
    region_conflicts,
    require_reconciled_authority,
)

SafetyRefreshStatus = Literal[
    "safety_refresh_completed",
    "refresh_scope_unclear",
    "safety_conflict",
    "safety_refresh_blocked",
]


@dataclass(frozen=True, slots=True)
class SafetyRefreshRequest:
    board_id: str
    continuation_id: str
    inputs: FingerprintInputs
    rebuilt_groups: tuple[FingerprintSource, ...] = ()
    replacement_regions: tuple[RegionContribution, ...] = ()


@dataclass(frozen=True, slots=True)
class SafetyRefreshResult:
    status: SafetyRefreshStatus
    board_id: str
    continuation_id: str
    agent_prompt: str
    changed_sources: tuple[FingerprintSource, ...]
    rebuilt_groups: tuple[FingerprintSource, ...]
    remedy: tuple[str, ...]
    report_path: Path
    aggregate_fingerprint: str | None
    drift_classification: str

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "agent_prompt": self.agent_prompt,
            "choices": [],
            "observed": {
                "board_id": self.board_id,
                "changed_sources": [item.value for item in self.changed_sources],
                "rebuilt_groups": [item.value for item in self.rebuilt_groups],
                "aggregate_fingerprint": self.aggregate_fingerprint,
                "drift_classification": self.drift_classification,
                "report": str(self.report_path),
            },
            "constraints": [
                "Refresh cannot replace full safety setup for anchor or structural changes.",
                (
                    "Refresh may restamp only an already hardware-validated active connection; "
                    "it cannot restore validation after disconnect or restart."
                ),
                NO_INTERNALS,
            ],
            "rejected_candidates": [],
            "accepted_response": None,
            "validation_plan": list(self.remedy),
        }


class SafetyRefresher:
    def __init__(
        self,
        store: FirmStore,
        *,
        on_commit: Callable[[str, str], None] | None = None,
        authority_verifier: Callable[[object], None] | None = None,
    ) -> None:
        self.repository = SafetyArtifactRepository(store)
        self.on_commit = on_commit or (lambda _board_id, _aggregate: None)
        self.authority_verifier = authority_verifier or require_reconciled_authority

    def _result(
        self,
        request: SafetyRefreshRequest,
        *,
        status: SafetyRefreshStatus,
        message: str,
        changed: tuple[FingerprintSource, ...],
        rebuilt: tuple[FingerprintSource, ...] = (),
        remedy: tuple[str, ...],
        aggregate: str | None = None,
        classification: str,
        details: Mapping[str, object] | None = None,
    ) -> SafetyRefreshResult:
        prompt = f"{message.strip()} {NO_INTERNALS}"
        report = {
            "schema_version": SAFETY_MAP_SCHEMA_VERSION,
            "report_type": "safety_refresh",
            "board_id": request.board_id,
            "continuation_id": request.continuation_id,
            "created_at": _timestamp(),
            "status": status,
            "agent_prompt": prompt,
            "changed_sources": [item.value for item in changed],
            "rebuilt_groups": [item.value for item in rebuilt],
            "remedy": list(remedy),
            "aggregate_fingerprint": aggregate,
            "details": dict(details or {}),
        }
        report_path = self.repository.write_report(request.board_id, report)
        return SafetyRefreshResult(
            status,
            request.board_id,
            request.continuation_id,
            prompt,
            changed,
            rebuilt,
            remedy,
            report_path,
            aggregate,
            classification,
        )

    def refresh(self, request: SafetyRefreshRequest) -> SafetyRefreshResult:
        _require_board_id(request.board_id)
        if not request.continuation_id.strip():
            raise SafetyArtifactError("continuation_id must be non-empty")
        try:
            current = self.repository.load_current(request.board_id)
            self.authority_verifier(current)
        except (SafetyArtifactError, ValueError) as exc:
            return self._result(
                request,
                status="refresh_scope_unclear",
                message="Current safety sources are missing, stale, or inconsistent; run full safety setup.",
                changed=(),
                remedy=("board_safety_setup",),
                classification="unclear_scope",
                details={"reason": str(exc)},
            )
        candidate = FingerprintSet.build(request.inputs)
        changed = current.fingerprints.changed_sources(candidate)
        if not changed:
            self.on_commit(request.board_id, current.fingerprints.aggregate)
            return self._result(
                request,
                status="safety_refresh_completed",
                message="Safety inputs are already fresh; board validation still owns gate opening.",
                changed=(),
                remedy=("board_validate_if_gate_is_closed",),
                aggregate=current.fingerprints.aggregate,
                classification="fresh",
            )

        changed_set = set(changed)
        if FingerprintSource.PART_TARGET in changed_set:
            return self._result(
                request,
                status="safety_refresh_blocked",
                message="The MCU part number or target changed. Refresh is insufficient; run full safety setup and validation.",
                changed=changed,
                remedy=("board_safety_setup", "board_validate"),
                classification="anchor_change",
            )
        structural = changed_set.intersection(
            {FingerprintSource.GEOMETRY, FingerprintSource.SCHEMA}
        )
        if structural:
            return self._result(
                request,
                status="safety_refresh_blocked",
                message="Flash geometry or the map schema changed and requires full safety setup.",
                changed=changed,
                remedy=("board_safety_setup", "board_validate"),
                classification=_drift_classification(changed_set),
            )
        if FingerprintSource.PROFILE in changed_set:
            return self._result(
                request,
                status="refresh_scope_unclear",
                message="Profile drift cannot be safely scoped from its fingerprint; run full safety setup.",
                changed=changed,
                remedy=("board_safety_setup",),
                classification="unclear_scope",
            )

        rebuild = set(changed_set)
        if rebuild.intersection({FingerprintSource.PACK, FingerprintSource.EVIDENCE}):
            rebuild.update({FingerprintSource.PACK, FingerprintSource.EVIDENCE})
        expected_rebuilt = tuple(sorted(rebuild, key=lambda item: item.value))
        supplied_rebuilt = tuple(sorted(set(request.rebuilt_groups), key=lambda item: item.value))
        if supplied_rebuilt != expected_rebuilt:
            return self._result(
                request,
                status="refresh_scope_unclear",
                message="The supplied rebuilt source scope is incomplete or broader than the detected drift.",
                changed=changed,
                rebuilt=supplied_rebuilt,
                remedy=("board_safety_setup",),
                classification=_drift_classification(changed_set),
                details={
                    "required_rebuilt_groups": [item.value for item in expected_rebuilt],
                },
            )
        if any(
            not set(item.source_groups).intersection(rebuild)
            for item in request.replacement_regions
        ):
            return self._result(
                request,
                status="refresh_scope_unclear",
                message="A replacement region is unrelated to the detected source drift.",
                changed=changed,
                rebuilt=supplied_rebuilt,
                remedy=("board_safety_setup",),
                classification="unclear_scope",
            )
        hardware_groups = {FingerprintSource.PACK, FingerprintSource.EVIDENCE}
        if rebuild.intersection(hardware_groups) and not all(
            any(group in item.source_groups for item in request.replacement_regions)
            for group in hardware_groups
        ):
            return self._result(
                request,
                status="refresh_scope_unclear",
                message="The hardware-evidence rebuild is incomplete; run full safety setup.",
                changed=changed,
                rebuilt=supplied_rebuilt,
                remedy=("board_safety_setup",),
                classification=_drift_classification(changed_set),
            )

        retained = tuple(
            item for item in current.regions if not set(item.source_groups).intersection(rebuild)
        )
        refreshed_regions = tuple(
            sorted(
                (*retained, *request.replacement_regions),
                key=lambda item: (
                    item.region.address_range.start,
                    item.region.address_range.end,
                    item.region.kind.value,
                    item.region.name,
                ),
            )
        )
        if not refreshed_regions:
            return self._result(
                request,
                status="refresh_scope_unclear",
                message="The scoped rebuild produced no authoritative regions; run full safety setup.",
                changed=changed,
                rebuilt=supplied_rebuilt,
                remedy=("board_safety_setup",),
                classification="unclear_scope",
            )
        conflicts = region_conflicts(refreshed_regions)
        if conflicts:
            return self._result(
                request,
                status="safety_conflict",
                message="Refreshed regions conflict; the previous map remains current and actions stay closed.",
                changed=changed,
                rebuilt=supplied_rebuilt,
                remedy=("resolve_safety_sources", "board_safety_refresh"),
                classification="safety_conflict",
                details={"conflicts": conflicts},
            )

        setup_request = SafetySetupRequest(
            request.board_id,
            request.continuation_id,
            request.inputs,
            refreshed_regions,
        )
        prompt = (
            f"Safety refresh completed. Board validation still owns gate opening. {NO_INTERNALS}"
        )
        memory, manifest, report = build_documents(
            setup_request,
            candidate,
            status="safety_refresh_completed",
            prompt=prompt,
        )
        report["report_type"] = "safety_refresh"
        report["changed_sources"] = [item.value for item in changed]
        report["rebuilt_groups"] = [item.value for item in supplied_rebuilt]
        self.repository.commit(
            request.board_id,
            memory_map=memory,
            source_manifest=manifest,
            safety_report=report,
        )
        self.on_commit(request.board_id, candidate.aggregate)
        return SafetyRefreshResult(
            "safety_refresh_completed",
            request.board_id,
            request.continuation_id,
            prompt,
            changed,
            supplied_rebuilt,
            ("board_validate_if_gate_is_closed",),
            self.repository.paths(request.board_id)["safety_report"],
            candidate.aggregate,
            _drift_classification(changed_set),
        )

    def blocked(
        self,
        request: SafetyRefreshRequest,
        *,
        message: str,
        classification: str,
        changed: tuple[FingerprintSource, ...],
        remedy: tuple[str, ...],
        details: Mapping[str, object] | None = None,
    ) -> SafetyRefreshResult:
        """Write the standard immutable report for a pre-promotion refresh blocker."""

        return self._result(
            request,
            status="safety_refresh_blocked",
            message=message,
            changed=changed,
            remedy=remedy,
            classification=classification,
            details=details,
        )


def _drift_classification(changed: set[FingerprintSource]) -> str:
    if FingerprintSource.PART_TARGET in changed:
        return "anchor_change"
    geometry = FingerprintSource.GEOMETRY in changed
    schema = FingerprintSource.SCHEMA in changed
    if geometry and schema:
        return "geometry_and_schema_change"
    if geometry:
        return "geometry_change"
    if schema:
        return "schema_change"
    if FingerprintSource.PROFILE in changed:
        return "unclear_scope"
    pack = FingerprintSource.PACK in changed
    evidence = FingerprintSource.EVIDENCE in changed
    if pack and evidence:
        return "pack_and_official_evidence_change"
    if pack:
        return "pack_change"
    if evidence:
        return "official_evidence_change"
    application = FingerprintSource.APPLICATION_ARTIFACTS in changed
    bootloader = FingerprintSource.BOOTLOADER_ARTIFACTS in changed
    if application and bootloader:
        return "application_and_bootloader_change"
    if application:
        return "application_change"
    if bootloader:
        return "bootloader_change"
    return "unclear_scope"
