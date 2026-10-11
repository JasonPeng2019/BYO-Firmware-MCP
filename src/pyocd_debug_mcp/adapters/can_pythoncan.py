# SPDX-License-Identifier: Apache-2.0
# Derived from Firmware-CLI commit 0e8a45d; modified for BYO-Firmware-MCP.
"""Worker-only translation between BYO CAN records and ``python-can``."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Protocol, cast

import can

from pyocd_debug_mcp.adapters.can_interface import (
    CANInterface,
    CanBackendCapabilities,
    CanBusHandle,
    CanDirection,
    CanFilter,
    CanFrame,
    CapabilityStatus,
)
from pyocd_debug_mcp.adapters.can_socketcan import LinuxSocketCanInspector
from pyocd_debug_mcp.can_capabilities import (
    validate_can_capabilities,
    validate_can_capability_continuity,
)
from pyocd_debug_mcp.can_config import LoadedCanFixtureConfig
from pyocd_debug_mcp.can_errors import (
    CanCleanupError,
    CanConnectionError,
    CanOperationError,
    CanPolicyError,
    CanTimeoutError,
)


class PhysicalCanInspector(Protocol):
    """Read-only host capability inspector that never constructs a CAN bus."""

    def inspect(self, config: LoadedCanFixtureConfig) -> CanBackendCapabilities: ...


def _validate_timeout(timeout_seconds: float) -> None:
    if type(timeout_seconds) not in {int, float}:
        raise CanOperationError("CAN adapter timeout must be a finite positive number")
    try:
        value = float(timeout_seconds)
    except OverflowError as exc:
        raise CanOperationError("CAN adapter timeout must be a finite positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise CanOperationError("CAN adapter timeout must be a finite positive number")


def _virtual_capabilities(kind: str) -> CanBackendCapabilities:
    is_in_process = kind == "virtual"
    return CanBackendCapabilities(
        bounded_open=CapabilityStatus.SUPPORTED,
        bounded_send=CapabilityStatus.SUPPORTED,
        bounded_receive=CapabilityStatus.SUPPORTED,
        bounded_close=CapabilityStatus.SUPPORTED,
        pending_tx_quiescence=(
            CapabilityStatus.SUPPORTED if is_in_process else CapabilityStatus.UNKNOWN
        ),
        physical=False,
        pending_tx_empty=True if is_in_process else None,
        driver=kind,
        device_identity=(kind,),
        nominal_bitrate=None,
        listen_only=False,
        one_shot=True if is_in_process else None,
        loopback=False if is_in_process else None,
        bus_state="active",
    )


class PythonCanAdapter(CANInterface):
    """Strict python-can backend used only inside the isolated CAN worker."""

    def __init__(
        self,
        *,
        bus_factory: Callable[..., object] = can.Bus,
        physical_inspector: PhysicalCanInspector | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus_factory = bus_factory
        self._physical_inspector = physical_inspector or LinuxSocketCanInspector()
        self._monotonic = monotonic
        self._closed_handles: list[CanBusHandle] = []
        self._opened_profiles: dict[
            int, tuple[CanBusHandle, LoadedCanFixtureConfig, CanBackendCapabilities]
        ] = {}
        self._close_lock = threading.Lock()

    def inspect(self, config: LoadedCanFixtureConfig) -> CanBackendCapabilities:
        kind = config.config.adapter.kind
        if kind in {"virtual", "socketcan_vcan"}:
            if kind == "virtual":
                return _virtual_capabilities(kind)
            return self._physical_inspector.inspect(config)
        if kind == "socketcan":
            return self._physical_inspector.inspect(config)
        raise CanConnectionError(
            f"Unsupported CAN backend kind: {kind!r}", code="can/backend-unsupported"
        )

    @staticmethod
    def _require_physical_capabilities(
        loaded: LoadedCanFixtureConfig, capabilities: CanBackendCapabilities
    ) -> None:
        validate_can_capabilities(loaded, capabilities)

    @staticmethod
    def _require_vcan_capabilities(
        loaded: LoadedCanFixtureConfig, capabilities: CanBackendCapabilities
    ) -> None:
        validate_can_capabilities(loaded, capabilities)

    def open(self, config: LoadedCanFixtureConfig, timeout_seconds: float) -> CanBusHandle:
        _validate_timeout(timeout_seconds)
        fixture = config.config
        capabilities: CanBackendCapabilities | None = None
        if fixture.adapter.kind == "socketcan_vcan":
            capabilities = self.inspect(config)
            self._require_vcan_capabilities(config, capabilities)
        elif fixture.adapter.physical:
            capabilities = self.inspect(config)
            self._require_physical_capabilities(config, capabilities)
        interface = "virtual" if fixture.adapter.kind == "virtual" else "socketcan"
        filters = tuple(CanFilter.exact(identifier) for identifier in fixture.receive.exact_ids)
        backend_filters = [
            {
                "can_id": item.arbitration_id,
                "can_mask": item.mask,
                "extended": item.extended,
            }
            for item in filters
        ]
        options: dict[str, object] = {
            "interface": interface,
            "channel": fixture.adapter.channel,
            "ignore_config": True,
            "receive_own_messages": fixture.bus.receive_own_messages,
            "fd": False,
            "can_filters": backend_filters,
        }
        try:
            native = self._bus_factory(**options)
        except Exception as exc:  # noqa: BLE001 - backend implementations vary
            raise CanConnectionError(f"Unable to open CAN backend: {exc}") from exc
        if capabilities is not None:
            try:
                after_open = self.inspect(config)
                if fixture.adapter.kind == "socketcan_vcan":
                    self._require_vcan_capabilities(config, after_open)
                else:
                    self._require_physical_capabilities(config, after_open)
                validate_can_capability_continuity(capabilities, after_open)
                capabilities = after_open
            except BaseException:
                try:
                    cast(can.BusABC, native).shutdown()
                except Exception as cleanup_exc:  # noqa: BLE001 - backend-specific cleanup
                    raise CanCleanupError(
                        f"CAN backend attestation failed and cleanup was uncertain: {cleanup_exc}"
                    ) from cleanup_exc
                raise
        handle = CanBusHandle(
            native_handle=native,
            profile_id=fixture.profile_id,
            profile_sha256=config.canonical_sha256,
            interface=interface,
            channel=fixture.adapter.channel,
            protocol=fixture.protocol,
            filters=filters,
            interface_index=capabilities.interface_index if capabilities is not None else None,
        )
        self._opened_profiles[id(handle)] = (
            handle,
            config,
            capabilities or self.inspect(config),
        )
        return handle

    @staticmethod
    def _native(handle: CanBusHandle) -> can.BusABC:
        return cast(can.BusABC, handle.native_handle)

    def _opened_profile(
        self, handle: CanBusHandle
    ) -> tuple[LoadedCanFixtureConfig, CanBackendCapabilities]:
        opened = self._opened_profiles.get(id(handle))
        if opened is None or opened[0] is not handle:
            raise CanOperationError("CAN handle is closed or was not opened by this adapter")
        return opened[1], opened[2]

    def _revalidate_endpoint(
        self, handle: CanBusHandle
    ) -> tuple[LoadedCanFixtureConfig, CanBackendCapabilities]:
        loaded, previous = self._opened_profile(handle)
        if loaded.config.adapter.kind == "virtual":
            return loaded, previous
        current = self.inspect(loaded)
        if loaded.config.adapter.kind == "socketcan_vcan":
            self._require_vcan_capabilities(loaded, current)
        else:
            self._require_physical_capabilities(loaded, current)
        validate_can_capability_continuity(previous, current)
        return loaded, current

    def close(self, handle: CanBusHandle) -> None:
        native = self._native(handle)
        with self._close_lock:
            if any(closed is handle for closed in self._closed_handles):
                return
            self._opened_profile(handle)
            try:
                native.shutdown()
            except Exception as exc:  # noqa: BLE001 - backend implementations vary
                raise CanCleanupError(f"Unable to close CAN backend: {exc}") from exc
            self._closed_handles.append(handle)
            self._opened_profiles.pop(id(handle), None)

    def send(
        self,
        handle: CanBusHandle,
        frame: CanFrame,
        timeout_seconds: float,
        *,
        not_after_monotonic: float | None = None,
    ) -> CanFrame:
        _validate_timeout(timeout_seconds)
        if frame.is_error or frame.is_remote or frame.is_fd:
            raise CanOperationError("CAN v1 transmits only Classical CAN data frames")
        loaded, _capabilities = self._revalidate_endpoint(handle)
        absolute_deadline: float | None = None
        if not_after_monotonic is not None:
            if type(not_after_monotonic) not in {int, float}:
                raise CanOperationError("CAN absolute send deadline must be finite")
            try:
                absolute_deadline = float(not_after_monotonic)
            except OverflowError as exc:
                raise CanOperationError("CAN absolute send deadline must be finite") from exc
            if not math.isfinite(absolute_deadline):
                raise CanOperationError("CAN absolute send deadline must be finite")
        if not loaded.config.transmit.enabled:
            raise CanPolicyError("can/tx-disabled", "CAN transmit is disabled by operator policy.")
        if frame.identifier not in loaded.config.transmit.exact_ids:
            raise CanPolicyError(
                "can/id-forbidden",
                "Outgoing CAN identifier is absent from the exact transmit allowlist.",
            )
        try:
            message = can.Message(
                arbitration_id=frame.arbitration_id,
                is_extended_id=frame.extended,
                data=frame.data,
                is_remote_frame=False,
                is_error_frame=False,
                is_fd=False,
                check=True,
            )
        except Exception as exc:  # noqa: BLE001 - normalize all backend failures
            raise CanOperationError(f"CAN send failed: {exc}") from exc

        effective_timeout = float(timeout_seconds)
        if absolute_deadline is not None:
            remaining = absolute_deadline - self._monotonic()
            if remaining <= 0:
                raise CanTimeoutError("CAN send deadline expired before backend submission")
            effective_timeout = min(effective_timeout, remaining)
        try:
            self._native(handle).send(message, timeout=effective_timeout)
        except can.CanTimeoutError as exc:
            raise CanTimeoutError(f"CAN send timed out: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - normalize all backend failures
            raise CanOperationError(f"CAN send failed: {exc}") from exc
        return CanFrame(
            arbitration_id=frame.arbitration_id,
            extended=frame.extended,
            data=frame.data,
            dlc=len(frame.data),
            direction=CanDirection.TX,
            channel=handle.channel,
        )

    def receive(self, handle: CanBusHandle, timeout_seconds: float) -> CanFrame | None:
        _validate_timeout(timeout_seconds)
        self._revalidate_endpoint(handle)
        try:
            message = self._native(handle).recv(timeout=float(timeout_seconds))
            if message is None:
                return None
            channel = str(message.channel) if message.channel is not None else None
            direction = CanDirection.RX if message.is_rx else CanDirection.TX
            return CanFrame(
                arbitration_id=message.arbitration_id,
                extended=message.is_extended_id,
                data=bytes(message.data),
                dlc=message.dlc,
                timestamp=message.timestamp,
                direction=direction,
                channel=channel,
                is_error=message.is_error_frame,
                is_remote=message.is_remote_frame,
                is_fd=message.is_fd,
            )
        except can.CanTimeoutError as exc:
            raise CanTimeoutError(f"CAN receive timed out: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - normalize all backend failures
            raise CanOperationError(f"CAN receive failed: {exc}") from exc

    def state(self, handle: CanBusHandle) -> str:
        self._revalidate_endpoint(handle)
        try:
            state = self._native(handle).state
        except Exception as exc:  # noqa: BLE001 - normalize all backend failures
            raise CanOperationError(f"Unable to read CAN backend state: {exc}") from exc
        name = getattr(state, "name", None)
        return str(name).lower() if name is not None else str(state).lower()
