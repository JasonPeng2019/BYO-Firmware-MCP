# SPDX-License-Identifier: Apache-2.0
# Derived from Firmware-CLI commit 0e8a45d; modified for BYO-Firmware-MCP.
"""Backend-neutral CAN domain records and adapter protocol."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pyocd_debug_mcp.can_config import LoadedCanFixtureConfig


STANDARD_CAN_ID_MAX = 0x7FF
EXTENDED_CAN_ID_MAX = 0x1FFFFFFF
CLASSIC_CAN_MAX_DATA_BYTES = 8
CAN_FD_MAX_DATA_BYTES = 64
MAX_CAN_FRAME_CHANNEL_UTF8_BYTES = 128


def _strict_int(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an integer, not {type(value).__name__}")
    return value


@dataclass(frozen=True, order=True)
class CanIdentifier:
    """An exact CAN arbitration identifier plus its frame format."""

    arbitration_id: int
    extended: bool

    def __post_init__(self) -> None:
        arbitration_id = _strict_int(self.arbitration_id, "arbitration_id")
        if type(self.extended) is not bool:
            raise TypeError("extended must be a boolean")
        maximum = EXTENDED_CAN_ID_MAX if self.extended else STANDARD_CAN_ID_MAX
        if not 0 <= arbitration_id <= maximum:
            kind = "extended" if self.extended else "standard"
            raise ValueError(f"arbitration_id {arbitration_id!r} is outside the {kind} CAN range")

    @property
    def full_mask(self) -> int:
        return EXTENDED_CAN_ID_MAX if self.extended else STANDARD_CAN_ID_MAX

    def display(self) -> str:
        width = 8 if self.extended else 3
        return f"0x{self.arbitration_id:0{width}X}/{'extended' if self.extended else 'standard'}"


class CanDirection(str, Enum):
    RX = "rx"
    TX = "tx"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CanFrame:
    """A normalized CAN frame observation or Classical CAN command.

    ``dlc`` is retained separately from ``data`` because an RTR frame may carry
    no bytes while reporting a nonzero DLC.
    """

    arbitration_id: int
    extended: bool
    data: bytes
    dlc: int | None = None
    timestamp: float | None = None
    direction: CanDirection = CanDirection.UNKNOWN
    channel: str | None = None
    is_error: bool = False
    is_remote: bool = False
    is_fd: bool = False

    def __post_init__(self) -> None:
        CanIdentifier(self.arbitration_id, self.extended)
        if not isinstance(self.data, bytes):
            raise TypeError("data must be bytes")
        for name in ("is_error", "is_remote", "is_fd"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a boolean")
        if not isinstance(self.direction, CanDirection):
            raise TypeError("direction must be a CanDirection")
        if self.is_fd and self.is_remote:
            raise ValueError("CAN FD remote frames are not valid")
        maximum_data_bytes = CAN_FD_MAX_DATA_BYTES if self.is_fd else CLASSIC_CAN_MAX_DATA_BYTES
        if len(self.data) > maximum_data_bytes:
            frame_kind = "CAN FD" if self.is_fd else "Classical CAN"
            raise ValueError(f"{frame_kind} data must contain at most {maximum_data_bytes} bytes")
        if self.is_remote and self.data:
            raise ValueError("RTR frames must not contain payload bytes")
        effective_dlc = len(self.data) if self.dlc is None else _strict_int(self.dlc, "dlc")
        if not 0 <= effective_dlc <= maximum_data_bytes:
            raise ValueError(f"dlc must be between 0 and {maximum_data_bytes}")
        if not self.is_remote and effective_dlc != len(self.data):
            raise ValueError("non-RTR frame dlc must equal payload length")
        if self.timestamp is not None:
            if type(self.timestamp) not in {int, float}:
                raise TypeError("timestamp must be a finite number")
            try:
                timestamp = float(self.timestamp)
            except OverflowError as exc:
                raise ValueError("timestamp must be a finite non-negative number") from exc
            if not math.isfinite(timestamp) or timestamp < 0:
                raise ValueError("timestamp must be a finite non-negative number")
        if self.channel is not None:
            if not isinstance(self.channel, str):
                raise TypeError("channel must be a string or None")
            try:
                encoded_channel = self.channel.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("channel must be valid UTF-8 text") from exc
            if len(encoded_channel) > MAX_CAN_FRAME_CHANNEL_UTF8_BYTES:
                raise ValueError(
                    f"channel must contain at most {MAX_CAN_FRAME_CHANNEL_UTF8_BYTES} UTF-8 bytes"
                )
            if self.channel and not self.channel.isprintable():
                raise ValueError("channel must contain only printable characters")
        object.__setattr__(self, "dlc", effective_dlc)

    @property
    def identifier(self) -> CanIdentifier:
        return CanIdentifier(self.arbitration_id, self.extended)


@dataclass(frozen=True)
class CanFilter:
    """A v1 exact-match backend filter; partial masks are not representable."""

    arbitration_id: int
    mask: int
    extended: bool

    def __post_init__(self) -> None:
        identifier = CanIdentifier(self.arbitration_id, self.extended)
        if _strict_int(self.mask, "mask") != identifier.full_mask:
            raise ValueError("CAN v1 filters must use the full-width exact-match mask")

    @classmethod
    def exact(cls, identifier: CanIdentifier) -> CanFilter:
        return cls(
            arbitration_id=identifier.arbitration_id,
            mask=identifier.full_mask,
            extended=identifier.extended,
        )


class CapabilityStatus(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CanBackendCapabilities:
    bounded_open: CapabilityStatus
    bounded_send: CapabilityStatus
    bounded_receive: CapabilityStatus
    bounded_close: CapabilityStatus
    pending_tx_quiescence: CapabilityStatus
    physical: bool
    pending_tx_empty: bool | None = None
    driver: str | None = None
    device_identity: tuple[str, ...] = ()
    observed_channel: str | None = None
    interface_index: int | None = None
    link_kind: str | None = None
    nominal_bitrate: int | None = None
    listen_only: bool | None = None
    one_shot: bool | None = None
    loopback: bool | None = None
    bus_state: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "bounded_open",
            "bounded_send",
            "bounded_receive",
            "bounded_close",
            "pending_tx_quiescence",
        ):
            if not isinstance(getattr(self, field_name), CapabilityStatus):
                raise TypeError(f"{field_name} must be a CapabilityStatus")
        if type(self.physical) is not bool:
            raise TypeError("physical must be a boolean")
        if self.driver is not None and (not isinstance(self.driver, str) or not self.driver):
            raise TypeError("driver must be a non-empty string or None")
        if isinstance(self.device_identity, (str, bytes)):
            raise TypeError("device_identity must be an iterable of strings")
        try:
            identity = tuple(self.device_identity)
        except TypeError as exc:
            raise TypeError("device_identity must be an iterable of strings") from exc
        if any(not isinstance(value, str) or not value for value in identity):
            raise TypeError("device_identity entries must be non-empty strings")
        object.__setattr__(self, "device_identity", identity)
        for field_name in ("observed_channel", "link_kind"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value):
                raise TypeError(f"{field_name} must be a non-empty string or None")
        if self.interface_index is not None:
            if type(self.interface_index) is not int or self.interface_index <= 0:
                raise TypeError("interface_index must be a positive integer or None")
        if self.nominal_bitrate is not None:
            if type(self.nominal_bitrate) is not int or self.nominal_bitrate <= 0:
                raise TypeError("nominal_bitrate must be a positive integer or None")
        for field_name in ("pending_tx_empty", "listen_only", "one_shot", "loopback"):
            value = getattr(self, field_name)
            if value is not None and type(value) is not bool:
                raise TypeError(f"{field_name} must be a boolean or None")
        if self.bus_state is not None and (
            not isinstance(self.bus_state, str) or not self.bus_state
        ):
            raise TypeError("bus_state must be a non-empty string or None")


@dataclass(frozen=True)
class CanBusHandle:
    native_handle: object
    profile_id: str
    profile_sha256: str
    interface: str
    channel: str
    protocol: str
    filters: tuple[CanFilter, ...]
    interface_index: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("profile_id", "profile_sha256", "interface", "channel", "protocol"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise TypeError(f"{field_name} must be a non-empty string")
        filters = tuple(self.filters)
        if any(not isinstance(item, CanFilter) for item in filters):
            raise TypeError("filters must contain only CanFilter records")
        object.__setattr__(self, "filters", filters)
        if self.interface_index is not None:
            if type(self.interface_index) is not int or self.interface_index <= 0:
                raise TypeError("interface_index must be a positive integer or None")


@runtime_checkable
class CANInterface(Protocol):
    """The worker-owned backend contract implemented in milestone M2."""

    def inspect(self, config: LoadedCanFixtureConfig) -> CanBackendCapabilities: ...

    def open(self, config: LoadedCanFixtureConfig, timeout_seconds: float) -> CanBusHandle: ...

    def close(self, handle: CanBusHandle) -> None: ...

    def send(
        self,
        handle: CanBusHandle,
        frame: CanFrame,
        timeout_seconds: float,
        *,
        not_after_monotonic: float | None = None,
    ) -> CanFrame: ...

    def receive(self, handle: CanBusHandle, timeout_seconds: float) -> CanFrame | None: ...

    def state(self, handle: CanBusHandle) -> str: ...
