# SPDX-License-Identifier: Apache-2.0
# Derived from Firmware-CLI commit 0e8a45d; modified for BYO-Firmware-MCP.
"""Strict immutable CAN fixture-profile schema and single-read loader."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, TypeVar, cast

from pyocd_debug_mcp.adapters.can_interface import CanIdentifier
from pyocd_debug_mcp.can_errors import CanBackendUnsupportedError, CanConfigError
from pyocd_debug_mcp.can_secure_files import (
    ensure_private_directory,
    read_regular_file_no_symlinks,
    write_immutable_private_file,
)


MAX_PROFILE_BYTES = 64 * 1024
MAX_RECEIVE_IDS = 64
MAX_TRANSMIT_IDS = 32
MAX_BOARD_IDS = 32
MAX_PROBE_UIDS = 32
MAX_READ_SECONDS = 10.0
MAX_READ_FRAMES = 128
MAX_SEND_TIMEOUT_SECONDS = 0.5
MAX_CAN_DATA_BYTES = 8
MAX_DATA_HEX_TEXT_LENGTH = 64
MIN_HEALTH_ARTIFACT_COUNT = 2
MIN_HEALTH_ARTIFACT_BYTES = 12 * 1024
MAX_HEALTH_TIMEOUT_SECONDS = 10.0
MAX_FRAMES_PER_SECOND = 1000
HEALTH_DRAIN_FRAME_CAP = 16

PROCESS_LIMIT_CAPS: dict[str, int] = {
    "max_can_tool_calls": 4096,
    "max_target_policy_refusals": 1024,
    "max_read_calls": 2048,
    "max_health_transactions": 256,
    "max_received_frames": 65536,
    "max_capture_artifacts": 1024,
    "max_capture_bytes": 64 * 1024 * 1024,
}

_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9_]{0,63}")
_BOARD_ID = re.compile(r"[a-z0-9][a-z0-9_]{0,63}")
_CHANNEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_HEX = re.compile(r"[0-9a-fA-F]*")
_SUPPORTED_ADAPTERS = frozenset({"virtual", "tcp_sim", "socketcan_vcan", "socketcan"})

_T = TypeVar("_T")


@dataclass(frozen=True)
class CmsisPackBinding:
    vendor: str
    name: str
    version: str
    sha256: str


@dataclass(frozen=True)
class DebugAttachmentConfig:
    pyocd_version: str
    cmsis_pack: CmsisPackBinding
    pack_debug_sequences: str


@dataclass(frozen=True)
class CanAdapterIdentity:
    driver: str
    usb_vid: int
    usb_pid: int
    serial: str


@dataclass(frozen=True)
class CanAdapterConfig:
    kind: str
    channel: str
    identity: CanAdapterIdentity | None

    @property
    def physical(self) -> bool:
        return self.kind == "socketcan"


@dataclass(frozen=True)
class CanBusPolicy:
    expected_nominal_bitrate: int
    require_one_shot_for_tx: bool
    receive_own_messages: bool


@dataclass(frozen=True)
class CanReceivePolicy:
    exact_ids: tuple[CanIdentifier, ...]
    default_read_seconds: float
    max_read_seconds: float
    default_max_frames: int
    max_frames: int


@dataclass(frozen=True)
class CanTransmitPolicy:
    enabled: bool
    exact_ids: tuple[CanIdentifier, ...]
    max_frames_per_second: int
    max_tx_attempts_per_process: int
    default_send_timeout_seconds: float
    max_send_timeout_seconds: float


@dataclass(frozen=True)
class CanProcessLimits:
    max_can_tool_calls: int
    max_target_policy_refusals: int
    max_read_calls: int
    max_health_transactions: int
    max_received_frames: int
    max_capture_artifacts: int
    max_capture_bytes: int


@dataclass(frozen=True)
class CanEvidencePolicy:
    provider_payloads: str
    artifact_payloads: str


@dataclass(frozen=True)
class CanPayloadPattern:
    prefix: bytes
    challenge_bytes: int
    suffix: bytes

    @property
    def dlc(self) -> int:
        return len(self.prefix) + self.challenge_bytes + len(self.suffix)


@dataclass(frozen=True)
class CanHealthRequest:
    identifier: CanIdentifier
    payload: CanPayloadPattern


@dataclass(frozen=True)
class CanHealthResponse:
    identifier: CanIdentifier
    prefix: bytes
    echo_challenge: bool
    suffix: bytes

    @property
    def dlc(self) -> int:
        echoed_bytes = 4 if self.echo_challenge else 0
        return len(self.prefix) + echoed_bytes + len(self.suffix)


@dataclass(frozen=True)
class CanHealthCheck:
    request: CanHealthRequest
    expected_response: CanHealthResponse
    timeout_seconds: float


@dataclass(frozen=True)
class CanFixtureConfig:
    schema_version: int
    profile_id: str
    board_ids: tuple[str, ...]
    probe_uids: tuple[str, ...]
    protocol: str
    debug_attachment: DebugAttachmentConfig | None
    adapter: CanAdapterConfig
    bus: CanBusPolicy
    receive: CanReceivePolicy
    transmit: CanTransmitPolicy
    process_limits: CanProcessLimits
    evidence: CanEvidencePolicy
    health_check: CanHealthCheck | None


@dataclass(frozen=True)
class CanFixtureSummary:
    profile_id: str
    profile_sha256: str
    board_ids: tuple[str, ...]
    adapter_kind: str
    adapter_channel: str
    protocol: str
    expected_nominal_bitrate: int
    require_one_shot_for_tx: bool
    receive_own_messages: bool
    receive_ids: tuple[str, ...]
    default_read_seconds: float
    max_read_seconds: float
    default_max_frames: int
    max_read_frames: int
    transmit_enabled: bool
    transmit_ids: tuple[str, ...]
    max_frames_per_second: int
    max_tx_attempts_per_process: int
    default_send_timeout_seconds: float
    max_send_timeout_seconds: float
    process_limits: CanProcessLimits
    provider_payloads: str
    artifact_payloads: str
    health_check_configured: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class LoadedCanFixtureConfig:
    config: CanFixtureConfig
    summary: CanFixtureSummary
    source_path: Path
    source_sha256: str
    canonical_sha256: str
    snapshot_path: Path | None


def _error(path: str, message: str) -> CanConfigError:
    return CanConfigError(f"{path}: {message}")


def _mapping(
    value: object,
    path: str,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _error(path, "must be an object")
    if any(not isinstance(key, str) for key in value):
        raise _error(path, "field names must be strings")
    unknown = sorted(set(value) - required - optional)
    if unknown:
        raise _error(path, f"unknown field(s): {', '.join(unknown)}")
    missing = sorted(required - set(value))
    if missing:
        raise _error(path, f"missing required field(s): {', '.join(missing)}")
    return value


def _string(
    value: object,
    path: str,
    *,
    maximum: int = 256,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str):
        raise _error(path, "must be a string")
    if value != value.strip() or not value:
        raise _error(path, "must be non-empty with no surrounding whitespace")
    if len(value) > maximum:
        raise _error(path, f"must contain at most {maximum} characters")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise _error(path, "contains unsupported characters")
    return value


def _boolean(value: object, path: str) -> bool:
    if type(value) is not bool:
        raise _error(path, "must be a boolean")
    return value


def _positive_int(value: object, path: str, *, maximum: int) -> int:
    if type(value) is not int:
        raise _error(path, "must be a positive integer (booleans and strings are rejected)")
    if not 1 <= value <= maximum:
        raise _error(path, f"must be between 1 and {maximum}")
    return value


def _bounded_nonnegative_int(value: object, path: str, *, maximum: int) -> int:
    if type(value) is not int:
        raise _error(path, "must be an integer (booleans and strings are rejected)")
    if not 0 <= value <= maximum:
        raise _error(path, f"must be between 0 and {maximum}")
    return value


def _positive_float(value: object, path: str, *, maximum: float) -> float:
    if type(value) not in {int, float}:
        raise _error(path, "must be a finite positive number (booleans and strings are rejected)")
    try:
        result = float(cast("int | float", value))
    except OverflowError as exc:
        raise _error(path, "must be a finite positive number") from exc
    if not math.isfinite(result) or not 0 < result <= maximum:
        raise _error(path, f"must be finite, greater than 0, and at most {maximum}")
    return result


def _strict_list(
    value: object,
    path: str,
    *,
    maximum: int,
    parser: Callable[[object, str], _T],
    nonempty: bool = False,
) -> tuple[_T, ...]:
    if not isinstance(value, list):
        raise _error(path, "must be a list")
    if nonempty and not value:
        raise _error(path, "must not be empty")
    if len(value) > maximum:
        raise _error(path, f"must contain at most {maximum} entries")
    parsed = tuple(parser(item, f"{path}[{index}]") for index, item in enumerate(value))
    if len(set(parsed)) != len(parsed):
        raise _error(path, "must not contain duplicate entries")
    return parsed


def _parse_id(value: object, path: str) -> CanIdentifier:
    raw = _mapping(
        value,
        path,
        required=frozenset({"arbitration_id", "extended"}),
    )
    extended = _boolean(raw["extended"], f"{path}.extended")
    maximum = 0x1FFFFFFF if extended else 0x7FF
    arbitration_id = _bounded_nonnegative_int(
        raw["arbitration_id"], f"{path}.arbitration_id", maximum=maximum
    )
    return CanIdentifier(arbitration_id, extended)


def _hex_bytes(value: object, path: str) -> bytes:
    if not isinstance(value, str):
        raise _error(path, "must be a hexadecimal string")
    compact = re.sub(r"\s+", "", value)
    if len(compact) % 2 != 0 or _HEX.fullmatch(compact) is None:
        raise _error(path, "must contain an even number of hexadecimal digits")
    return bytes.fromhex(compact)


def _parse_debug_attachment(value: object, path: str) -> DebugAttachmentConfig:
    raw = _mapping(
        value,
        path,
        required=frozenset({"pyocd_version", "cmsis_pack", "pack_debug_sequences"}),
    )
    pack_raw = _mapping(
        raw["cmsis_pack"],
        f"{path}.cmsis_pack",
        required=frozenset({"vendor", "name", "version", "sha256"}),
    )
    pack_sha256 = _string(pack_raw["sha256"], f"{path}.cmsis_pack.sha256", maximum=64)
    if _SHA256.fullmatch(pack_sha256) is None:
        raise _error(f"{path}.cmsis_pack.sha256", "must be 64 lowercase hexadecimal digits")
    debug_sequences = _string(
        raw["pack_debug_sequences"], f"{path}.pack_debug_sequences", maximum=64
    )
    if debug_sequences != "disabled":
        raise _error(
            f"{path}.pack_debug_sequences",
            "must be 'disabled' until an exact debug-sequence manifest is qualified",
        )
    return DebugAttachmentConfig(
        pyocd_version=_string(raw["pyocd_version"], f"{path}.pyocd_version", maximum=64),
        cmsis_pack=CmsisPackBinding(
            vendor=_string(pack_raw["vendor"], f"{path}.cmsis_pack.vendor"),
            name=_string(pack_raw["name"], f"{path}.cmsis_pack.name"),
            version=_string(pack_raw["version"], f"{path}.cmsis_pack.version", maximum=64),
            sha256=pack_sha256,
        ),
        pack_debug_sequences=debug_sequences,
    )


def _parse_adapter(value: object, path: str) -> CanAdapterConfig:
    if not isinstance(value, dict):
        raise _error(path, "must be an object")
    kind = _string(value.get("kind"), f"{path}.kind", maximum=32)
    if kind not in _SUPPORTED_ADAPTERS:
        raise CanBackendUnsupportedError(
            f"{path}.kind must be one of: {', '.join(sorted(_SUPPORTED_ADAPTERS))}"
        )
    if kind == "socketcan":
        raw = _mapping(
            value,
            path,
            required=frozenset({"kind", "channel", "identity"}),
        )
        identity_raw = _mapping(
            raw["identity"],
            f"{path}.identity",
            required=frozenset({"driver", "usb_vid", "usb_pid", "serial"}),
        )
        identity = CanAdapterIdentity(
            driver=_string(identity_raw["driver"], f"{path}.identity.driver", maximum=64),
            usb_vid=_bounded_nonnegative_int(
                identity_raw["usb_vid"], f"{path}.identity.usb_vid", maximum=0xFFFF
            ),
            usb_pid=_bounded_nonnegative_int(
                identity_raw["usb_pid"], f"{path}.identity.usb_pid", maximum=0xFFFF
            ),
            serial=_string(identity_raw["serial"], f"{path}.identity.serial", maximum=128),
        )
    else:
        raw = _mapping(value, path, required=frozenset({"kind", "channel"}))
        identity = None
    if kind == "tcp_sim":
        from pyocd_debug_mcp.adapters.can_sim import endpoint

        try:
            endpoint(str(raw["channel"]))
        except ValueError as exc:
            raise _error(f"{path}.channel", str(exc)) from exc
    return CanAdapterConfig(
        kind=kind,
        channel=_string(raw["channel"], f"{path}.channel", pattern=_CHANNEL),
        identity=identity,
    )


def _parse_payload_pattern(value: object, path: str) -> CanPayloadPattern:
    raw = _mapping(
        value,
        path,
        required=frozenset({"prefix_hex", "challenge_bytes", "suffix_hex"}),
    )
    prefix = _hex_bytes(raw["prefix_hex"], f"{path}.prefix_hex")
    challenge_bytes = _positive_int(raw["challenge_bytes"], f"{path}.challenge_bytes", maximum=8)
    if challenge_bytes != 4:
        raise _error(f"{path}.challenge_bytes", "must equal 4 in schema v1")
    suffix = _hex_bytes(raw["suffix_hex"], f"{path}.suffix_hex")
    result = CanPayloadPattern(prefix=prefix, challenge_bytes=challenge_bytes, suffix=suffix)
    if result.dlc > 8:
        raise _error(path, "prefix + challenge + suffix exceeds 8 bytes")
    return result


def _parse_health(value: object, path: str) -> CanHealthCheck:
    raw = _mapping(
        value,
        path,
        required=frozenset({"request", "expected_response", "timeout_seconds"}),
    )
    request_raw = _mapping(
        raw["request"],
        f"{path}.request",
        required=frozenset({"arbitration_id", "extended", "payload"}),
    )
    response_raw = _mapping(
        raw["expected_response"],
        f"{path}.expected_response",
        required=frozenset({"arbitration_id", "extended", "payload"}),
    )
    response_payload = _mapping(
        response_raw["payload"],
        f"{path}.expected_response.payload",
        required=frozenset({"prefix_hex", "echo_challenge", "suffix_hex"}),
    )
    response = CanHealthResponse(
        identifier=_parse_id(
            {
                "arbitration_id": response_raw["arbitration_id"],
                "extended": response_raw["extended"],
            },
            f"{path}.expected_response",
        ),
        prefix=_hex_bytes(
            response_payload["prefix_hex"], f"{path}.expected_response.payload.prefix_hex"
        ),
        echo_challenge=_boolean(
            response_payload["echo_challenge"],
            f"{path}.expected_response.payload.echo_challenge",
        ),
        suffix=_hex_bytes(
            response_payload["suffix_hex"], f"{path}.expected_response.payload.suffix_hex"
        ),
    )
    if response.dlc > 8:
        raise _error(f"{path}.expected_response.payload", "response payload exceeds 8 bytes")
    return CanHealthCheck(
        request=CanHealthRequest(
            identifier=_parse_id(
                {
                    "arbitration_id": request_raw["arbitration_id"],
                    "extended": request_raw["extended"],
                },
                f"{path}.request",
            ),
            payload=_parse_payload_pattern(request_raw["payload"], f"{path}.request.payload"),
        ),
        expected_response=response,
        timeout_seconds=_positive_float(
            raw["timeout_seconds"],
            f"{path}.timeout_seconds",
            maximum=MAX_HEALTH_TIMEOUT_SECONDS,
        ),
    )


def _patterns_are_statically_distinguishable(health: CanHealthCheck) -> bool:
    if health.request.identifier != health.expected_response.identifier:
        return True
    request = (
        tuple(health.request.payload.prefix)
        + (None,) * health.request.payload.challenge_bytes
        + tuple(health.request.payload.suffix)
    )
    response = (
        tuple(health.expected_response.prefix)
        + (None,) * (4 if health.expected_response.echo_challenge else 0)
        + tuple(health.expected_response.suffix)
    )
    if len(request) != len(response):
        return True
    return any(
        left is not None and right is not None and left != right
        for left, right in zip(request, response, strict=True)
    )


def make_can_fixture_config(raw_value: object) -> CanFixtureConfig:
    raw = _mapping(
        raw_value,
        "profile",
        required=frozenset(
            {
                "schema_version",
                "profile_id",
                "board_ids",
                "probe_uids",
                "protocol",
                "adapter",
                "bus",
                "receive",
                "transmit",
                "process_limits",
                "evidence",
            }
        ),
        optional=frozenset({"debug_attachment", "health_check"}),
    )
    schema_version = _positive_int(raw["schema_version"], "profile.schema_version", maximum=1)
    if schema_version != 1:
        raise _error("profile.schema_version", "must equal 1")
    profile_id = _string(raw["profile_id"], "profile.profile_id", pattern=_PROFILE_ID)
    board_ids = _strict_list(
        raw["board_ids"],
        "profile.board_ids",
        maximum=MAX_BOARD_IDS,
        parser=lambda item, path: _string(item, path, pattern=_BOARD_ID),
        nonempty=True,
    )
    probe_uids = _strict_list(
        raw["probe_uids"],
        "profile.probe_uids",
        maximum=MAX_PROBE_UIDS,
        parser=lambda item, path: _string(item, path, maximum=128),
    )
    protocol = _string(raw["protocol"], "profile.protocol", maximum=16)
    if protocol != "classic":
        raise _error("profile.protocol", "must equal 'classic' in schema v1")
    adapter = _parse_adapter(raw["adapter"], "profile.adapter")

    bus_raw = _mapping(
        raw["bus"],
        "profile.bus",
        required=frozenset(
            {"expected_nominal_bitrate", "require_one_shot_for_tx", "receive_own_messages"}
        ),
    )
    bus = CanBusPolicy(
        expected_nominal_bitrate=_positive_int(
            bus_raw["expected_nominal_bitrate"],
            "profile.bus.expected_nominal_bitrate",
            maximum=8_000_000,
        ),
        require_one_shot_for_tx=_boolean(
            bus_raw["require_one_shot_for_tx"], "profile.bus.require_one_shot_for_tx"
        ),
        receive_own_messages=_boolean(
            bus_raw["receive_own_messages"], "profile.bus.receive_own_messages"
        ),
    )

    receive_raw = _mapping(
        raw["receive"],
        "profile.receive",
        required=frozenset(
            {
                "exact_ids",
                "default_read_seconds",
                "max_read_seconds",
                "default_max_frames",
                "max_frames",
            }
        ),
    )
    max_read_seconds = _positive_float(
        receive_raw["max_read_seconds"],
        "profile.receive.max_read_seconds",
        maximum=MAX_READ_SECONDS,
    )
    default_read_seconds = _positive_float(
        receive_raw["default_read_seconds"],
        "profile.receive.default_read_seconds",
        maximum=MAX_READ_SECONDS,
    )
    if default_read_seconds > max_read_seconds:
        raise _error("profile.receive.default_read_seconds", "must not exceed max_read_seconds")
    max_frames = _positive_int(
        receive_raw["max_frames"], "profile.receive.max_frames", maximum=MAX_READ_FRAMES
    )
    default_max_frames = _positive_int(
        receive_raw["default_max_frames"],
        "profile.receive.default_max_frames",
        maximum=MAX_READ_FRAMES,
    )
    if default_max_frames > max_frames:
        raise _error("profile.receive.default_max_frames", "must not exceed max_frames")
    receive = CanReceivePolicy(
        exact_ids=_strict_list(
            receive_raw["exact_ids"],
            "profile.receive.exact_ids",
            maximum=MAX_RECEIVE_IDS,
            parser=_parse_id,
            nonempty=True,
        ),
        default_read_seconds=default_read_seconds,
        max_read_seconds=max_read_seconds,
        default_max_frames=default_max_frames,
        max_frames=max_frames,
    )

    transmit_raw = _mapping(
        raw["transmit"],
        "profile.transmit",
        required=frozenset(
            {
                "exact_ids",
                "max_frames_per_second",
                "max_tx_attempts_per_process",
                "default_send_timeout_seconds",
                "max_send_timeout_seconds",
            }
        ),
        optional=frozenset({"enabled"}),
    )
    transmit_enabled = _boolean(transmit_raw.get("enabled", False), "profile.transmit.enabled")
    transmit_ids = _strict_list(
        transmit_raw["exact_ids"],
        "profile.transmit.exact_ids",
        maximum=MAX_TRANSMIT_IDS,
        parser=_parse_id,
    )
    if transmit_enabled and not transmit_ids:
        raise _error("profile.transmit.exact_ids", "must not be empty when transmit is enabled")
    max_send_timeout = _positive_float(
        transmit_raw["max_send_timeout_seconds"],
        "profile.transmit.max_send_timeout_seconds",
        maximum=MAX_SEND_TIMEOUT_SECONDS,
    )
    default_send_timeout = _positive_float(
        transmit_raw["default_send_timeout_seconds"],
        "profile.transmit.default_send_timeout_seconds",
        maximum=MAX_SEND_TIMEOUT_SECONDS,
    )
    if default_send_timeout > max_send_timeout:
        raise _error(
            "profile.transmit.default_send_timeout_seconds",
            "must not exceed max_send_timeout_seconds",
        )
    transmit = CanTransmitPolicy(
        enabled=transmit_enabled,
        exact_ids=transmit_ids,
        max_frames_per_second=_positive_int(
            transmit_raw["max_frames_per_second"],
            "profile.transmit.max_frames_per_second",
            maximum=MAX_FRAMES_PER_SECOND,
        ),
        max_tx_attempts_per_process=_positive_int(
            transmit_raw["max_tx_attempts_per_process"],
            "profile.transmit.max_tx_attempts_per_process",
            maximum=PROCESS_LIMIT_CAPS["max_can_tool_calls"],
        ),
        default_send_timeout_seconds=default_send_timeout,
        max_send_timeout_seconds=max_send_timeout,
    )

    limits_raw = _mapping(
        raw["process_limits"],
        "profile.process_limits",
        required=frozenset(PROCESS_LIMIT_CAPS),
    )
    limit_values = {
        field: _positive_int(limits_raw[field], f"profile.process_limits.{field}", maximum=maximum)
        for field, maximum in PROCESS_LIMIT_CAPS.items()
    }
    process_limits = CanProcessLimits(**limit_values)
    if transmit.max_tx_attempts_per_process > process_limits.max_can_tool_calls:
        raise _error(
            "profile.transmit.max_tx_attempts_per_process",
            "must not exceed process_limits.max_can_tool_calls",
        )
    if process_limits.max_health_transactions > process_limits.max_read_calls:
        raise _error(
            "profile.process_limits.max_health_transactions",
            "must not exceed max_read_calls",
        )
    if process_limits.max_read_calls > process_limits.max_can_tool_calls:
        raise _error("profile.process_limits.max_read_calls", "must not exceed max_can_tool_calls")

    evidence_raw = _mapping(
        raw["evidence"],
        "profile.evidence",
        required=frozenset({"provider_payloads", "artifact_payloads"}),
    )
    provider_payloads = _string(
        evidence_raw["provider_payloads"], "profile.evidence.provider_payloads", maximum=32
    )
    if provider_payloads not in {"metadata_only", "full"}:
        raise _error("profile.evidence.provider_payloads", "must be 'metadata_only' or 'full'")
    artifact_payloads = _string(
        evidence_raw["artifact_payloads"], "profile.evidence.artifact_payloads", maximum=32
    )
    if artifact_payloads not in {"full", "hash_only"}:
        raise _error("profile.evidence.artifact_payloads", "must be 'full' or 'hash_only'")
    evidence = CanEvidencePolicy(provider_payloads, artifact_payloads)

    debug_attachment = None
    if "debug_attachment" in raw:
        debug_attachment = _parse_debug_attachment(
            raw["debug_attachment"], "profile.debug_attachment"
        )
    if adapter.physical:
        if not probe_uids:
            raise _error("profile.probe_uids", "must not be empty for a physical profile")
        if debug_attachment is None:
            raise _error("profile.debug_attachment", "is required for a physical socketcan profile")
        if transmit.enabled and not bus.require_one_shot_for_tx:
            raise _error(
                "profile.bus.require_one_shot_for_tx",
                "must be true when physical transmit is enabled",
            )
    elif debug_attachment is not None:
        raise _error("profile.debug_attachment", "is allowed only for a physical socketcan profile")

    health = (
        _parse_health(raw["health_check"], "profile.health_check")
        if "health_check" in raw
        else None
    )
    if health is not None:
        minimum_health_rx_capacity = HEALTH_DRAIN_FRAME_CAP + receive.max_frames
        if process_limits.max_received_frames < minimum_health_rx_capacity:
            raise _error(
                "profile.process_limits.max_received_frames",
                "must be at least "
                f"{minimum_health_rx_capacity} to fit one bounded health transaction",
            )
        if process_limits.max_capture_artifacts < MIN_HEALTH_ARTIFACT_COUNT:
            raise _error(
                "profile.process_limits.max_capture_artifacts",
                "must reserve at least two artifacts for health evidence and terminal proof",
            )
        if process_limits.max_capture_bytes < MIN_HEALTH_ARTIFACT_BYTES:
            raise _error(
                "profile.process_limits.max_capture_bytes",
                f"must be at least {MIN_HEALTH_ARTIFACT_BYTES} when health_check is configured",
            )
        if bus.receive_own_messages:
            raise _error(
                "profile.bus.receive_own_messages",
                "must be false when health_check is configured",
            )
        if health.request.identifier not in transmit.exact_ids:
            raise _error(
                "profile.health_check.request",
                "identifier must be present in transmit.exact_ids",
            )
        if health.expected_response.identifier not in receive.exact_ids:
            raise _error(
                "profile.health_check.expected_response",
                "identifier must be present in receive.exact_ids",
            )
        if not _patterns_are_statically_distinguishable(health):
            raise _error(
                "profile.health_check",
                "request and response predicates must be statically distinguishable",
            )

    return CanFixtureConfig(
        schema_version=schema_version,
        profile_id=profile_id,
        board_ids=board_ids,
        probe_uids=probe_uids,
        protocol=protocol,
        debug_attachment=debug_attachment,
        adapter=adapter,
        bus=bus,
        receive=receive,
        transmit=transmit,
        process_limits=process_limits,
        evidence=evidence,
        health_check=health,
    )


def _identifier_dict(identifier: CanIdentifier) -> dict[str, object]:
    return {"arbitration_id": identifier.arbitration_id, "extended": identifier.extended}


def _canonical_document(config: CanFixtureConfig) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": config.schema_version,
        "profile_id": config.profile_id,
        "board_ids": list(config.board_ids),
        "probe_uids": list(config.probe_uids),
        "protocol": config.protocol,
        "adapter": {
            "kind": config.adapter.kind,
            "channel": config.adapter.channel,
        },
        "bus": asdict(config.bus),
        "receive": {
            "exact_ids": [_identifier_dict(item) for item in config.receive.exact_ids],
            "default_read_seconds": config.receive.default_read_seconds,
            "max_read_seconds": config.receive.max_read_seconds,
            "default_max_frames": config.receive.default_max_frames,
            "max_frames": config.receive.max_frames,
        },
        "transmit": {
            "enabled": config.transmit.enabled,
            "exact_ids": [_identifier_dict(item) for item in config.transmit.exact_ids],
            "max_frames_per_second": config.transmit.max_frames_per_second,
            "max_tx_attempts_per_process": config.transmit.max_tx_attempts_per_process,
            "default_send_timeout_seconds": config.transmit.default_send_timeout_seconds,
            "max_send_timeout_seconds": config.transmit.max_send_timeout_seconds,
        },
        "process_limits": asdict(config.process_limits),
        "evidence": asdict(config.evidence),
    }
    adapter_doc = document["adapter"]
    if config.adapter.identity is not None and isinstance(adapter_doc, dict):
        adapter_doc["identity"] = asdict(config.adapter.identity)
    if config.debug_attachment is not None:
        document["debug_attachment"] = asdict(config.debug_attachment)
    if config.health_check is not None:
        health = config.health_check
        document["health_check"] = {
            "request": {
                **_identifier_dict(health.request.identifier),
                "payload": {
                    "prefix_hex": health.request.payload.prefix.hex(),
                    "challenge_bytes": health.request.payload.challenge_bytes,
                    "suffix_hex": health.request.payload.suffix.hex(),
                },
            },
            "expected_response": {
                **_identifier_dict(health.expected_response.identifier),
                "payload": {
                    "prefix_hex": health.expected_response.prefix.hex(),
                    "echo_challenge": health.expected_response.echo_challenge,
                    "suffix_hex": health.expected_response.suffix.hex(),
                },
            },
            "timeout_seconds": health.timeout_seconds,
        }
    return document


def _canonical_bytes(config: CanFixtureConfig) -> bytes:
    return json.dumps(
        _canonical_document(config), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _parse_json(data: bytes, source_path: Path) -> object:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value!r} is forbidden")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        output: dict[str, object] = {}
        for key, value in pairs:
            if key in output:
                raise ValueError(f"duplicate key {key!r}")
            output[key] = value
        return output

    try:
        return json.loads(
            data.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise CanConfigError(f"{source_path}: invalid JSON: {exc}") from exc


def _parse_yaml(data: bytes, source_path: Path) -> object:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise CanConfigError("PyYAML is required to load a CAN fixture profile") from exc

    class UniqueKeySafeLoader(yaml.SafeLoader):  # type: ignore[misc]
        pass

    def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[object, object]:
        output: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                hash(key)
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "mapping key must be a scalar hashable value",
                    key_node.start_mark,
                ) from exc
            if key in output:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate key {key!r}",
                    key_node.start_mark,
                )
            output[key] = loader.construct_object(value_node, deep=deep)
        return output

    UniqueKeySafeLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
    )
    try:
        return yaml.load(data.decode("utf-8"), Loader=UniqueKeySafeLoader)
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError, TypeError, ValueError) as exc:
        raise CanConfigError(f"{source_path}: invalid YAML: {exc}") from exc


def _read_profile_once(path: Path) -> tuple[Path, bytes]:
    source_path = Path(os.path.abspath(path))
    try:
        data = read_regular_file_no_symlinks(source_path, maximum_bytes=MAX_PROFILE_BYTES)
    except OSError as exc:
        raise CanConfigError(f"Cannot read CAN profile {source_path}: {exc}") from exc
    return source_path, data


def _write_private_snapshot(snapshot_dir: Path, profile_id: str, data: bytes, digest: str) -> Path:
    snapshot_dir = Path(os.path.abspath(snapshot_dir))
    try:
        ensure_private_directory(snapshot_dir, parents=True)
    except OSError as exc:
        raise CanConfigError(f"Cannot prepare private CAN snapshot directory: {exc}") from exc
    snapshot_path = snapshot_dir / f"{profile_id}-{digest}.json"
    existed = os.path.lexists(snapshot_path)
    try:
        # ``multiprocessing`` spawn re-imports the parent's main module on
        # Windows/macOS. Reuse is allowed only after the platform seam proves
        # the content-addressed file private, unlinked, stable, and identical.
        write_immutable_private_file(snapshot_path, data, maximum_bytes=MAX_PROFILE_BYTES)
    except OSError as exc:
        action = "reuse" if existed else "create"
        raise CanConfigError(f"Cannot {action} immutable CAN profile snapshot: {exc}") from exc
    return snapshot_path


def load_can_fixture_config(
    path: Path | str,
    *,
    expected_sha256: str | None = None,
    snapshot_dir: Path | None = None,
) -> LoadedCanFixtureConfig:
    source_path, source_bytes = _read_profile_once(Path(path))
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if expected_sha256 is not None:
        if _SHA256.fullmatch(expected_sha256) is None:
            raise CanConfigError("Expected CAN profile SHA-256 must be 64 lowercase hex digits")
        if not hmac.compare_digest(source_sha256, expected_sha256):
            raise CanConfigError(
                f"CAN profile SHA-256 mismatch: expected {expected_sha256}, got {source_sha256}"
            )
    suffix = source_path.suffix.lower()
    if suffix == ".json":
        raw = _parse_json(source_bytes, source_path)
    elif suffix in {".yaml", ".yml"}:
        raw = _parse_yaml(source_bytes, source_path)
    else:
        raise CanConfigError("CAN profile must use .json, .yaml, or .yml")
    config = make_can_fixture_config(raw)
    if config.adapter.kind in {"socketcan", "socketcan_vcan"} and not sys.platform.startswith(
        "linux"
    ):
        raise CanConfigError("SocketCAN profiles are available only on Linux")
    canonical = _canonical_bytes(config)
    canonical_sha256 = hashlib.sha256(canonical).hexdigest()
    snapshot_path = None
    if snapshot_dir is not None:
        snapshot_path = _write_private_snapshot(
            snapshot_dir, config.profile_id, canonical, canonical_sha256
        )
    summary = CanFixtureSummary(
        profile_id=config.profile_id,
        profile_sha256=canonical_sha256,
        board_ids=config.board_ids,
        adapter_kind=config.adapter.kind,
        adapter_channel=config.adapter.channel,
        protocol=config.protocol,
        expected_nominal_bitrate=config.bus.expected_nominal_bitrate,
        require_one_shot_for_tx=config.bus.require_one_shot_for_tx,
        receive_own_messages=config.bus.receive_own_messages,
        receive_ids=tuple(item.display() for item in config.receive.exact_ids),
        default_read_seconds=config.receive.default_read_seconds,
        max_read_seconds=config.receive.max_read_seconds,
        default_max_frames=config.receive.default_max_frames,
        max_read_frames=config.receive.max_frames,
        transmit_enabled=config.transmit.enabled,
        transmit_ids=tuple(item.display() for item in config.transmit.exact_ids),
        max_frames_per_second=config.transmit.max_frames_per_second,
        max_tx_attempts_per_process=config.transmit.max_tx_attempts_per_process,
        default_send_timeout_seconds=config.transmit.default_send_timeout_seconds,
        max_send_timeout_seconds=config.transmit.max_send_timeout_seconds,
        process_limits=config.process_limits,
        provider_payloads=config.evidence.provider_payloads,
        artifact_payloads=config.evidence.artifact_payloads,
        health_check_configured=config.health_check is not None,
    )
    return LoadedCanFixtureConfig(
        config=config,
        summary=summary,
        source_path=source_path,
        source_sha256=source_sha256,
        canonical_sha256=canonical_sha256,
        snapshot_path=snapshot_path,
    )


def snapshot_loaded_can_fixture(
    loaded: LoadedCanFixtureConfig,
    snapshot_dir: Path,
) -> LoadedCanFixtureConfig:
    """Persist one already-validated canonical profile without rereading its source."""

    canonical = _canonical_bytes(loaded.config)
    digest = hashlib.sha256(canonical).hexdigest()
    if not hmac.compare_digest(digest, loaded.canonical_sha256):
        raise CanConfigError("Loaded CAN profile canonical hash changed before snapshot")
    snapshot_path = _write_private_snapshot(
        snapshot_dir,
        loaded.config.profile_id,
        canonical,
        digest,
    )
    return replace(loaded, snapshot_path=snapshot_path)
