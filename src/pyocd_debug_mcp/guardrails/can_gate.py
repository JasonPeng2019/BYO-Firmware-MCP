# SPDX-License-Identifier: Apache-2.0
# Derived from Firmware-CLI commit 0e8a45d; modified for BYO-Firmware-MCP.
"""Pure CAN argument and immutable-policy guardrails."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import cast

from pyocd_debug_mcp.adapters.can_interface import CanIdentifier
from pyocd_debug_mcp.can_config import (
    MAX_CAN_DATA_BYTES,
    MAX_DATA_HEX_TEXT_LENGTH,
    CanFixtureConfig,
)
from pyocd_debug_mcp.can_errors import CanPolicyError


_HEX = re.compile(r"[0-9a-fA-F]*")


@dataclass(frozen=True)
class ValidatedCanTransmit:
    """Statically validated arguments, not a live-backend submission permit."""

    identifier: CanIdentifier
    data: bytes
    timeout_seconds: float


@dataclass(frozen=True)
class ValidatedCanRead:
    """Statically validated read bounds, before process-budget reservation."""

    read_seconds: float
    max_frames: int


def _invalid(message: str) -> CanPolicyError:
    return CanPolicyError("can/invalid-argument", message)


def _strict_positive_float(value: object, field_name: str, maximum: float) -> float:
    if type(value) not in {int, float}:
        raise _invalid(f"{field_name} must be a finite positive JSON number")
    try:
        result = float(cast("int | float", value))
    except OverflowError as exc:
        raise _invalid(f"{field_name} must be a finite positive JSON number") from exc
    if not math.isfinite(result) or not 0 < result <= maximum:
        raise _invalid(f"{field_name} must be greater than 0 and at most {maximum}")
    return result


def _strict_positive_int(value: object, field_name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise _invalid(f"{field_name} must be an integer between 1 and {maximum}")
    return value


def _identifier(arbitration_id: object, extended: object) -> CanIdentifier:
    try:
        return CanIdentifier(arbitration_id, extended)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise _invalid(str(exc)) from exc


def _payload(data_hex: object) -> bytes:
    if not isinstance(data_hex, str):
        raise _invalid("data_hex must be a hexadecimal string")
    if len(data_hex) > MAX_DATA_HEX_TEXT_LENGTH:
        raise _invalid(
            f"data_hex textual representation must contain at most "
            f"{MAX_DATA_HEX_TEXT_LENGTH} characters"
        )
    compact = re.sub(r"\s+", "", data_hex)
    if len(compact) % 2 != 0 or _HEX.fullmatch(compact) is None:
        raise _invalid("data_hex must contain an even number of hexadecimal digits")
    data = bytes.fromhex(compact)
    if len(data) > MAX_CAN_DATA_BYTES:
        raise _invalid(f"data_hex must encode at most {MAX_CAN_DATA_BYTES} bytes for Classical CAN")
    return data


class CanGate:
    """Validates requests against one frozen ``CanFixtureConfig``."""

    def __init__(self, config: CanFixtureConfig) -> None:
        self._config = config

    @property
    def config(self) -> CanFixtureConfig:
        return self._config

    def require_board(self, board_id: object) -> None:
        if not isinstance(board_id, str) or board_id not in self._config.board_ids:
            raise CanPolicyError(
                "can/board-mismatch",
                f"Active board {board_id!r} is not authorized by CAN profile {self._config.profile_id!r}.",
            )

    def require_probe(self, probe_uid: object) -> None:
        if not self._config.adapter.physical:
            return
        if not isinstance(probe_uid, str) or probe_uid not in self._config.probe_uids:
            raise CanPolicyError(
                "can/probe-mismatch",
                "Active probe UID is not an exact authorized CAN fixture probe.",
            )

    def validate_transmit_request(
        self,
        arbitration_id: object,
        extended: object,
        data_hex: object,
        *,
        timeout_seconds: object,
    ) -> ValidatedCanTransmit:
        identifier = _identifier(arbitration_id, extended)
        data = _payload(data_hex)
        timeout = (
            self._config.transmit.default_send_timeout_seconds
            if timeout_seconds is None
            else _strict_positive_float(
                timeout_seconds,
                "timeout_seconds",
                self._config.transmit.max_send_timeout_seconds,
            )
        )
        if not self._config.transmit.enabled:
            raise CanPolicyError("can/tx-disabled", "CAN transmit is disabled by operator policy.")
        if identifier not in self._config.transmit.exact_ids:
            raise CanPolicyError(
                "can/id-forbidden",
                f"CAN identifier {identifier.display()} is absent from the exact transmit allowlist.",
            )
        return ValidatedCanTransmit(identifier=identifier, data=data, timeout_seconds=timeout)

    def validate_read_request(
        self, *, read_seconds: object, max_frames: object
    ) -> ValidatedCanRead:
        duration = (
            self._config.receive.default_read_seconds
            if read_seconds is None
            else _strict_positive_float(
                read_seconds, "read_seconds", self._config.receive.max_read_seconds
            )
        )
        frame_count = (
            self._config.receive.default_max_frames
            if max_frames is None
            else _strict_positive_int(max_frames, "max_frames", self._config.receive.max_frames)
        )
        return ValidatedCanRead(read_seconds=duration, max_frames=frame_count)
