# SPDX-License-Identifier: Apache-2.0
# Derived from Firmware-CLI commit 0e8a45d; modified for BYO-Firmware-MCP.
"""Backend-neutral validation for CAN capability attestations."""

from __future__ import annotations

from pyocd_debug_mcp.adapters.can_interface import CanBackendCapabilities, CapabilityStatus
from pyocd_debug_mcp.can_config import LoadedCanFixtureConfig
from pyocd_debug_mcp.can_errors import CanConnectionError


def _unproven(message: str) -> CanConnectionError:
    return CanConnectionError(message, code="can/capability-unproven")


def validate_can_capabilities(
    loaded: LoadedCanFixtureConfig, capabilities: CanBackendCapabilities
) -> None:
    """Require the positive capability facts needed by one frozen profile."""

    config = loaded.config
    kind = config.adapter.kind
    if kind == "tcp_sim":
        if capabilities.physical is not False or capabilities.driver != "tcp_sim":
            raise _unproven("TCP simulator must be positively attested as software-only.")
        if capabilities.observed_channel != config.adapter.channel:
            raise _unproven("TCP simulator endpoint does not match the profile.")
        if any(
            status is not CapabilityStatus.SUPPORTED
            for status in (
                capabilities.bounded_open,
                capabilities.bounded_send,
                capabilities.bounded_receive,
                capabilities.bounded_close,
            )
        ):
            raise _unproven("Bounded TCP simulation operations are unproven.")
        # TCP submission is not peer delivery. No physical resource exists, and
        # buffered simulator frames are not a physical TX-quiescence guarantee.
        return

    if kind == "virtual":
        if capabilities.physical is not False:
            raise _unproven("Virtual CAN backend was not positively attested as virtual.")
        virtual_required = (
            capabilities.bounded_open,
            capabilities.bounded_send,
            capabilities.bounded_receive,
            capabilities.bounded_close,
            capabilities.pending_tx_quiescence,
        )
        if any(status is not CapabilityStatus.SUPPORTED for status in virtual_required):
            raise _unproven("Bounded virtual CAN capabilities are unproven.")
        if config.transmit.enabled and capabilities.pending_tx_empty is not True:
            raise _unproven("Virtual CAN pending-transmit-empty state is unproven.")
        return

    if kind == "socketcan_vcan":
        if capabilities.physical is not False:
            raise _unproven("socketcan_vcan requires a positively attested virtual CAN link.")
        if capabilities.link_kind != "vcan":
            raise _unproven("SocketCAN link kind is not vcan.")
        if capabilities.observed_channel != config.adapter.channel:
            raise _unproven("Observed vcan channel does not match the profile.")
        if capabilities.interface_index is None:
            raise _unproven("Observed vcan ifindex is unavailable.")
        if capabilities.bus_state not in {"active", "up", "error_active"}:
            raise _unproven("Observed vcan link is not active.")
        vcan_required = (
            capabilities.bounded_open,
            capabilities.bounded_send,
            capabilities.bounded_receive,
            capabilities.bounded_close,
            capabilities.pending_tx_quiescence,
        )
        if any(status is not CapabilityStatus.SUPPORTED for status in vcan_required):
            raise _unproven("Bounded vcan capabilities are unproven.")
        if config.transmit.enabled and capabilities.pending_tx_empty is not True:
            raise _unproven("vcan pending-transmit-empty state is unproven.")
        return

    if kind != "socketcan":
        raise CanConnectionError(
            f"Unsupported CAN backend kind: {kind!r}", code="can/backend-unsupported"
        )
    identity = config.adapter.identity
    if identity is None:
        raise _unproven("Physical adapter identity is missing.")
    if capabilities.physical is not True:
        raise _unproven("Observed SocketCAN channel is not a physical CAN interface.")
    if capabilities.observed_channel != config.adapter.channel:
        raise _unproven("Observed SocketCAN channel does not match the profile.")
    if capabilities.link_kind != "can":
        raise _unproven("Observed SocketCAN link kind is not physical CAN.")
    if capabilities.interface_index is None:
        raise _unproven("Observed physical CAN ifindex is unavailable.")
    physical_required = (
        capabilities.bounded_open,
        capabilities.bounded_receive,
        capabilities.bounded_close,
    )
    if any(status is not CapabilityStatus.SUPPORTED for status in physical_required):
        raise _unproven("Bounded physical CAN open/receive/close capability is unproven.")
    expected_identity = (
        identity.driver,
        f"{identity.usb_vid:04x}",
        f"{identity.usb_pid:04x}",
        identity.serial,
    )
    if capabilities.driver != identity.driver or capabilities.device_identity != expected_identity:
        raise _unproven(
            "Observed physical CAN adapter identity does not exactly match the profile."
        )
    if capabilities.nominal_bitrate != config.bus.expected_nominal_bitrate:
        raise _unproven("Observed CAN bitrate is unavailable or does not match the profile.")
    if capabilities.bus_state not in {"active", "up", "error_active"}:
        raise _unproven("Observed physical CAN link is not active.")
    if config.health_check is not None and capabilities.loopback is not False:
        raise _unproven("Physical CAN loopback-off state is unproven.")
    if config.transmit.enabled:
        tx_statuses = (
            capabilities.bounded_send,
            capabilities.pending_tx_quiescence,
        )
        if any(status is not CapabilityStatus.SUPPORTED for status in tx_statuses):
            raise _unproven("Bounded physical CAN transmit/quiescence capability is unproven.")
        if capabilities.pending_tx_empty is not True:
            raise _unproven("Physical CAN pending-transmit-empty state is unproven.")
        if capabilities.listen_only is not False:
            raise _unproven("Physical CAN listen-only-off state is unproven.")
        if config.bus.require_one_shot_for_tx and capabilities.one_shot is not True:
            raise _unproven("Physical CAN one-shot mode is unproven.")


_ENDPOINT_CONTINUITY_FIELDS = (
    "physical",
    "driver",
    "device_identity",
    "observed_channel",
    "interface_index",
    "link_kind",
    "nominal_bitrate",
    "listen_only",
    "one_shot",
    "loopback",
)


def validate_can_capability_continuity(
    previous: CanBackendCapabilities, current: CanBackendCapabilities
) -> None:
    """Reject endpoint/config identity drift across one open generation."""

    changed = [
        field
        for field in _ENDPOINT_CONTINUITY_FIELDS
        if getattr(previous, field) != getattr(current, field)
    ]
    if changed:
        raise _unproven(
            "CAN endpoint capability identity changed after open: " + ", ".join(changed)
        )
