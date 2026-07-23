"""Lean, non-destructive live board-identity validation."""

from __future__ import annotations

import copy
import secrets
from contextlib import nullcontext
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, ContextManager, Literal

from firmware_mcp.firmstore.cache import SerialEndpoint
from firmware_mcp.firmstore.profiles import BoardProfile, ProfileError, ProfileRepository
from firmware_mcp.identity_observation import (
    IdentityObservationError,
    validate_identity_observation,
)
from firmware_mcp.firmstore.reports import ReportPaths, ReportWriter
from firmware_mcp.kernel.operations import OperationCancelledError
from firmware_mcp.setup_flow.preflight import FriendlyChoice
from firmware_mcp.target_errors import LockedTargetError, TargetConnectionError, TargetStateError

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
        if self.usb_serial is None:
            return FriendlyChoice(
                self.probe_id,
                f"{self.description} (session-local live connection)",
                "Currently connected debug probe; this token is not a hardware-stable "
                "identifier and is not stable across reconnects.",
            )
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
class ValidationBackend:
    inventory: Callable[[], ValidationInventory]
    target_supported: Callable[[str], bool | None]
    connect: Callable[[BoardProfile, ValidationProbe], object]
    read_memory: Callable[[object, int, int], int]
    close: Callable[[object], None]
    observe_identity: Callable[[BoardProfile, object], Mapping[str, Any]] | None = None


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
                "tool": "validate_board",
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
                "Validation never uses UART, installs packages, flashes, recovers, or rewrites profiles.",
                "Validation is diagnostic output; each operation checks the live facts it needs.",
            ],
            "rejected_candidates": [],
            "accepted_response": accepted_response,
            "validation_steps": [f"{step.number}. {step.name}" for step in self.steps],
        }


def _prompt(message: str) -> str:
    return message.strip()


def _stable_probe_identity_equal(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    left_normalized = left.strip().casefold()
    right_normalized = right.strip().casefold()
    if left_normalized == right_normalized:
        return True
    if left_normalized.isdecimal() and right_normalized.isdecimal():
        return (left_normalized.lstrip("0") or "0") == (right_normalized.lstrip("0") or "0")
    return False


class BoardValidator:
    """Report live silicon identity and connection diagnostics."""

    def __init__(
        self,
        profiles: ProfileRepository,
        reports: ReportWriter,
        backend: ValidationBackend,
        *,
        cancellation_checkpoint: Callable[[], None] | None = None,
        lock_for_board: Callable[[str], ContextManager[object]] | None = None,
    ) -> None:
        self._profiles = profiles
        self._reports = reports
        self._backend = backend
        self._cancellation_checkpoint = cancellation_checkpoint or (lambda: None)
        self._lock_for_board = lock_for_board or (lambda _board_id: nullcontext())

    def validate(self, request: ValidationRequest) -> ValidationResult:
        """Run one complete validation while serializing its logical board."""

        with self._lock_for_board(request.board_id):
            return self._validate_locked(request)

    def _validate_locked(self, request: ValidationRequest) -> ValidationResult:
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
        self._cancellation_checkpoint()
        try:
            try:
                profile = self._profiles.load(request.board_id)
            except ProfileError as exc:
                raise ValidationBackendError(
                    "validation_incomplete", "validation/profile-missing", str(exc)
                ) from exc
            if profile.mcu_part_number is None:
                raise ValidationBackendError(
                    "validation_incomplete",
                    "validation/profile-incomplete",
                    "The board profile lacks required schema-v2 identity facts.",
                )
            if profile.source_path.exists():
                profile_before = profile.source_path.read_bytes()
            steps.append(
                ValidationStep(
                    1,
                    "Load persisted profile",
                    "passed",
                    {"profile": profile.source_path.name},
                )
            )

            inventory = self._backend.inventory()
            self._cancellation_checkpoint()
            observed["inventory"] = {"probes": [asdict(item) for item in inventory.probes]}
            steps.append(ValidationStep(2, "Enumerate debug probes", "passed"))

            selected_probe, probe_choices = self._select_probe(profile, inventory, request.probe_id)
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
            probe_identity_scope = (
                "hardware-stable" if selected_probe.usb_serial is not None else "session-local"
            )
            observed["probe_identity"] = probe_identity
            observed["probe_identity_scope"] = probe_identity_scope
            steps.append(
                ValidationStep(
                    3,
                    (
                        "Resolve intended stable hardware probe identity"
                        if selected_probe.usb_serial is not None
                        else "Resolve intended live session-local probe identity"
                    ),
                    "passed",
                    {
                        "probe_id": selected_probe.probe_id,
                        "probe_identity": probe_identity,
                        "identity_scope": probe_identity_scope,
                    },
                )
            )

            support = self._backend.target_supported(profile.board.target)
            self._cancellation_checkpoint()
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
            steps.append(ValidationStep(4, "Confirm verified target support", "passed"))

            connection = self._backend.connect(profile, selected_probe)
            self._cancellation_checkpoint()
            steps.append(ValidationStep(5, "Connect without target mutation", "passed"))

            if self._backend.observe_identity is not None:
                try:
                    identity = dict(self._backend.observe_identity(profile, connection))
                except TargetStateError as exc:
                    # Import only while handling the runtime observation.  The live-identity
                    # service depends on support replay, whose package exposes this validator.
                    # Keeping that one-way import edge avoids a startup cycle.
                    from firmware_mcp.services.live_identity import LiveIdentityContradiction

                    if isinstance(exc, LiveIdentityContradiction):
                        raise ValidationBackendError(
                            "validation_failed",
                            "validation/live-identity-contradiction",
                            f"Current live identity contradicts the configured session: {exc}",
                        ) from exc
                    raise ValidationBackendError(
                        "validation_failed",
                        "validation/live-identity-observation-failed",
                        "Current configured live identity could not be observed; reconnect and retry "
                        f"validation: {exc}",
                    ) from exc
                observed["live_identity"] = identity
                capability = identity.get("capability")
                comparison = identity.get("comparison_status")
                steps.append(
                    ValidationStep(
                        6,
                        "Observe replayed live identity capability",
                        "passed",
                        {"capability": capability, "comparison_status": comparison},
                    )
                )
                status = "validation_passed"
                code = "validation/passed"
                if capability == "compatible":
                    message = (
                        "Compatible live identity was reread and matched; it is not exact MCU-part identity. "
                        "UART readiness is reported separately by get_setup_status."
                    )
                elif capability == "unavailable":
                    message = (
                        "Live identity comparison is unavailable for this session; this diagnostic does not "
                        "invent exact part identity. UART readiness is reported separately by get_setup_status."
                    )
                else:
                    message = (
                        "Exact live silicon identity was validated. UART readiness is reported separately "
                        "by get_setup_status."
                    )
            else:
                identity_address = profile.board.silicon_id_addr
                identity_expected = profile.board.silicon_id_expected
                identity_mask = profile.board.silicon_id_mask
                identity_width = profile.board.silicon_id_width_bits
                if identity_address is None or identity_expected is None or identity_mask is None:
                    observed["capability_level"] = "connected_diagnostics_only"
                    raise ValidationBackendError(
                        "validation_blocked",
                        "validation/live-identity-evidence-missing",
                        "Live attach succeeded, but replayable silicon-identity evidence is unavailable.",
                    )
                raw_actual = self._backend.read_memory(connection, identity_address, identity_width)
                try:
                    actual = validate_identity_observation(raw_actual, identity_width)
                except IdentityObservationError as exc:
                    raise ValidationBackendError(
                        "validation_failed",
                        "validation/live-identity-observation-failed",
                        "Live silicon identity observation is malformed; reconnect and retry "
                        "validation before using this profile.",
                    ) from exc
                self._cancellation_checkpoint()
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
                    self._cancellation_checkpoint()
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
                steps.append(
                    ValidationStep(
                        7,
                        "Report live identity comparison",
                        "passed",
                        {"observed_mcu": observed_identity},
                    )
                )
                status = "validation_passed"
                code = "validation/passed"
                message = (
                    "Live silicon identity was validated. "
                    "UART readiness is reported separately by get_setup_status."
                )
        except OperationCancelledError:
            raise
        except ValidationBackendError as exc:
            status = exc.status
            code = exc.code
            message = str(exc)
        except LockedTargetError as exc:
            status, code, message = (
                "validation_blocked",
                "validation/target-locked",
                f"The target is locked. Use recover_target if the connected provider supports it, then validate again: {exc}",
            )
        except TargetConnectionError as exc:
            status, code, message = (
                "validation_blocked",
                "validation/backend-unavailable",
                f"The debug backend could not establish the validation connection: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 - every validation attempt needs a report
            status, code, message = (
                "validation_blocked",
                f"validation/{type(exc).__name__}",
                f"Validation stopped at a provider operation: {exc}",
            )
        finally:
            if connection is not None:
                try:
                    self._backend.close(connection)
                except Exception as exc:  # noqa: BLE001 - preserve primary result and report
                    observed["close_error"] = str(exc)

        self._cancellation_checkpoint()
        try:
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
        except Exception:
            raise
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
        selected = next(
            (
                probe
                for probe in compatible
                if _stable_probe_identity_equal(probe.probe_id, selected_id)
                or _stable_probe_identity_equal(probe.usb_serial, selected_id)
            ),
            None,
        )
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
