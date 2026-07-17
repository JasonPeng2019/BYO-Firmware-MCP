"""Bounded, non-destructive board validation shared by MCP and Stage 0."""

from __future__ import annotations

import copy
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from pyocd_debug_mcp.firmstore.cache import (
    AttachmentCache,
    ProbeIdentity,
    SerialEndpoint,
)
from pyocd_debug_mcp.firmstore.profiles import BoardProfile, ProfileError, ProfileRepository
from pyocd_debug_mcp.firmstore.reports import ReportPaths, ReportWriter
from pyocd_debug_mcp.setup_flow.preflight import FriendlyChoice, NO_INTERNALS_RELAY_INSTRUCTION
from pyocd_debug_mcp.target_errors import LockedTargetError, TargetConnectionError

VALIDATION_STATUSES = (
    "validation_passed",
    "validation_passed_uart_not_configured",
    "validation_needs_user_input",
    "validation_research_required",
    "validation_blocked",
    "validation_failed",
    "validation_incomplete",
)
ValidationStatus = Literal[
    "validation_passed",
    "validation_passed_uart_not_configured",
    "validation_needs_user_input",
    "validation_research_required",
    "validation_blocked",
    "validation_failed",
    "validation_incomplete",
]

DEFAULT_SERIAL_CAPTURE_SECONDS = 3.0
MAX_SERIAL_CAPTURE_BYTES = 64 * 1024
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
    serial_id: str
    port_path: str
    description: str
    usb_serial: str | None = None
    vid: int | None = None
    pid: int | None = None

    def endpoint(self) -> SerialEndpoint:
        return SerialEndpoint(self.port_path, self.usb_serial, self.vid, self.pid)

    def choice(self) -> FriendlyChoice:
        suffix = self.usb_serial[-6:] if self.usb_serial else "unknown serial"
        return FriendlyChoice(
            self.serial_id,
            f"{self.description} (identifier ending {suffix})",
            "Currently visible serial connection",
        )


@dataclass(frozen=True, slots=True)
class ValidationInventory:
    probes: tuple[ValidationProbe, ...] = ()
    serial_ports: tuple[ValidationSerial, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationRequest:
    board_id: str
    probe_id: str | None = None
    serial_id: str | None = None


@dataclass(frozen=True, slots=True)
class Layer0Snapshot:
    present: bool
    consistent: bool
    aggregate_fingerprint: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ValidationHooks:
    """Task-14 extension points for map consistency and gate stamping."""

    load_layer0: Callable[[BoardProfile], Layer0Snapshot]
    stamp_session: Callable[[str, str, str, str | None, str], bool]

    @classmethod
    def closed_placeholders(cls) -> ValidationHooks:
        return cls(
            load_layer0=lambda _profile: Layer0Snapshot(
                False,
                False,
                reason=(
                    "Safety-map consistency is intentionally unavailable until Task 14; "
                    "write-capable actions remain closed."
                ),
            ),
            stamp_session=lambda _board, _result, _probe_id, _probe_uid, _fingerprint: False,
        )


@dataclass(frozen=True, slots=True)
class ValidationBackend:
    inventory: Callable[[], ValidationInventory]
    target_supported: Callable[[str], bool | None]
    connect: Callable[[BoardProfile, ValidationProbe, float], object]
    read_memory: Callable[[object, int, int, float], int]
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

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "continuation_id": self.validation_id,
            "agent_prompt": self.agent_prompt,
            "choices": [asdict(choice) for choice in self.choices],
            "observed": copy.deepcopy(dict(self.observed)),
            "constraints": [
                "Validation is bounded and non-destructive.",
                "Validation never installs packages, flashes, recovers, or rewrites profiles.",
                "A successful result stamps only this board and connection in memory.",
            ],
            "rejected_candidates": [],
            "accepted_response": None,
            "validation_plan": [f"{step.number}. {step.name}" for step in self.steps],
        }


def _prompt(message: str) -> str:
    return f"{message.strip()} {NO_INTERNALS_RELAY_INSTRUCTION}"


class BoardValidator:
    """Execute validation steps 1-9 with no mutating backend capability."""

    def __init__(
        self,
        profiles: ProfileRepository,
        reports: ReportWriter,
        backend: ValidationBackend,
        *,
        cache: AttachmentCache | None = None,
        hooks: ValidationHooks | None = None,
        serial_capture_seconds: float = DEFAULT_SERIAL_CAPTURE_SECONDS,
        serial_capture_bytes: int = MAX_SERIAL_CAPTURE_BYTES,
        step_timeout_seconds: float = DEFAULT_STEP_TIMEOUT_SECONDS,
    ) -> None:
        if not 0 < serial_capture_seconds <= DEFAULT_SERIAL_CAPTURE_SECONDS:
            raise ValueError("serial capture must be in (0, 3] seconds")
        if not 1 <= serial_capture_bytes <= MAX_SERIAL_CAPTURE_BYTES:
            raise ValueError("serial capture byte bound must be 1-65536")
        if step_timeout_seconds <= 0:
            raise ValueError("step timeout must be positive")
        self._profiles = profiles
        self._reports = reports
        self._backend = backend
        self._cache = cache
        self._hooks = hooks or ValidationHooks.closed_placeholders()
        self._serial_capture_seconds = serial_capture_seconds
        self._serial_capture_bytes = serial_capture_bytes
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
        selected_serial: ValidationSerial | None = None
        hardware_result = "not_started"
        status: ValidationStatus
        code: str
        message: str
        layer0 = Layer0Snapshot(False, False, reason="not loaded")
        try:
            # Step 1: profile plus current Layer-0 evidence. The placeholder is
            # recorded but hardware validation still runs so M6 evidence is useful.
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
            layer0 = self._hooks.load_layer0(profile)
            steps.append(
                ValidationStep(
                    1,
                    "Load profile and safety evidence",
                    "passed" if layer0.present else "placeholder_closed",
                    {"profile": profile.source_path.name, "layer0_reason": layer0.reason},
                )
            )

            # Step 2: one bounded inventory snapshot.
            inventory = self._backend.inventory()
            observed["inventory"] = {
                "probes": [asdict(item) for item in inventory.probes],
                "serial_ports": [asdict(item) for item in inventory.serial_ports],
            }
            steps.append(ValidationStep(2, "Re-enumerate probes and serial ports", "passed"))

            # Step 3: current selection and cache resolution.
            selected_probe, probe_choices = self._select_probe(profile, inventory, request.probe_id)
            if probe_choices:
                choices = probe_choices
                raise ValidationBackendError(
                    "validation_needs_user_input",
                    "validation/probe-selection-required",
                    "Ask which friendly probe is connected to the intended board.",
                )
            needs_uart = bool(profile.board.expected_uart_substring)
            selected_serial, serial_choices, cache_reason = self._select_serial(
                profile,
                selected_probe,
                inventory,
                request.serial_id,
                needs_uart=needs_uart,
            )
            observed["cache"] = {"reason": cache_reason}
            if serial_choices:
                choices = serial_choices
                raise ValidationBackendError(
                    "validation_needs_user_input",
                    "validation/serial-selection-required",
                    "Ask which friendly serial connection belongs to the intended board.",
                )
            steps.append(
                ValidationStep(
                    3,
                    "Resolve current hardware and attachment cache",
                    "passed",
                    {
                        "probe_id": selected_probe.probe_id,
                        "serial_id": selected_serial.serial_id if selected_serial else None,
                        "cache_reason": cache_reason,
                    },
                )
            )

            # Step 4: support lookup never installs or changes a package.
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
            steps.append(ValidationStep(4, "Confirm built-in or pinned target support", "passed"))

            # Step 5: bounded live connection. No reset, halt, flash, erase, or recovery.
            connection = self._backend.connect(
                profile, selected_probe, self._step_timeout_seconds
            )
            steps.append(ValidationStep(5, "Connect and verify target compatibility", "passed"))

            # Step 6: configured safe read and optional silicon identity.
            test_value = self._backend.read_memory(
                connection,
                profile.board.test_addr,
                32,
                self._step_timeout_seconds,
            )
            check_details: dict[str, Any] = {
                "test_read_address": profile.board.test_addr,
                "test_read_value": test_value,
            }
            if (
                profile.board.silicon_id_addr is not None
                and profile.board.silicon_id_expected is not None
            ):
                actual = self._backend.read_memory(
                    connection,
                    profile.board.silicon_id_addr,
                    profile.board.silicon_id_width_bits,
                    self._step_timeout_seconds,
                )
                mask = profile.board.silicon_id_mask
                if mask is None:
                    mask = (1 << profile.board.silicon_id_width_bits) - 1
                check_details.update(
                    {"silicon_actual": actual, "silicon_expected": profile.board.silicon_id_expected, "silicon_mask": mask}
                )
                if actual & mask != profile.board.silicon_id_expected & mask:
                    raise ValidationBackendError(
                        "validation_failed",
                        "validation/silicon-mismatch",
                        "The attached hardware does not match the profile. Correct the physical "
                        "assignment or attach the intended board; the profile was not changed.",
                    )
            steps.append(ValidationStep(6, "Run configured safe memory and identity checks", "passed", check_details))

            # Step 7: Q-8 conservative fixed default, hard-capped by construction.
            expected_uart = profile.board.expected_uart_substring
            if expected_uart:
                if selected_serial is None:
                    raise ValidationBackendError(
                        "validation_blocked",
                        "validation/serial-unavailable",
                        "Configured serial validation cannot run because no serial port is available.",
                    )
                captured = self._backend.capture_serial(
                    selected_serial,
                    profile.board.default_baudrate,
                    self._serial_capture_seconds,
                    self._serial_capture_bytes,
                )
                if expected_uart not in captured:
                    raise ValidationBackendError(
                        "validation_failed",
                        "validation/uart-mismatch",
                        "The configured expected serial text was not observed within the bounded capture.",
                    )
                hardware_result = "validation_passed"
                steps.append(
                    ValidationStep(
                        7,
                        "Run bounded configured serial check",
                        "passed",
                        {"captured_bytes": len(captured.encode("utf-8"))},
                    )
                )
            else:
                hardware_result = "validation_passed_uart_not_configured"
                steps.append(
                    ValidationStep(
                        7,
                        "Run bounded configured serial check",
                        "not_configured",
                    )
                )

            self._confirm_cache(profile, selected_probe, selected_serial)

            if not layer0.present:
                steps.append(ValidationStep(8, "Confirm safety-map consistency", "incomplete", {"reason": layer0.reason}))
                raise ValidationBackendError(
                    "validation_incomplete",
                    "validation/safety-missing",
                    layer0.reason or "Required safety evidence is absent.",
                )
            if not layer0.consistent or not layer0.aggregate_fingerprint:
                steps.append(ValidationStep(8, "Confirm safety-map consistency", "failed", {"reason": layer0.reason}))
                raise ValidationBackendError(
                    "validation_blocked",
                    "validation/safety-invalid",
                    layer0.reason or "Safety evidence is inconsistent.",
                )
            steps.append(ValidationStep(8, "Confirm safety-map consistency", "passed"))

            assert layer0.aggregate_fingerprint is not None
            stamped = self._hooks.stamp_session(
                profile.board_id,
                hardware_result,
                selected_probe.probe_id,
                selected_probe.usb_serial,
                layer0.aggregate_fingerprint,
            )
            if not stamped:
                steps.append(ValidationStep(9, "Stamp validated session", "failed"))
                raise ValidationBackendError(
                    "validation_incomplete",
                    "validation/stamp-failed",
                    "The validated connection could not be stamped; reconnect and run board_validate.",
                )
            steps.append(ValidationStep(9, "Stamp validated session", "passed"))
            if hardware_result == "validation_passed":
                status = "validation_passed"
            else:
                status = "validation_passed_uart_not_configured"
            code = "validation/passed"
            message = "All configured validation checks passed for this board."
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

        observed["hardware_result"] = hardware_result
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
        )

    def _select_probe(
        self,
        profile: BoardProfile,
        inventory: ValidationInventory,
        selected_id: str | None,
    ) -> tuple[ValidationProbe, tuple[FriendlyChoice, ...]]:
        compatible: list[ValidationProbe] = [
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

    def _select_serial(
        self,
        profile: BoardProfile,
        probe: ValidationProbe,
        inventory: ValidationInventory,
        selected_id: str | None,
        *,
        needs_uart: bool,
    ) -> tuple[ValidationSerial | None, tuple[FriendlyChoice, ...], str]:
        if not needs_uart:
            return None, (), "uart_not_configured"
        ports: list[ValidationSerial] = list(inventory.serial_ports)
        if not ports:
            raise ValidationBackendError(
                "validation_blocked",
                "validation/no-uart",
                "The configured serial check requires a visible serial port.",
            )
        selected = next((serial for serial in ports if serial.serial_id == selected_id), None)
        if selected_id is not None and selected is None:
            return ports[0], tuple(serial.choice() for serial in ports), "selection_invalid"
        if selected is not None:
            return selected, (), "explicit_selection"
        cache_reason = "no_cache"
        if self._cache is not None:
            resolution = self._cache.resolve(
                profile.board_id,
                ProbeIdentity(probe.probe_family, probe.usb_serial),
                [serial.endpoint() for serial in ports],
            )
            cache_reason = resolution.reason
            if resolution.reused:
                matches = [serial for serial in ports if serial.port_path == resolution.port_path]
                if len(matches) == 1:
                    return matches[0], (), resolution.reason
        if len(ports) > 1:
            return ports[0], tuple(serial.choice() for serial in ports), cache_reason
        return ports[0], (), cache_reason

    def _confirm_cache(
        self,
        profile: BoardProfile,
        probe: ValidationProbe,
        serial: ValidationSerial | None,
    ) -> None:
        if self._cache is None or serial is None:
            return
        probe_identity = ProbeIdentity(probe.probe_family, probe.usb_serial)
        endpoint = serial.endpoint()
        if probe_identity.is_stable and endpoint.has_stable_identity:
            self._cache.confirm(profile.board_id, probe_identity, endpoint)

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
    """Build a read-only compatibility view for the Stage 0 shared engine.

    The CLI keeps its existing board-config loader while status classification and
    all non-destructive checks use :class:`BoardValidator`.
    """

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
