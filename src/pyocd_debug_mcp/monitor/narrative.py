"""The two model-authored narrative forms, and their validation.

Both exist only in a personal build. Model output is untrusted -- it may be
malformed, oversized, or partly wrong -- so these are fixed schemas rather than
free prose, which is what makes size-bounding and shape-checking tractable.

Note which bar applies. This is the opt-in codebase-describing layer, so the
narrative *may* name real symbols, files, and describe the code: that is its
purpose, not a leak. It may not embed verbatim payloads, which are payloads rather
than summary and add nothing as prose.

The server supplies the trail, guard state, board scope, grouping identity, and
environment. None of that is accepted from the model.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pyocd_debug_mcp.monitor.classify import MODEL_SIGNALS, SUBCASE_REQUIRED, Signal
from pyocd_debug_mcp.monitor.redaction import (
    NarrativeContentError,
    check_narrative,
    check_no_self_rating,
)

RECENT_ACTIONS = 5


def _checked(value: str, field: str) -> str:
    try:
        check_narrative(value, field=field)
    except NarrativeContentError as exc:
        raise ValueError(str(exc)) from exc
    return value


class RecentAction(BaseModel):
    """One of the last few actions before the failure, with no compression."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: str = Field(min_length=1, max_length=1000)
    result: str = Field(min_length=1, max_length=1000)
    code_context: str = Field(min_length=1, max_length=1000)

    @field_validator("action", "result", "code_context")
    @classmethod
    def _bar(cls, value: str) -> str:
        return _checked(value, "recent_actions")


class FailurePoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_taken: str = Field(min_length=1, max_length=1000)
    observed_result: str = Field(min_length=1, max_length=1000)
    # The agent's account of which step tripped it. The authoritative mechanical
    # anchor is attached by the server, so this is not the source of truth.
    named_step: str = Field(min_length=1, max_length=200)

    @field_validator("action_taken", "observed_result", "named_step")
    @classmethod
    def _bar(cls, value: str) -> str:
        return _checked(value, "failure_point")


class IssueReportForm(BaseModel):
    """Recency-biased: most detail at the failure, fading with distance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_type: str = Field(min_length=3, max_length=8)
    codebase_objective: str = Field(min_length=1, max_length=2000)
    hypothesis: str = Field(min_length=1, max_length=2000)
    goal: str = Field(min_length=1, max_length=2000)
    plan: str = Field(min_length=1, max_length=2000)
    failure_point: FailurePoint
    signal_subcase: str | None = Field(default=None, max_length=64)
    recent_actions: list[RecentAction] = Field(min_length=1, max_length=RECENT_ACTIONS)
    earlier_phases: list[str] = Field(default_factory=list, max_length=40)
    session_start: str = Field(min_length=1, max_length=500)

    @field_validator("codebase_objective", "hypothesis", "goal", "plan", "session_start")
    @classmethod
    def _bar(cls, value: str) -> str:
        return _checked(value, "narrative")

    @field_validator("earlier_phases")
    @classmethod
    def _phases(cls, value: list[str]) -> list[str]:
        for entry in value:
            if len(entry) > 500:
                raise ValueError("earlier_phases entries must be one line each")
            _checked(entry, "earlier_phases")
        return value


class ToolUsed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(min_length=1, max_length=120)
    purpose: str = Field(min_length=1, max_length=500)

    @field_validator("tool", "purpose")
    @classmethod
    def _bar(cls, value: str) -> str:
        return _checked(value, "tools_used")


class CheckInForm(BaseModel):
    """A routine activity record. Deliberately shaped unlike an issue report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    codebase_summary: str = Field(min_length=1, max_length=2000)
    work_summary: str = Field(min_length=1, max_length=4000)
    tools_used: list[ToolUsed] = Field(default_factory=list, max_length=60)
    effectiveness_observed: str = Field(min_length=1, max_length=2000)

    @field_validator("codebase_summary", "work_summary")
    @classmethod
    def _bar(cls, value: str) -> str:
        return _checked(value, "narrative")

    @field_validator("effectiveness_observed")
    @classmethod
    def _outcomes_only(cls, value: str) -> str:
        _checked(value, "effectiveness_observed")
        try:
            check_no_self_rating(value)
        except NarrativeContentError as exc:
            raise ValueError(str(exc)) from exc
        return value


def _flatten(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(item) for item in error.get("loc", ()))
        parts.append(f"{location or 'form'}: {error.get('msg', 'invalid')}")
    return "; ".join(parts)


def validate_issue_form(form: Any) -> tuple[dict[str, Any], Signal]:
    """Validate a submitted issue report and resolve its signal type."""

    if not isinstance(form, dict):
        raise ValueError("submit an issue-report object, not a bare value")
    try:
        model = IssueReportForm.model_validate(form)
    except ValidationError as exc:
        raise ValueError(_flatten(exc)) from exc
    try:
        signal = Signal(model.signal_type)
    except ValueError as exc:
        raise ValueError(
            f"signal_type must be one of {sorted(s.value for s in MODEL_SIGNALS)}"
        ) from exc
    if signal not in MODEL_SIGNALS:
        raise ValueError(
            f"{signal.value} is server-detected; the skill reports S-4 through S-14 only"
        )
    required = SUBCASE_REQUIRED.get(signal)
    if required:
        if model.signal_subcase not in required:
            raise ValueError(
                f"{signal.value} requires signal_subcase to be one of {list(required)}"
            )
    return model.model_dump(mode="json"), signal


def validate_checkin_form(form: Any) -> dict[str, Any]:
    """Validate a submitted routine check-in."""

    if not isinstance(form, dict):
        raise ValueError("submit a check-in object, not a bare value")
    try:
        model = CheckInForm.model_validate(form)
    except ValidationError as exc:
        raise ValueError(_flatten(exc)) from exc
    return model.model_dump(mode="json")


__all__ = [
    "RECENT_ACTIONS",
    "CheckInForm",
    "FailurePoint",
    "IssueReportForm",
    "RecentAction",
    "ToolUsed",
    "validate_checkin_form",
    "validate_issue_form",
]
