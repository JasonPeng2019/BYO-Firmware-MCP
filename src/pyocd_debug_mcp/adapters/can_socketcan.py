# SPDX-License-Identifier: Apache-2.0
# Derived from Firmware-CLI commit 0e8a45d; modified for BYO-Firmware-MCP.
"""Read-only Linux SocketCAN capability and stable-device inspection."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

from pyocd_debug_mcp.adapters.can_interface import (
    CanBackendCapabilities,
    CapabilityStatus,
)
from pyocd_debug_mcp.can_config import LoadedCanFixtureConfig


IFF_UP = 0x1


def _attribute(container: object, name: str) -> object | None:
    getter = getattr(container, "get_attr", None)
    if not callable(getter):
        return None
    return cast(object | None, getter(name))


def _read_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _usb_identity(device_path: Path) -> tuple[str | None, str | None, str | None, str | None]:
    try:
        current = device_path.resolve(strict=True)
    except OSError:
        return None, None, None, None
    driver: str | None = None
    driver_link = device_path / "driver"
    try:
        driver = driver_link.resolve(strict=True).name
    except OSError:
        pass
    for candidate in (current, *current.parents):
        vendor = _read_text(candidate / "idVendor")
        product = _read_text(candidate / "idProduct")
        serial = _read_text(candidate / "serial")
        if vendor is not None and product is not None and serial is not None:
            return driver, vendor.lower().zfill(4), product.lower().zfill(4), serial
    return driver, None, None, None


class LinuxSocketCanInspector:
    """Inspect one existing CAN netdev without changing host configuration."""

    def __init__(self, *, sys_class_net: Path = Path("/sys/class/net")) -> None:
        self._sys_class_net = sys_class_net

    @staticmethod
    def _link_message(channel: str) -> object:
        from pyroute2 import IPRoute  # type: ignore[import-untyped]

        with IPRoute() as route:
            indexes = list(route.link_lookup(ifname=channel))
            if len(indexes) != 1:
                raise RuntimeError("SocketCAN channel did not resolve to exactly one interface")
            messages = list(route.get_links(indexes[0]))
        if len(messages) != 1:
            raise RuntimeError("SocketCAN channel inspection returned an ambiguous interface")
        return messages[0]

    def inspect(self, config: LoadedCanFixtureConfig) -> CanBackendCapabilities:
        channel = config.config.adapter.channel
        if os.name != "posix" or not hasattr(os, "uname") or os.uname().sysname != "Linux":
            return self._unknown(channel)
        try:
            message = self._link_message(channel)
            ifname = _attribute(message, "IFLA_IFNAME")
            link_info = _attribute(message, "IFLA_LINKINFO")
            link_kind = _attribute(link_info, "IFLA_INFO_KIND")
            info_data = _attribute(link_info, "IFLA_INFO_DATA")
            index = (
                message.get("index") if isinstance(message, dict) else message["index"]  # type: ignore[index]
            )
            flags = (
                message.get("flags", 0) if isinstance(message, dict) else message["flags"]  # type: ignore[index]
            )
            if not isinstance(ifname, str) or not isinstance(link_kind, str):
                raise RuntimeError("SocketCAN link identity was not observable")
            if type(index) is not int or index <= 0 or type(flags) is not int:
                raise RuntimeError("SocketCAN ifindex/flags were not observable")
            bitrate_record = _attribute(info_data, "IFLA_CAN_BITTIMING")
            ctrlmode = _attribute(info_data, "IFLA_CAN_CTRLMODE")
            can_state = _attribute(info_data, "IFLA_CAN_STATE")
            bitrate = bitrate_record.get("bitrate") if isinstance(bitrate_record, dict) else None

            def ctrl(name: str) -> bool | None:
                if not isinstance(ctrlmode, dict):
                    return None
                value = ctrlmode.get(name)
                if value == "on":
                    return True
                if value == "off":
                    return False
                return None

            is_up = bool(flags & IFF_UP)
            normalized_state = str(can_state).lower() if can_state is not None else None
            if not is_up:
                bus_state = "down"
            elif normalized_state is not None:
                bus_state = normalized_state
            elif link_kind == "vcan":
                # vcan does not expose the physical controller CAN-state
                # attribute. A positively identified IFF_UP vcan link is the
                # complete liveness observation available for this test-only
                # backend.
                bus_state = "active"
            else:
                # Never turn a missing physical controller state into a
                # positive active attestation.
                bus_state = None
            device_path = self._sys_class_net / channel / "device"
            driver: str | None
            identity: tuple[str, ...]
            if link_kind == "vcan":
                physical = False
                driver = "socketcan"
                identity = ("vcan", ifname, str(index))
            else:
                physical = True
                driver, vendor, product, serial = _usb_identity(device_path)
                identity = (
                    cast(tuple[str, str, str, str], (driver, vendor, product, serial))
                    if all(value is not None for value in (driver, vendor, product, serial))
                    else ()
                )
            # Worker process isolation bounds vcan calls. Physical driver
            # capabilities remain UNKNOWN until an M0 hardware qualification
            # supplies a driver-specific close/TX-quiescence oracle; generic
            # rtnetlink state must not be promoted into that claim.
            bounded = (
                CapabilityStatus.SUPPORTED if link_kind == "vcan" else CapabilityStatus.UNKNOWN
            )
            return CanBackendCapabilities(
                bounded_open=bounded,
                bounded_send=bounded,
                bounded_receive=bounded,
                bounded_close=bounded,
                pending_tx_quiescence=(
                    CapabilityStatus.SUPPORTED if link_kind == "vcan" else CapabilityStatus.UNKNOWN
                ),
                physical=physical,
                pending_tx_empty=True if link_kind == "vcan" else None,
                driver=driver,
                device_identity=identity,
                observed_channel=ifname,
                interface_index=index,
                link_kind=link_kind,
                nominal_bitrate=bitrate if type(bitrate) is int and bitrate > 0 else None,
                listen_only=ctrl("listenonly"),
                one_shot=ctrl("one_shot"),
                loopback=ctrl("loopback"),
                bus_state=bus_state,
            )
        except Exception:  # noqa: BLE001 - any inspection failure is conservative UNKNOWN
            return self._unknown(channel)

    @staticmethod
    def _unknown(channel: str) -> CanBackendCapabilities:
        return CanBackendCapabilities(
            bounded_open=CapabilityStatus.UNKNOWN,
            bounded_send=CapabilityStatus.UNKNOWN,
            bounded_receive=CapabilityStatus.UNKNOWN,
            bounded_close=CapabilityStatus.UNKNOWN,
            pending_tx_quiescence=CapabilityStatus.UNKNOWN,
            physical=True,
            observed_channel=channel,
        )
