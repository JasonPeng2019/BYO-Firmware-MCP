"""Strict, run-scoped research handoff for board setup facts."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pyocd_debug_mcp.setup_flow.preflight import NO_INTERNALS_RELAY_INSTRUCTION

MAX_CANDIDATES_PER_FACT = 3
BLOCKED_CONDITIONS = frozenset(
    {"locked_target", "missing_probe", "missing_driver", "probe_disconnected"}
)


class ResearchError(ValueError):
    """A research reply violates the requested schema or immutable facts."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    fact_id: str
    continuation_token: str
    board_id: str
    mcu_part_number: str
    unresolved_fact: str
    requested_fields: tuple[str, ...]
    authoritative_facts: Mapping[str, Any]
    observed_output: tuple[Mapping[str, Any], ...] = ()
    acceptable_sources: tuple[str, ...] = ()
    validation_plan: tuple[str, ...] = ()
    immutable_fields: tuple[str, ...] = ("mcu_part_number",)


@dataclass(frozen=True, slots=True)
class CandidateFailure:
    fingerprint: str
    candidate: Mapping[str, Any]
    reason: str
    observed: Mapping[str, Any]

    def to_document(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "candidate": copy.deepcopy(dict(self.candidate)),
            "reason": self.reason,
            "observed": copy.deepcopy(dict(self.observed)),
        }


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    accepted: bool
    reason: str = ""
    observed: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResearchResult:
    status: Literal["setup_research_required", "setup_unresolved", "accepted"]
    fingerprint: str
    candidate: Mapping[str, Any] | None = None
    failure: CandidateFailure | None = None
    duplicate: bool = False


def candidate_fingerprint(candidate: Mapping[str, Any]) -> str:
    """Return a deterministic, type-preserving candidate identity."""

    encoded = json.dumps(
        candidate,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def classify_research_condition(condition: str) -> Literal["setup_blocked", "research"]:
    """Separate conditions that research cannot solve from unknown facts."""

    return "setup_blocked" if condition in BLOCKED_CONDITIONS else "research"


class ResearchTracker:
    """Track candidate attempts in memory for one server run."""

    def __init__(self, *, max_candidates: int = MAX_CANDIDATES_PER_FACT) -> None:
        if max_candidates < 1:
            raise ValueError("max_candidates must be positive")
        self._max_candidates = max_candidates
        self._failures: dict[tuple[str, str], list[CandidateFailure]] = {}

    @staticmethod
    def _key(request: ResearchRequest) -> tuple[str, str]:
        return request.board_id, request.fact_id

    def failures(self, request: ResearchRequest) -> tuple[CandidateFailure, ...]:
        return tuple(self._failures.get(self._key(request), ()))

    def clear(self, board_id: str) -> None:
        """Discard rejected candidates when a board's setup allowance closes."""

        normalized = board_id.strip()
        for key in tuple(self._failures):
            if key[0] == normalized:
                self._failures.pop(key, None)

    def prompt(self, request: ResearchRequest) -> dict[str, Any]:
        """Build a self-contained relay payload with no authority-bearing state."""

        rejected = [failure.to_document() for failure in self.failures(request)]
        return {
            "status": "setup_research_required",
            "continuation_token": request.continuation_token,
            "unresolved_fact": request.unresolved_fact,
            "authoritative_facts": copy.deepcopy(dict(request.authoritative_facts)),
            "observed_output": [copy.deepcopy(dict(item)) for item in request.observed_output],
            "rejected_candidates": rejected,
            "acceptable_sources": list(request.acceptable_sources),
            "exact_response_fields": list(request.requested_fields),
            "fields_that_must_not_change": list(request.immutable_fields),
            "validation_plan": list(request.validation_plan),
            "user_approval_default": "no",
            "agent_prompt": (
                f"Research only this unresolved fact: {request.unresolved_fact}. "
                f"Reply with exactly these fields: {', '.join(request.requested_fields)}. "
                "Do not change authoritative facts, grant permission, open a gate, or persist "
                f"the candidate. {NO_INTERNALS_RELAY_INSTRUCTION}"
            ),
        }

    def validate_reply(
        self,
        request: ResearchRequest,
        reply: Mapping[str, Any],
        validator: Callable[[Mapping[str, Any]], ValidationOutcome],
    ) -> ResearchResult:
        """Validate exactly one distinct candidate and account for its retry budget."""

        supplied = set(reply)
        requested = set(request.requested_fields)
        immutable = supplied.intersection(request.immutable_fields)
        if immutable:
            raise ResearchError(
                "research/immutable-field",
                f"Research reply attempted to supply immutable fields: {sorted(immutable)}",
            )
        if supplied != requested:
            unexpected = sorted(supplied - requested)
            missing = sorted(requested - supplied)
            raise ResearchError(
                "research/field-set-mismatch",
                f"Research reply fields must match exactly; missing={missing}, unexpected={unexpected}",
            )

        candidate = copy.deepcopy(dict(reply))
        fingerprint = candidate_fingerprint(candidate)
        failures = self._failures.setdefault(self._key(request), [])
        previous = next(
            (failure for failure in failures if failure.fingerprint == fingerprint), None
        )
        if previous is not None:
            return ResearchResult(
                status="setup_research_required",
                fingerprint=fingerprint,
                failure=previous,
                duplicate=True,
            )
        if len(failures) >= self._max_candidates:
            return ResearchResult(status="setup_unresolved", fingerprint=fingerprint)

        outcome = validator(candidate)
        if outcome.accepted:
            return ResearchResult(status="accepted", fingerprint=fingerprint, candidate=candidate)
        failure = CandidateFailure(
            fingerprint=fingerprint,
            candidate=candidate,
            reason=outcome.reason or "candidate validation failed",
            observed=copy.deepcopy(dict(outcome.observed)),
        )
        failures.append(failure)
        status: Literal["setup_research_required", "setup_unresolved"] = (
            "setup_unresolved"
            if len(failures) >= self._max_candidates
            else "setup_research_required"
        )
        return ResearchResult(status=status, fingerprint=fingerprint, failure=failure)


def make_research_request(
    *,
    fact_id: str,
    continuation_token: str,
    board_id: str,
    mcu_part_number: str,
    unresolved_fact: str,
    requested_fields: Sequence[str],
    authoritative_facts: Mapping[str, Any],
    observed_output: Sequence[Mapping[str, Any]] = (),
    acceptable_sources: Sequence[str] = (),
    validation_plan: Sequence[str] = (),
) -> ResearchRequest:
    if not mcu_part_number or not mcu_part_number.strip():
        raise ResearchError("research/missing-part", "An exact MCU part number is required")
    if not requested_fields or len(set(requested_fields)) != len(requested_fields):
        raise ResearchError(
            "research/invalid-fields", "Requested response fields must be nonempty and unique"
        )
    facts = copy.deepcopy(dict(authoritative_facts))
    facts["board_id"] = board_id
    facts["mcu_part_number"] = mcu_part_number
    return ResearchRequest(
        fact_id=fact_id,
        continuation_token=continuation_token,
        board_id=board_id,
        mcu_part_number=mcu_part_number,
        unresolved_fact=unresolved_fact,
        requested_fields=tuple(requested_fields),
        authoritative_facts=facts,
        observed_output=tuple(copy.deepcopy(list(observed_output))),
        acceptable_sources=tuple(acceptable_sources),
        validation_plan=tuple(validation_plan),
    )
