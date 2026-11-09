"""Shared inventory fixture fixtures; no collected cases."""

from __future__ import annotations
from typing import Any, cast
from pyocd_debug_mcp.hardware_inventory import InventorySnapshot, ProbeRow, UartRow
from pyocd_debug_mcp.probe_inventory import EMPTY_NATIVE_PROBE_LISTING


def _probe_row(
    provider: str,
    uid: str | None,
    *,
    probe_id: str | None = None,
    provenance: tuple[str, ...] = ("native",),
    sha: str | None = None,
    scope: str = "stable",
) -> ProbeRow:
    identity = probe_id if probe_id is not None else (uid or "")
    return ProbeRow(
        provider=provider,
        probe_id=identity,
        unique_id=uid,
        row_id=f"row-{provider}-{identity}",
        description=f"{provider} {identity}",
        stable_identity=uid,
        provenance=provenance,
        hook_source_sha256=sha,
        identity_scope=cast(Any, scope),
        snapshot_id="snap",
    )


def _uart_row(path: str, *, scope: str = "session", serial: str | None = None) -> UartRow:
    return UartRow(
        port_path=path,
        description=f"UART {path}",
        usb_serial=serial,
        vid=1 if scope == "stable" else None,
        pid=2 if scope == "stable" else None,
        provenance=("hook:h",),
        identity_scope=cast(Any, scope),
        row_id=f"row-{path}",
        snapshot_id="snap",
    )


def _snapshot(*rows: ProbeRow, uarts: tuple[UartRow, ...] = ()) -> InventorySnapshot:
    return InventorySnapshot(
        snapshot_id="snap",
        probes=rows,
        uarts=uarts,
        native_probe_diagnostics=EMPTY_NATIVE_PROBE_LISTING,
        native_uart_available=True,
        hook_diagnostics=(),
    )
