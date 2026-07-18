"""Lean, bounded, non-destructive live board-identity validation."""

from __future__ import annotations

import copy
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from pyocd_debug_mcp.firmstore.cache import SerialEndpoint
from pyocd_debug_mcp.firmstore.profiles import BoardProfile, ProfileError, ProfileRepository
from pyocd_debug_mcp.firmstore.reports import ReportPaths, ReportWriter
from pyocd_debug_mcp.setup_flow.preflight import FriendlyChoice, NO_INTERNALS_RELAY_INSTRUCTION
from pyocd_debug_mcp.target_errors import LockedTargetError, TargetConnectionError

VALIDATION_STATUSES = (
    "validation_passed",
    "validation_needs_user_input",
    "validation_research_required",
    "validation_blocked",
    "validation_failed",
    "validation_incomplete",
)
ValidationStatus = Literal[
    "validation_passed",
    "validation_needs_user_input",
    "validation_research_required",
    "validation_blocked",
    "validation_failed",
    "validation_incomplete",
]

DEFAULT_STEP_TIMEOUT_SECONDS = 5.0


class ValidationBackendError(RuntimeError):
    """A typed validation backend failure with an exact result classification."""

    status: ValidationStatus
    code: str

    def __init__(self, status: ValidationStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class ValidationProbe:
    probe_id: str
    description: str
    probe_family: str
    usb_serial: str | None = None

    def choice(self) -> FriendlyChoice:
        suffix = self.usb_serial[-6:] if self.usb_serial else "unknown serial"
        return FriendlyChoice(
            self.probe_id,
            f"{self.description} (identifier ending {suffix})",
            "Currently connected debug probe",
        )


@dataclass(frozen=True, slots=True)
class ValidationSerial:
    """Shared inventory record; lean validation deliberately never selects or reads it."""

    serial_id: str
    port_path: str
    description: str
    usb_serial: str | None = None
    vid: int | None = None
    pid: int | None = None

    def endpoint(self) -> SerialEndpoint:
        return SerialEndpoint(self.port_path, self.usb_serial, self.vid, self.pid)


@dataclass(frozen=True, slots=True)
class ValidationInventory:
    probes: tuple[ValidationProbe, ...] = ()
    serial_ports: tuple[ValidationSerial, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationRequest:
    board_id: str
    probe_id: str | None = None


@dataclass(frozen=True, slots=True)
class SafetyMapSnapshot:
    """Non-authoritative validation view of the parsed single safety map."""

    present: bool
    consistent: bool
    map_digest: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ValidationHooks:
    """Server-owned map, gate, and mismatch integrations."""

    load_safety_map: Callable[[BoardProfile], SafetyMapSnapshot]
    stamp_session: Callable[[str, str, str, str | None, str, str], bool]
    record_mismatch: Callable[[str, str, str, str | None, str, str], bool]

    @classmethod
    def closed_placeholders(cls) -> ValidationHooks:
        return cls(
            load_safety_map=lambda _profile: SafetyMapSnapshot(
                False,
                False,
                reason="The schema-v2 safety map is unavailable. Run board_safety_refresh.",
            ),
            stamp_session=lambda _board, _run, _probe_id, _probe_uid, _mcu, _digest: False,
            record_mismatch=lambda _board, _run, _probe_id, _probe_uid, _expected, _observed: False,
        )


@dataclass(frozen=True, slots=True)
class ValidationBackend:
    inventory: Callable[[], ValidationInventory]
    target_supported: Callable[[str], bool | None]
    connect: Callable[[BoardProfile, ValidationProbe, float], object]
    read_memory: Callable[[object, int, int, float], int]
    # Kept as a shared backend service for setup/status callers. BoardValidator never invokes it.
    capture_serial: Callable[[ValidationSerial, int, float, int], str]
    close: Callable[[object], None]


@dataclass(frozen=True, slots=True)
class ValidationStep:
    number: int
    name: str
    outcome: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    status: ValidationStatus
    code: str
    validation_id: str
    agent_prompt: str
    choices: tuple[FriendlyChoice, ...]
    observed: Mapping[str, Any]
    steps: tuple[ValidationStep, ...]
    report_paths: ReportPaths
    retry_arguments: Mapping[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        accepted_response = None
        if self.status == "validation_needs_user_input" and self.retry_arguments is not None:
            accepted_response = {
                "tool": "board_validate",
                "arguments": copy.deepcopy(dict(self.retry_arguments)),
            }
        return {
            "status": self.status,
            "code": self.code,
            "continuation_id": self.validation_id,
            "agent_prompt": self.agent_prompt,
            "choices": [asdict(choice) for choice in self.choices],
            "observed": copy.deepcopy(dict(self.observed)),
            "constraints": [
                "Validation is bounded and non-destructive.",
                "Validation never uses UART, installs packages, flashes, recovers, or rewrites profiles.",
                "A successful result stamps only this board and connection in memory.",
            ],
            "rejected_candidates": [],
            "accepted_response": accepted_response,
            "validation_plan": [f"{step.number}. {step.name}" for step in self.steps],
        }


def _prompt(message: str) -> str:
    return f"{message.strip()} {NO_INTERNALS_RELAY_INSTRUCTION}"


class BoardValidator:
    """Prove live silicon identity and associate one parsed safety map."""

    def __init__(
        self,
        profiles: ProfileRepository,
        reports: ReportWriter,
        backend: ValidationBackend,
        *,
        hooks: ValidationHooks | None = None,
        step_timeout_seconds: float = DEFAULT_STEP_TIMEOUT_SECONDS,
    ) -> None:
        if step_timeout_seconds <= 0:
            raise ValueError("step timeout must be positive")
        self._profiles = profiles
        self._reports = reports
        self._backend = backend
        self._hooks = hooks or ValidationHooks.closed_placeholders()
        self._step_timeout_seconds = step_timeout_seconds

    def validate(self, request: ValidationRequest) -> ValidationResult:
        validation_id = f"validation-{secrets.token_hex(8)}"
        steps: list[ValidationStep] = []
        observed: dict[str, Any] = {"board_id": request.board_id}
        connection: object | None = None
        choices: tuple[FriendlyChoice, ...] = ()
        profile: BoardProfile | None = None
        profile_before: bytes | None = None
        selected_probe: ValidationProbe | None = None
        retry_arguments: dict[str, Any] | None = None
        status: ValidationStatus
        code: str
        message: str
        safety_map = SafetyMapSnapshot(False, False, reason="not loaded")
        try:
            try:
                profile = self._profiles.load(request.board_id)
            except ProfileError as exc:
                raise ValidationBackendError(
                    "validation_incomplete", "validation/profile-missing", str(exc)
                ) from exc
            if profile.schema_version < 2 or profile.mcu_part_number is None:
                raise ValidationBackendError(
                    "validation_incomplete",
                    "validation/profile-incomplete",
                    "The board profile lacks required schema-v2 identity facts.",
                )
            if profile.source_path.exists():
                profile_before = profile.source_path.read_bytes()
            safety_map = self._hooks.load_safety_map(profile)
            steps.append(
                ValidationStep(
                    1,
                    "Load profile and safety map",
                    "passed" if safety_map.present else "missing",
                    {"profile": profile.source_path.name, "map_reason": safety_map.reason},
                )
            )

            inventory = self._backend.inventory()
            observed["inventory"] = {"probes": [asdict(item) for item in inventory.probes]}
            steps.append(ValidationStep(2, "Enumerate debug probes", "passed"))

            selected_probe, probe_choices = self._select_probe(
                profile, inventory, request.probe_id
            )
            if probe_choices:
                choices = probe_choices
                retry_arguments = {
                    "board_id": request.board_id,
                    "probe_id": "<one choice_id from choices>",
                }
                raise ValidationBackendError(
                    "validation_needs_user_input",
                    "validation/probe-selection-required",
                    "Ask which friendly probe is connected to the intended board.",
                )
            probe_identity = selected_probe.usb_serial or selected_probe.probe_id
            observed["probe_identity"] = probe_identity
            steps.append(
                ValidationStep(
                    3,
                    "Resolve intended stable probe identity",
                    "passed",
                    {"probe_id": selected_probe.probe_id, "probe_identity": probe_identity},
                )
            )

            support = self._backend.target_supported(profile.board.pyocd_target)
            if support is None:
                raise ValidationBackendError(
                    "validation_research_required",
                    "validation/target-metadata-unknown",
                    "Target-support metadata is unresolved and requires the research handoff.",
                )
            if not support:
                raise ValidationBackendError(
                    "validation_blocked",
                    "validation/target-unavailable",
                    "The profile target is unavailable from built-in or pinned support.",
                )
            steps.append(ValidationStep(4, "Confirm reviewed target support", "passed"))

            identity_address = profile.board.silicon_id_addr
            identity_expected = profile.board.silicon_id_expected
            identity_mask = profile.board.silicon_id_mask
            identity_width = profile.board.silicon_id_width_bits
            if identity_address is None or identity_expected is None or identity_mask is None:
                raise ValidationBackendError(
                    "validation_blocked",
                    "validation/live-identity-evidence-missing",
                    "Reviewed live silicon-identity evidence is unavailable for this board. "
                    "Validation cannot stamp a gate until maintainers add that evidence.",
                )

            connection = self._backend.connect(
                profile, selected_probe, self._step_timeout_seconds
            )
            steps.append(ValidationStep(5, "Connect without target mutation", "passed"))

            actual = self._backend.read_memory(
                connection,
                identity_address,
                identity_width,
                self._step_timeout_seconds,
            )
            digits = max(1, (identity_width + 3) // 4)
            identity_label = profile.board.silicon_id_label or "silicon identity"
            observed_identity = f"{identity_label} 0x{actual:0{digits}X}"
            observed.update(
                {
                    "expected_mcu": profile.mcu_part_number,
                    "observed_mcu": observed_identity,
                    "silicon_actual": actual,
                    "silicon_expected": identity_expected,
                    "silicon_mask": identity_mask,
                }
            )
            if actual & identity_mask != identity_expected & identity_mask:
                recorded = self._hooks.record_mismatch(
                    profile.board_id,
                    validation_id,
                    selected_probe.probe_id,
                    selected_probe.usb_serial,
                    profile.mcu_part_number,
                    observed_identity,
                )
                observed["mismatch_allowance_recorded"] = recorded
                raise ValidationBackendError(
                    "validation_failed",
                    "validation/silicon-mismatch",
                    f"Expected MCU {profile.mcu_part_number}, but observed {observed_identity}. "
                    "Tell the user about the mismatch and ask what they want to do. The established "
                    "profile was not changed.",
                )
            steps.append(
                ValidationStep(
                    6,
                    "Read and compare reviewed silicon identity",
                    "passed",
                    {"observed_mcu": observed_identity},
                )
            )

            if not safety_map.present:
                steps.append(
                    ValidationStep(
                        7,
                        "Associate current safety map",
                        "incomplete",
                        {"reason": safety_map.reason},
                    )
                )
                raise ValidationBackendError(
                    "validation_incomplete",
                    "validation/safety-missing",
                    safety_map.reason
                    or "The schema-v2 safety map is absent. Run board_safety_refresh.",
                )
            if not safety_map.consistent or not safety_map.map_digest:
                steps.append(
                    ValidationStep(
                        7,
                        "Associate current safety map",
                        "failed",
                        {"reason": safety_map.reason},
                    )
                )
                raise ValidationBackendError(
                    "validation_blocked",
                    "validation/safety-invalid",
                    safety_map.reason
                    or "The safety map is inconsistent. Run board_safety_refresh.",
                )
            stamped = self._hooks.stamp_session(
                profile.board_id,
                validation_id,
                selected_probe.probe_id,
                selected_probe.usb_serial,
                observed_identity,
                safety_map.map_digest,
            )
            if not stamped:
                steps.append(ValidationStep(7, "Associate current safety map", "failed"))
                raise ValidationBackendError(
                    "validation_incomplete",
                    "validation/stamp-failed",
                    "The validated connection could not be stamped; reconnect and run board_validate.",
                )
            steps.append(ValidationStep(7, "Associate current safety map", "passed"))
            status = "validation_passed"
            code = "validation/passed"
            message = (
                "Live silicon identity and the current safety map were validated. "
                "UART readiness is reported separately by get_setup_status."
            )
        except ValidationBackendError as exc:
            status = exc.status
            code = exc.code
            message = str(exc)
        except LockedTargetError as exc:
            status, code, message = (
                "validation_blocked",
                "validation/target-locked",
                f"The target is locked. Use the separate unlock plan, then validate again: {exc}",
            )
        except TargetConnectionError as exc:
            status, code, message = (
                "validation_blocked",
                "validation/backend-unavailable",
                f"The debug backend could not establish the bounded validation connection: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 - every validation attempt needs a report
            status, code, message = (
                "validation_blocked",
                f"validation/{type(exc).__name__}",
                f"Validation stopped at a bounded backend operation: {exc}",
            )
        finally:
            if connection is not None:
                try:
                    self._backend.close(connection)
                except Exception as exc:  # noqa: BLE001 - preserve primary result and report
                    observed["close_error"] = str(exc)

        report_paths = self._write_report(
            validation_id,
            request,
            status,
            code,
            message,
            observed,
            steps,
            profile,
            profile_before,
        )
        return ValidationResult(
            status,
            code,
            validation_id,
            _prompt(message),
            choices,
            observed,
            tuple(steps),
            report_paths,
            retry_arguments,
        )

    def _select_probe(
        self,
        profile: BoardProfile,
        inventory: ValidationInventory,
        selected_id: str | None,
    ) -> tuple[ValidationProbe, tuple[FriendlyChoice, ...]]:
        compatible = [
            probe
            for probe in inventory.probes
            if probe.probe_family.casefold() == profile.board.probe_family.casefold()
        ]
        if not compatible:
            raise ValidationBackendError(
                "validation_blocked",
                "validation/no-probe",
                "No compatible debug probe is currently visible.",
            )
        selected = next((probe for probe in compatible if probe.probe_id == selected_id), None)
        if selected_id is not None and selected is None:
            return compatible[0], tuple(probe.choice() for probe in compatible)
        if selected is not None:
            return selected, ()
        if len(compatible) > 1:
            return compatible[0], tuple(probe.choice() for probe in compatible)
        return compatible[0], ()

    def _write_report(
        self,
        validation_id: str,
        request: ValidationRequest,
        status: ValidationStatus,
        code: str,
        message: str,
        observed: Mapping[str, Any],
        steps: Sequence[ValidationStep],
        profile: BoardProfile | None,
        profile_before: bytes | None,
    ) -> ReportPaths:
        paths = self._reports.create_validation(
            validation_id,
            {
                "board_id": request.board_id,
                "terminal_status": status,
                "code": code,
                "message": message,
                "observed": copy.deepcopy(dict(observed)),
                "steps": [asdict(step) for step in steps],
                "profile_unchanged": (
                    profile_before == profile.source_path.read_bytes()
                    if profile_before is not None and profile is not None
                    else True
                ),
            },
        )
        for step in steps:
            self._reports.append_validation_event(
                validation_id,
                {"step": step.number, "name": step.name, "outcome": step.outcome},
            )
        if not steps:
            self._reports.append_validation_event(
                validation_id, {"step": 0, "name": "validation", "outcome": status}
            )
        return paths


def profile_from_board_config(board: Any, source_path: Path) -> BoardProfile:
    """Build a read-only compatibility view for the Stage 0 shared engine."""

    part_number = getattr(board, "mcu_part_number", None) or board.pyocd_target
    document = {
        "board_id": board.board_id,
        "display_name": board.display_name,
        "mcu_part_number": part_number,
        "mcu_family": board.mcu_family,
        "probe_family": board.probe_family,
        "pyocd_target": board.pyocd_target,
    }
    return BoardProfile(2, part_number, board, None, None, None, source_path, True, document)
