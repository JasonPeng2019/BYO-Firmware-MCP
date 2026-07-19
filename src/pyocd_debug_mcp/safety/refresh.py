"""Deterministic single-map refresh for Safety Layer v2."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.safety.map_build import (
    GenericSafetyMapDocument,
    NO_INTERNALS,
    SafetyMapDocument,
    SafetyMapError,
    SafetyMapRepository,
    _require_board_id,
)

SafetyRefreshStatus = Literal["safety_refresh_completed", "safety_refresh_blocked"]
MapDocument = SafetyMapDocument | GenericSafetyMapDocument
MapDeriver = Callable[[str], MapDocument]
LiveIdentityProvider = Callable[[str], bool]
MapCommitHook = Callable[[str, str, bool], None]


@dataclass(frozen=True, slots=True)
class SafetyRefreshRequest:
    board_id: str
    continuation_id: str


@dataclass(frozen=True, slots=True)
class SafetyRefreshResult:
    status: SafetyRefreshStatus
    board_id: str
    continuation_id: str
    agent_prompt: str
    changed_groups: tuple[str, ...]
    map_digest: str | None
    validation_required: bool
    remedy: tuple[str, ...]
    drift_classification: str
    reason: str | None = None

    def to_payload(self) -> dict[str, object]:
        next_action = "board_validate" if self.validation_required else "continue_current_workflow"
        return {
            "status": self.status,
            "agent_prompt": self.agent_prompt,
            "choices": [],
            "observed": {
                "board_id": self.board_id,
                "changed_groups": list(self.changed_groups),
                "map_digest": self.map_digest,
                "drift_classification": self.drift_classification,
                "validation_required": self.validation_required,
                "next_action": next_action,
                **({"reason": self.reason} if self.reason else {}),
            },
            "constraints": [
                "Refresh rebuilds only stable server-owned safety authority.",
                "Refresh never creates live identity authority or accepts caller ranges.",
                "Ordinary firmware builds and artifact changes do not require refresh.",
                NO_INTERNALS,
            ],
            "rejected_candidates": [],
            "accepted_response": None,
            "validation_plan": list(self.remedy),
        }


class SafetyRefresher:
    """Re-derive and atomically replace one complete candidate on every call."""

    def __init__(
        self,
        store: FirmStore,
        *,
        derive: MapDeriver,
        has_live_identity: LiveIdentityProvider | None = None,
        on_commit: MapCommitHook | None = None,
    ) -> None:
        self.repository = SafetyMapRepository(store)
        self.derive = derive
        self.has_live_identity = has_live_identity or (lambda _board_id: False)
        self.on_commit = on_commit or (lambda _board_id, _digest, _identity_changed: None)

    def refresh(self, request: SafetyRefreshRequest) -> SafetyRefreshResult:
        board_id = _require_board_id(request.board_id)
        if not request.continuation_id.strip():
            raise SafetyMapError("continuation_id must be non-empty")
        previous: MapDocument | None = None
        prior_invalid = False
        try:
            previous = self.repository.load_current(board_id)
        except (SafetyMapError, ValueError):
            prior_invalid = True
        try:
            candidate = self.derive(board_id)
        except (SafetyMapError, OSError, ValueError) as exc:
            return self.blocked(request, str(exc))
        if candidate.board_id != board_id:
            return self.blocked(request, "derived memory map belongs to a different board")

        changed = _changed_groups(previous, candidate, prior_invalid=prior_invalid)
        identity_changed = previous is not None and previous.identity != candidate.identity
        self.repository.commit(board_id, candidate)
        self.on_commit(board_id, candidate.canonical_digest, identity_changed)
        live = self.has_live_identity(board_id)
        validation_required = not live
        message = (
            "Safety refresh completed. The existing live identity proof remains valid; continue "
            "the current workflow without revalidation."
            if live
            else "Safety refresh completed. No current live identity proof exists; run board_validate."
        )
        return SafetyRefreshResult(
            "safety_refresh_completed",
            board_id,
            request.continuation_id,
            f"{message} {NO_INTERNALS}",
            changed,
            candidate.canonical_digest,
            validation_required,
            (("board_validate",) if validation_required else ()),
            _classification(changed),
        )

    def blocked(self, request: SafetyRefreshRequest, reason: str) -> SafetyRefreshResult:
        return SafetyRefreshResult(
            "safety_refresh_blocked",
            request.board_id,
            request.continuation_id,
            (
                "The server could not reproduce the complete stable safety map from reviewed "
                f"sources: {reason}. Resolve that reviewed evidence and run board_safety_refresh "
                f"again. {NO_INTERNALS}"
            ),
            (),
            None,
            not self.has_live_identity(request.board_id),
            ("resolve_reviewed_safety_sources", "board_safety_refresh"),
            "reviewed_evidence_unavailable",
            reason,
        )


def _changed_groups(
    previous: MapDocument | None,
    candidate: MapDocument,
    *,
    prior_invalid: bool,
) -> tuple[str, ...]:
    if previous is None:
        return ("missing_or_invalid_map",) if prior_invalid else ("initial_map",)
    changed: list[str] = []
    if previous.identity != candidate.identity:
        changed.append("identity")
    previous_sources = previous.source_digests.to_document()
    candidate_sources = candidate.source_digests.to_document()
    for name in sorted(set(previous_sources) | set(candidate_sources)):
        if previous_sources.get(name) != candidate_sources.get(name):
            changed.append(name)
    if previous.geometry != candidate.geometry:
        changed.append("geometry")
    if previous.partitions != candidate.partitions:
        changed.append("partitions")
    if previous.regions != candidate.regions:
        changed.append("regions")
    return tuple(changed)


def _classification(changed: tuple[str, ...]) -> str:
    if not changed:
        return "fresh"
    if changed in {("initial_map",), ("missing_or_invalid_map",)}:
        return changed[0]
    if "identity" in changed:
        return "identity_change"
    if "geometry" in changed or "partitions" in changed:
        return "stable_authority_change"
    return "reviewed_source_change"
