"""Target resolution and live-validation-before-profile-commit primitives."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pyocd_debug_mcp.firmstore.profiles import BoardProfile, ProfileRepository
from pyocd_debug_mcp.setup_flow.research import (
    ResearchRequest,
    ResearchTracker,
    make_research_request,
)

_TARGET_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class TargetResolutionError(RuntimeError):
    """A target or enrichment candidate failed deterministic validation."""

    def __init__(
        self, code: str, message: str, *, observed: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.observed = dict(observed or {})


@dataclass(frozen=True, slots=True)
class TargetResolution:
    status: Literal["exact", "research"]
    target: str | None = None
    research_request: ResearchRequest | None = None
    agent_prompt: Mapping[str, Any] | None = None


class TargetResolver:
    """Resolve exact detection first and request research only for unknown targets."""

    def __init__(self, research: ResearchTracker) -> None:
        self._research = research

    def resolve_detection(
        self,
        *,
        board_id: str,
        mcu_part_number: str,
        detected_targets: Sequence[str],
        continuation_token: str,
        observed_output: Sequence[Mapping[str, Any]] = (),
    ) -> TargetResolution:
        exact = tuple(
            dict.fromkeys(target.strip() for target in detected_targets if target.strip())
        )
        if len(exact) == 1:
            return TargetResolution(status="exact", target=exact[0])
        unresolved = (
            "No exact pyOCD target was auto-detected for the exact MCU part number."
            if not exact
            else "Multiple target identifiers were detected and no exact choice is deterministic."
        )
        request = make_research_request(
            fact_id="pyocd_target",
            continuation_token=continuation_token,
            board_id=board_id,
            mcu_part_number=mcu_part_number,
            unresolved_fact=unresolved,
            requested_fields=("pyocd_target", "evidence", "reasoning_summary"),
            authoritative_facts={"detected_targets": list(exact)},
            observed_output=observed_output,
            acceptable_sources=("pyOCD built-in target list", "official vendor CMSIS-Pack"),
            validation_plan=(
                "Check target syntax and exact-part consistency.",
                "Confirm built-in or staged package support.",
                "Live-connect before committing the profile.",
            ),
        )
        prompt = self._research.prompt(request)
        prompt["optional_response_fields"] = {
            "when": (
                "Supply all three only when target/probe documentation requires a non-default "
                "debug attachment policy; the server will live-test it before persistence."
            ),
            "fields": ["debug_protocol", "debug_connect_mode", "debug_clock_hz"],
            "allowed": {
                "debug_protocol": ["default", "swd", "jtag"],
                "debug_connect_mode": ["attach", "halt", "pre-reset", "under-reset"],
                "debug_clock_hz": "positive integer Hz",
            },
        }
        prompt["agent_prompt"] = (
            str(prompt["agent_prompt"])
            + " If authoritative target/probe documentation requires a non-default attachment, "
            "also include all three typed debug fields described in optional_response_fields."
        )
        return TargetResolution(
            status="research",
            research_request=request,
            agent_prompt=prompt,
        )

    @staticmethod
    def validate_candidate(
        candidate: str,
        *,
        expected_target: str,
        built_in_targets: Sequence[str],
        staged_targets: Sequence[str] = (),
    ) -> Literal["built_in", "staged_pack"]:
        if _TARGET_PATTERN.fullmatch(candidate) is None:
            raise TargetResolutionError(
                "target/invalid-syntax", "Target must be a valid pyOCD target identifier"
            )
        if candidate != expected_target:
            raise TargetResolutionError(
                "target/reviewed-mapping-mismatch",
                "Target candidate does not match the exact reviewed board/part mapping",
            )
        if candidate in built_in_targets:
            return "built_in"
        if candidate in staged_targets:
            return "staged_pack"
        raise TargetResolutionError(
            "target/support-missing", "Target is absent from built-in and staged support"
        )


@dataclass(frozen=True, slots=True)
class SiliconIdentityCandidate:
    address: int
    expected: int
    mask: int
    width_bits: int = 32
    label: str = "silicon identity"


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    fields: Mapping[str, Any] = field(default_factory=dict)
    observations: Mapping[str, Any] = field(default_factory=dict)


class ProfileCommitCoordinator:
    """Keep staged facts off disk until the target has connected successfully."""

    def __init__(
        self,
        repository: ProfileRepository,
        *,
        live_connect: Callable[[str, str | None], None],
        before_commit: Callable[[], None] | None = None,
    ) -> None:
        self._repository = repository
        self._live_connect = live_connect
        self._before_commit = before_commit or (lambda: None)

    def commit_core(
        self,
        fields: Mapping[str, object],
        *,
        pack_path: str | None = None,
    ) -> BoardProfile:
        staged = self._repository.stage_core(copy.deepcopy(dict(fields)))
        target = staged.profile.board.pyocd_target
        try:
            self._live_connect(target, pack_path)
        except Exception as exc:
            raise TargetResolutionError(
                "target/live-connect-failed",
                f"Live target connection failed before profile commit: {exc}",
            ) from exc
        self._before_commit()
        return self._repository.commit_core(staged)

    def commit_optional(self, board_id: str, result: EnrichmentResult) -> BoardProfile:
        if not result.fields:
            return self._repository.load(board_id, include_legacy=False)
        staged = self._repository.stage_optional(board_id, result.fields)
        self._before_commit()
        return self._repository.commit_optional(staged)
