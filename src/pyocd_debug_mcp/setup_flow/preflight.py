"""Deterministic Setup preflight implementing the Design §3.7 routing table."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from collections.abc import Mapping
from typing import Any, Literal

from pyocd_debug_mcp.firmstore.cache import CacheResolution


NO_INTERNALS_RELAY_INSTRUCTION = (
    "Do not expose structured payloads, continuation tokens, internal field names, "
    "or machine identifiers to the user. Relay only the plain-language question and "
    "friendly choices."
)
_BOARD_ID = re.compile(r"[a-z0-9_]{1,64}")

PreflightStatus = Literal[
    "preflight_ready",
    "setup_needs_user_input",
    "setup_research_required",
    "setup_blocked",
]


class PreflightError(ValueError):
    """Preflight input violates a deterministic setup constraint."""


def _nonempty(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise PreflightError(f"{field_name} must be non-empty")
    return normalized


@dataclass(frozen=True, slots=True)
class SetupUserInput:
    """Only the facts a firmware developer is expected to supply."""

    board_id: str
    connection_id: str
    display_name: str
    mcu_part_number: str
    serial_baudrate: int | None
    datasheet_path: str = ""
    serial_id: str = ""
    requires_uart: bool = True

    def __post_init__(self) -> None:
        if not _BOARD_ID.fullmatch(self.board_id):
            raise PreflightError(
                "board_id must be 1-64 lowercase letters, numbers, or underscores"
            )
        _nonempty(self.connection_id, "connection_id")
        _nonempty(self.display_name, "display_name")
        # Deliberately validate without normalizing or replacing the exact value.
        _nonempty(self.mcu_part_number, "mcu_part_number")
        if not isinstance(self.requires_uart, bool):
            raise PreflightError("requires_uart must be a boolean")
        if self.serial_baudrate is not None and (
            not isinstance(self.serial_baudrate, int)
            or isinstance(self.serial_baudrate, bool)
            or self.serial_baudrate < 1
        ):
            raise PreflightError("serial_baudrate must be a positive integer or null")
        if self.requires_uart and self.serial_baudrate is None:
            raise PreflightError("serial_baudrate is required when requires_uart is true")


@dataclass(frozen=True, slots=True)
class ProbeCandidate:
    probe_id: str
    description: str
    probe_family: str
    usb_serial: str | None = None

    def friendly_label(self) -> str:
        description = _nonempty(self.description, "probe description")
        suffix = self.usb_serial[-6:] if self.usb_serial else "unknown serial"
        return f"{description} (identifier ending {suffix})"


@dataclass(frozen=True, slots=True)
class SerialCandidate:
    serial_id: str
    port_path: str
    description: str
    usb_serial: str | None = None
    vid: int | None = None
    pid: int | None = None
    external_adapter: bool = False
    provably_mapped: bool = True

    def friendly_label(self) -> str:
        description = _nonempty(self.description, "serial description")
        suffix = self.usb_serial[-6:] if self.usb_serial else "unknown serial"
        kind = "external adapter" if self.external_adapter else "board UART"
        return f"{description} ({kind}, identifier ending {suffix})"


@dataclass(frozen=True, slots=True)
class BuildConfiguration:
    configuration_id: str
    description: str
    artifacts: tuple[str, ...] = ()

    def friendly_label(self) -> str:
        return _nonempty(self.description, "build configuration description")


@dataclass(frozen=True, slots=True)
class FriendlyChoice:
    choice_id: str
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class PreflightSelections:
    probe_id: str | None = None
    serial_id: str | None = None
    build_configuration_id: str | None = None
    external_adapter_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class PreflightBlock:
    """Typed server-owned reason setup must stop before hardware discovery."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class PreflightInventory:
    """One complete, already-enumerated view of current setup inputs."""

    probes: tuple[ProbeCandidate, ...] = ()
    serial_ports: tuple[SerialCandidate, ...] = ()
    cache_resolution: CacheResolution = field(
        default_factory=lambda: CacheResolution(False, "no_record")
    )
    built_in_targets: tuple[str, ...] = ()
    manifest_targets: tuple[str, ...] = ()
    exact_detected_targets: tuple[str, ...] = ()
    build_configurations: tuple[BuildConfiguration, ...] = ()
    blocking_error: PreflightBlock | None = None
    hook_contract_call: Mapping[str, Any] | None = None
    """The call that gets a discovery-hook contract, when the server has one to offer.

    Defaults to None so every existing construction site is untouched. `PreflightEngine`
    stays pure and has no server access: it only *renders* what was populated here.
    """

    uart_hook_contract_call: Mapping[str, Any] | None = None

    def normalized(self) -> PreflightInventory:
        """Return a stable-order, duplicate-free inventory for reporting/routing."""

        def unique_text(values: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(sorted({_nonempty(value, "target identifier") for value in values}))

        return PreflightInventory(
            probes=tuple(sorted(self.probes, key=lambda item: item.probe_id)),
            serial_ports=tuple(sorted(self.serial_ports, key=lambda item: item.serial_id)),
            cache_resolution=self.cache_resolution,
            built_in_targets=unique_text(self.built_in_targets),
            manifest_targets=unique_text(self.manifest_targets),
            exact_detected_targets=unique_text(self.exact_detected_targets),
            build_configurations=tuple(
                sorted(self.build_configurations, key=lambda item: item.configuration_id)
            ),
            blocking_error=self.blocking_error,
            hook_contract_call=self.hook_contract_call,
            uart_hook_contract_call=self.uart_hook_contract_call,
        )

    def to_report(self, user_input: SetupUserInput) -> dict[str, Any]:
        normalized = self.normalized()
        return {
            "user_input": {
                "board_id": user_input.board_id,
                "connection_id": user_input.connection_id,
                "display_name": user_input.display_name,
                # Exact preservation is intentional and testable.
                "mcu_part_number": user_input.mcu_part_number,
                "serial_baudrate": user_input.serial_baudrate,
                "serial_id": user_input.serial_id,
                "requires_uart": user_input.requires_uart,
            },
            "probes": [asdict(item) for item in normalized.probes],
            "serial_ports": [asdict(item) for item in normalized.serial_ports],
            "cache": asdict(normalized.cache_resolution),
            "built_in_targets": list(normalized.built_in_targets),
            "manifest_targets": list(normalized.manifest_targets),
            "exact_detected_targets": list(normalized.exact_detected_targets),
            "build_configurations": [
                asdict(item) for item in normalized.build_configurations
            ],
        }


@dataclass(frozen=True, slots=True)
class PreflightDecision:
    status: PreflightStatus
    code: str
    agent_prompt: str
    choices: tuple[FriendlyChoice, ...] = ()
    selected_probe: ProbeCandidate | None = None
    selected_serial: SerialCandidate | None = None
    selected_build: BuildConfiguration | None = None
    selected_target: str | None = None
    cache_confirmation_required: bool = False
    research_required: bool = False
    observed: dict[str, Any] = field(default_factory=dict)

    def to_payload(self, continuation_id: str) -> dict[str, Any]:
        return {
            "status": self.status,
            "continuation_id": continuation_id,
            "agent_prompt": self.agent_prompt,
            "choices": [asdict(choice) for choice in self.choices],
            "observed": self.observed,
        }


class PreflightEngine:
    """Apply every §3.7 row in a fixed, non-guessing order."""

    @staticmethod
    def _prompt(message: str) -> str:
        return f"{message} {NO_INTERNALS_RELAY_INSTRUCTION}"

    @staticmethod
    def _choice_decision(
        code: str,
        message: str,
        choices: tuple[FriendlyChoice, ...],
        **selected: object,
    ) -> PreflightDecision:
        return PreflightDecision(
            "setup_needs_user_input",
            code,
            PreflightEngine._prompt(message),
            choices,
            **selected,  # type: ignore[arg-type]
        )

    @staticmethod
    def _select_by_id(
        candidates: tuple[Any, ...],
        selected_id: str | None,
        id_field: str,
    ) -> Any | None:
        if selected_id is None:
            return None
        return next(
            (candidate for candidate in candidates if getattr(candidate, id_field) == selected_id),
            None,
        )

    def evaluate(
        self,
        user_input: SetupUserInput,
        inventory: PreflightInventory,
        selections: PreflightSelections | None = None,
    ) -> PreflightDecision:
        selections = selections or PreflightSelections()
        current = inventory.normalized()
        observed = current.to_report(user_input)

        if current.blocking_error is not None:
            return PreflightDecision(
                "setup_blocked",
                current.blocking_error.code,
                self._prompt(current.blocking_error.message),
                observed=observed,
            )

        # 1. A missing probe is deterministic and never researchable.
        if not current.probes:
            message = (
                "No compatible debug probe is currently visible. Tell the user to attach "
                "the intended board and retry. If it remains absent, from the BYO Server "
                "checkout run `uv run --locked python -m pyocd list --probes`; this checks "
                "whether the server's locked Python environment can enumerate the USB "
                "debugger. Do not begin documentation research."
            )
            if current.hook_contract_call is not None:
                # Appended, never substituted: the locked-environment check is still the
                # first thing to try, because a driver problem is far likelier than a
                # genuinely unenumerable device, and a hook cannot fix a driver.
                message += (
                    " If that command also reports nothing and the user can still see the "
                    "debugger in a vendor tool, call the tool named by hook_contract_call "
                    "to get the contract for a local discovery hook that names it."
                )
            return PreflightDecision(
                "setup_blocked",
                "setup/no-probe",
                self._prompt(message),
                observed=observed
                | {"hook_contract_call": dict(current.hook_contract_call)}
                if current.hook_contract_call is not None
                else observed,
            )

        selected_probe = self._select_by_id(
            current.probes, selections.probe_id, "probe_id"
        )
        if len(current.probes) > 1 and selected_probe is None:
            choices = tuple(
                FriendlyChoice(item.probe_id, item.friendly_label(), "Connected debug probe")
                for item in current.probes
            )
            code = (
                "setup/probe-selection-invalid"
                if selections.probe_id is not None
                else "setup/probe-selection-required"
            )
            return self._choice_decision(
                code,
                "Ask which connected board the user intends to configure.",
                choices,
                observed=observed,
            )
        if selected_probe is None:
            selected_probe = next(iter(current.probes))

        # 2. UART absence/ambiguity is settled before target research.
        selected_serial: SerialCandidate | None = None
        if user_input.requires_uart:
            if not current.serial_ports:
                uart_message = (
                    "The intended setup requires a serial port, but none is visible. Tell "
                    "the user to attach or enable the board UART and retry; do not research."
                )
                if current.uart_hook_contract_call is not None:
                    uart_message += (
                        " If none appears and the user can still see the port in a vendor "
                        "tool, call the tool named by uart_hook_contract_call to get the "
                        "contract for a local discovery hook that names it."
                    )
                return PreflightDecision(
                    "setup_blocked",
                    "setup/no-uart",
                    self._prompt(uart_message),
                    selected_probe=selected_probe,
                    observed=observed
                    | {"uart_hook_contract_call": dict(current.uart_hook_contract_call)}
                    if current.uart_hook_contract_call is not None
                    else observed,
                )
            selected_serial = self._select_by_id(
                current.serial_ports, selections.serial_id, "serial_id"
            )
            if len(current.serial_ports) > 1 and selected_serial is None:
                if current.cache_resolution.reused:
                    cache_matches = [
                        item
                        for item in current.serial_ports
                        if item.port_path == current.cache_resolution.port_path
                    ]
                    if len(cache_matches) == 1:
                        selected_serial = cache_matches[0]
                if selected_serial is None:
                    choices = tuple(
                        FriendlyChoice(
                            item.serial_id,
                            item.friendly_label(),
                            "Currently visible serial connection",
                        )
                        for item in current.serial_ports
                    )
                    code = (
                        "setup/serial-selection-invalid"
                        if selections.serial_id is not None
                        else "setup/serial-selection-required"
                    )
                    return self._choice_decision(
                        code,
                        "Ask which friendly serial connection belongs to the intended board.",
                        choices,
                        selected_probe=selected_probe,
                        observed=observed,
                    )
            if selected_serial is None:
                selected_serial = next(iter(current.serial_ports))
            if (
                selected_serial.external_adapter
                and not selected_serial.provably_mapped
                and not selections.external_adapter_confirmed
            ):
                return self._choice_decision(
                    "setup/external-adapter-confirmation-required",
                    "Ask the user to confirm that the selected external serial adapter is "
                    "physically connected to the intended board.",
                    (
                        FriendlyChoice(
                            "confirm_external_adapter",
                            f"Confirm {selected_serial.friendly_label()}",
                            "Use this adapter for the intended board and remember the stable match",
                        ),
                    ),
                    selected_probe=selected_probe,
                    selected_serial=selected_serial,
                    observed=observed,
                )

        # 3. Multiple build configurations are never guessed.
        selected_build = self._select_by_id(
            current.build_configurations,
            selections.build_configuration_id,
            "configuration_id",
        )
        if len(current.build_configurations) > 1 and selected_build is None:
            choices = tuple(
                FriendlyChoice(
                    item.configuration_id,
                    item.friendly_label(),
                    "Discovered project build configuration",
                )
                for item in current.build_configurations
            )
            code = (
                "setup/build-selection-invalid"
                if selections.build_configuration_id is not None
                else "setup/build-selection-required"
            )
            return self._choice_decision(
                code,
                "Ask which discovered build configuration is intended for this board.",
                choices,
                selected_probe=selected_probe,
                selected_serial=selected_serial,
                observed=observed,
            )
        if selected_build is None and len(current.build_configurations) == 1:
            selected_build = current.build_configurations[0]

        # 4. Target research is requested only after all local/user ambiguity is settled.
        if len(current.exact_detected_targets) != 1:
            qualifier = "No" if not current.exact_detected_targets else "More than one"
            return PreflightDecision(
                "setup_research_required",
                (
                    "setup/no-exact-target"
                    if not current.exact_detected_targets
                    else "setup/ambiguous-exact-target"
                ),
                self._prompt(
                    f"{qualifier} exact verified device-support binding was detected. Research "
                    "one official CMSIS-Pack for the immutable MCU part; the server will derive "
                    "the target. Do not ask the user for a pack or target identifier."
                ),
                selected_probe=selected_probe,
                selected_serial=selected_serial,
                selected_build=selected_build,
                cache_confirmation_required=bool(
                    selected_serial
                    and selected_serial.external_adapter
                    and not selected_serial.provably_mapped
                ),
                research_required=True,
                observed=observed,
            )

        return PreflightDecision(
            "preflight_ready",
            "setup/preflight-ready",
            self._prompt(
                "Preflight resolved the physical choices and one exact target. Continue the "
                "server-controlled setup phases without asking the user for internal details."
            ),
            selected_probe=selected_probe,
            selected_serial=selected_serial,
            selected_build=selected_build,
            selected_target=current.exact_detected_targets[0],
            cache_confirmation_required=bool(
                selected_serial
                and selected_serial.external_adapter
                and not selected_serial.provably_mapped
            ),
            observed=observed,
        )
