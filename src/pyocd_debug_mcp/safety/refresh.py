"""Deterministic full safety-map rebuilds."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.safety.fingerprints import FingerprintInputs, FingerprintSource
from pyocd_debug_mcp.safety.map_build import (
    NO_INTERNALS,
    RegionContribution,
    SafetyArtifactError,
    SafetyArtifactRepository,
    SafetyMapBuilder,
    SafetySetupRequest,
    _timestamp,
)


@dataclass(frozen=True, slots=True)
class SafetyRefreshRequest:
    board_id: str
    continuation_id: str
    inputs: FingerprintInputs
    rebuilt_groups: tuple[FingerprintSource, ...] = ()
    replacement_regions: tuple[RegionContribution, ...] = ()


@dataclass(frozen=True, slots=True)
class SafetyRefreshResult:
    status: str
    board_id: str
    continuation_id: str
    agent_prompt: str
    observed: Mapping[str, object]
    remedy: tuple[str, ...]
    report_path: Path | None
    aggregate_fingerprint: str | None
    validation_required: bool

    @property
    def map_digest(self) -> str | None:
        return self.aggregate_fingerprint

    @property
    def changed_sources(self) -> tuple[FingerprintSource, ...]:
        raw = self.observed.get("changed_sources", ())
        if not isinstance(raw, list):
            return ()
        return tuple(FingerprintSource(item) for item in raw if isinstance(item, str))

    @property
    def rebuilt_groups(self) -> tuple[FingerprintSource, ...]:
        """Compatibility name for scripts that display the full-rebuild causes."""

        return self.changed_sources

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "agent_prompt": self.agent_prompt,
            "choices": [],
            "observed": {
                **dict(self.observed),
                "validation_required": self.validation_required,
                "next_action": "board_validate" if self.validation_required else "continue",
            },
            "constraints": [
                "Refresh rederives the complete map from server-owned reviewed sources.",
                "Refresh never creates a live identity proof or accepts caller-defined ranges.",
                NO_INTERNALS,
            ],
            "rejected_candidates": [],
            "accepted_response": (
                {
                    "tool": "board_validate",
                    "arguments": {"board_id": self.board_id},
                }
                if self.validation_required and self.status == "safety_refresh_completed"
                else None
            ),
            "validation_plan": list(self.remedy),
        }


class SafetyRefresher:
    """Always build one complete candidate; change groups are explanatory only."""

    def __init__(
        self,
        store: FirmStore,
        *,
        on_commit: Callable[[str, str, bool], bool | None] | None = None,
        authority_verifier: Callable[[object], None] | None = None,
    ) -> None:
        self.repository = SafetyArtifactRepository(store)
        self.builder = SafetyMapBuilder(store)
        self.on_commit = on_commit or (
            lambda _board_id, _digest, _identity_unchanged: False
        )
        self.authority_verifier = authority_verifier

    def _blocked(
        self,
        request: SafetyRefreshRequest,
        *,
        message: str,
        classification: str,
        remedy: tuple[str, ...],
        details: Mapping[str, object] | None = None,
    ) -> SafetyRefreshResult:
        return SafetyRefreshResult(
            "safety_refresh_blocked",
            request.board_id,
            request.continuation_id,
            f"{message.strip()} {NO_INTERNALS}",
            {
                "board_id": request.board_id,
                "drift_classification": classification,
                "created_at": _timestamp(),
                **dict(details or {}),
            },
            remedy,
            None,
            None,
            False,
        )

    def blocked(
        self,
        request: SafetyRefreshRequest,
        *,
        message: str,
        classification: str,
        changed: tuple[FingerprintSource, ...] = (),
        remedy: tuple[str, ...] = ("board_safety_refresh",),
        details: Mapping[str, object] | None = None,
    ) -> SafetyRefreshResult:
        return self._blocked(
            request,
            message=message,
            classification=classification,
            remedy=remedy,
            details={
                "changed_sources": [item.value for item in changed],
                **dict(details or {}),
            },
        )

    def refresh(self, request: SafetyRefreshRequest) -> SafetyRefreshResult:
        try:
            old = self.repository.load_current(request.board_id)
        except SafetyArtifactError:
            old = None
        # Replacement regions represent the fully rederived candidate supplied by the composition
        # root. They are never merged with persisted regions.
        setup_request = SafetySetupRequest(
            request.board_id,
            request.continuation_id,
            request.inputs,
            request.replacement_regions,
        )
        try:
            candidate = self.builder.candidate(setup_request)
            if self.authority_verifier is not None:
                self.authority_verifier(candidate)
            self.repository.commit(request.board_id, memory_map=candidate.memory_map)
        except (SafetyArtifactError, OSError, ValueError) as exc:
            return self._blocked(
                request,
                message=(
                    f"The complete safety map could not be rebuilt: {exc}. Resolve the named "
                    "reviewed evidence problem and run board_safety_refresh again."
                ),
                classification="full_rebuild_blocked",
                remedy=("resolve_reviewed_safety_sources", "board_safety_refresh"),
                details={"reason": str(exc)},
            )
        changed = old is None or old.map_digest != candidate.map_digest
        identity_unchanged = old is not None and dict(old.identity) == dict(candidate.identity)
        # A refresh may update the map side of a same-connection stamp only when the reviewed
        # live identity anchor is unchanged. Identity repair is an explicit validation trigger;
        # never carry an older silicon proof across it.
        callback_preserved = bool(
            self.on_commit(request.board_id, candidate.map_digest, identity_unchanged)
        )
        validation_preserved = identity_unchanged and callback_preserved
        prompt = (
            "Safety refresh rebuilt the complete stable memory map. "
            + (
                "The same validated live board remains connected, so continue without calling "
                "board_validate. "
                if validation_preserved
                else "No current live identity proof exists; call board_validate once before "
                "guarded hardware work. "
            )
            + NO_INTERNALS
        )
        return SafetyRefreshResult(
            "safety_refresh_completed",
            request.board_id,
            request.continuation_id,
            prompt,
            {
                "board_id": request.board_id,
                "drift_classification": "complete_rebuild",
                "changed": changed,
                "identity_unchanged": identity_unchanged,
                "changed_sources": [item.value for item in request.rebuilt_groups],
                "memory_map": str(self.repository.paths(request.board_id)["memory_map"]),
                "created_at": _timestamp(),
            },
            (() if validation_preserved else ("board_validate",)),
            None,
            candidate.map_digest,
            not validation_preserved,
        )


def _drift_classification(changed: set[FingerprintSource]) -> str:
    """Compatibility helper: v2 always rebuilds, this only labels the cause."""

    if not changed:
        return "none"
    return "complete_rebuild"
