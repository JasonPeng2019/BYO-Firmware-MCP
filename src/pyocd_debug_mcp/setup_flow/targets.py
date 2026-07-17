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

    def __init__(self, code: str, message: str, *, observed: Mapping[str, Any] | None = None) -> None:
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
        exact = tuple(dict.fromkeys(target.strip() for target in detected_targets if target.strip()))
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
            requested_fields=("pyocd_target",),
            authoritative_facts={"detected_targets": list(exact)},
            observed_output=observed_output,
            acceptable_sources=("pyOCD built-in target list", "official vendor CMSIS-Pack"),
            validation_plan=(
                "Check target syntax and exact-part consistency.",
                "Confirm built-in or staged package support.",
                "Live-connect before committing the profile.",
            ),
        )
        return TargetResolution(
            status="research",
            research_request=request,
            agent_prompt=self._research.prompt(request),
        )

    @staticmethod
    def validate_candidate(
        candidate: str,
        *,
        mcu_part_number: str,
        part_consistent: Callable[[str, str], bool],
        built_in_targets: Sequence[str],
        staged_targets: Sequence[str] = (),
    ) -> Literal["built_in", "staged_pack"]:
        if _TARGET_PATTERN.fullmatch(candidate) is None:
            raise TargetResolutionError(
                "target/invalid-syntax", "Target must be a valid pyOCD target identifier"
            )
        if not part_consistent(mcu_part_number, candidate):
            raise TargetResolutionError(
                "target/part-mismatch",
                "Target candidate is not consistent with the exact MCU part number",
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


class EnrichmentValidator:
    """Validate optional profile facts against safe live reads."""

    def __init__(
        self,
        *,
        safe_readable: Callable[[int, int], bool],
        read_value: Callable[[int, int], int],
    ) -> None:
        self._safe_readable = safe_readable
        self._read_value = read_value

    def test_read_address(self, address: int, *, width_bits: int = 32) -> EnrichmentResult:
        self._validate_location(address, width_bits)
        actual = self._read_live(address, width_bits)
        return EnrichmentResult(
            fields={"test_read_address": address},
            observations={"address": address, "width_bits": width_bits, "actual": actual},
        )

    def silicon_identity(
        self, candidate: SiliconIdentityCandidate | None
    ) -> EnrichmentResult:
        if candidate is None:
            return EnrichmentResult(observations={"silicon_identity": "not supplied; optional"})
        self._validate_location(candidate.address, candidate.width_bits)
        full_mask = (1 << candidate.width_bits) - 1
        if candidate.mask < 0 or candidate.mask > full_mask:
            raise TargetResolutionError(
                "enrichment/invalid-mask", "Silicon identity mask exceeds its read width"
            )
        actual = self._read_live(candidate.address, candidate.width_bits)
        if actual & candidate.mask != candidate.expected & candidate.mask:
            raise TargetResolutionError(
                "enrichment/silicon-mismatch",
                "Live silicon identity does not match the candidate",
                observed={
                    "actual": actual,
                    "expected": candidate.expected,
                    "mask": candidate.mask,
                },
            )
        return EnrichmentResult(
            fields={
                "silicon_id_address": candidate.address,
                "silicon_id_expected": candidate.expected,
                "silicon_id_mask": candidate.mask,
                "silicon_id_width_bits": candidate.width_bits,
                "silicon_id_label": candidate.label,
            },
            observations={"actual": actual, "masked_match": True},
        )

    def _validate_location(self, address: int, width_bits: int) -> None:
        if address < 0 or width_bits not in {8, 16, 32}:
            raise TargetResolutionError(
                "enrichment/invalid-read", "Address and width are not a valid live read"
            )
        if not self._safe_readable(address, width_bits):
            raise TargetResolutionError(
                "enrichment/unsafe-read", "Candidate address is not classified safe-readable"
            )

    def _read_live(self, address: int, width_bits: int) -> int:
        try:
            return self._read_value(address, width_bits)
        except Exception as exc:
            raise TargetResolutionError(
                "enrichment/live-read-failed", f"Live candidate read failed: {exc}"
            ) from exc


class ProfileCommitCoordinator:
    """Keep staged facts off disk until the target has connected successfully."""

    def __init__(
        self,
        repository: ProfileRepository,
        *,
        live_connect: Callable[[str, str | None], None],
    ) -> None:
        self._repository = repository
        self._live_connect = live_connect

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
        return self._repository.commit_core(staged)

    def commit_optional(self, board_id: str, result: EnrichmentResult) -> BoardProfile:
        if not result.fields:
            return self._repository.load(board_id, include_legacy=False)
        staged = self._repository.stage_optional(board_id, result.fields)
        return self._repository.commit_optional(staged)
