"""One hardware inventory service for probes and UARTs, native and hook-discovered.

This is the promoted body of `server._validation_inventory`, parameterized by injected
callables so it is testable without a live server. Five of the eight discovery call
sites already funnelled through `_validation_inventory`; keeping one service -- rather
than a parallel one beside it -- is what converts them all at once.

Two invariants live here and nowhere else:

* **Hooks execute per kind, and only when that kind's native result is empty.** The
  decision is made inside `snapshot()`, never at a call site. `_resolve_serial_port_for_session`
  runs before *every* UART action, so an unconditional hook execution there would put
  subprocess launches inside `read_serial`, `write_serial`, `serial_exchange`, and the
  `on_exit` finalizer. Gating per kind removes that at the source; a machine where native
  detection works never pays for hooks at all. `hook_diagnostics` is therefore empty on a
  healthy machine, which callers must tolerate rather than treat as an error.
* **A hook row can never mask or outrank a natively visible device**, because a hook
  never runs while one is present.

Merge rules are still written to handle a native row and a hook row describing the same
device: a device can appear natively on one refresh and only via hook on the next.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from typing import Callable, Literal, Sequence

from pyocd_debug_mcp.discovery_hooks import (
    EMPTY_SNAPSHOT,
    DiscoveryHookSnapshot,
    HookExecution,
    execute_eligible_hooks,
)
from pyocd_debug_mcp.probe_inventory import (
    EMPTY_NATIVE_PROBE_LISTING,
    NativeProbeListing,
)
from pyocd_debug_mcp.serial_resolver import SerialPortInfo, normalize_port_name
from pyocd_debug_mcp.setup_flow.validate import (
    ValidationInventory,
    ValidationProbe,
    ValidationSerial,
)

IdentityScope = Literal["stable", "session"]

NATIVE_PROVENANCE = "native"


def stable_identity_equal(left: str | None, right: str | None) -> bool:
    """Compare stable USB identifiers without conflating mutable display labels.

    Moved here from `server.py` (and re-exported there) so the merge rules and the
    setup comparison helpers cannot drift apart. Deliberately narrow: exact casefold
    match, or decimal comparison with leading zeros stripped. Punctuation is never
    stripped broadly, because that would conflate distinct vendors' identifier formats.
    """

    if not left or not right:
        return False
    left_normalized = left.strip().casefold()
    right_normalized = right.strip().casefold()
    if left_normalized == right_normalized:
        return True
    if left_normalized.isdecimal() and right_normalized.isdecimal():
        return (left_normalized.lstrip("0") or "0") == (right_normalized.lstrip("0") or "0")
    return False


# --------------------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeRow:
    """One debug probe visible to this server, from any source."""

    provider: str
    probe_id: str
    """The key existing callers use as a pyOCD selector or session token.

    Equal to `unique_id` for every real probe; for a UID-less live session it is that
    session's exact connection ID, which is not hardware-stable.
    """
    unique_id: str | None
    """The exact pyOCD selector. `None` for a UID-less live session."""
    row_id: str
    description: str
    stable_identity: str | None
    provenance: tuple[str, ...]
    hook_source_sha256: str | None
    identity_scope: IdentityScope
    snapshot_id: str

    @property
    def from_hook(self) -> bool:
        return any(token.startswith("hook:") for token in self.provenance)

    @property
    def native(self) -> bool:
        return NATIVE_PROVENANCE in self.provenance


@dataclass(frozen=True, slots=True)
class UartRow:
    """One serial endpoint visible to this server, from any source."""

    port_path: str
    description: str
    usb_serial: str | None
    vid: int | None
    pid: int | None
    provenance: tuple[str, ...]
    identity_scope: IdentityScope
    row_id: str
    snapshot_id: str
    hook_source_sha256: str | None = None

    @property
    def from_hook(self) -> bool:
        return any(token.startswith("hook:") for token in self.provenance)

    @property
    def native(self) -> bool:
        return NATIVE_PROVENANCE in self.provenance

    def stable_key(self) -> tuple[str, int, int] | None:
        """The same key `SerialEndpoint.stable_key()` produces."""

        if self.identity_scope != "stable":
            return None
        assert self.usb_serial is not None and self.vid is not None and self.pid is not None
        return (self.usb_serial.strip(), self.vid, self.pid)


@dataclass(frozen=True, slots=True)
class ActiveConnectionRow:
    """A probe this process already has open.

    pyOCD's inventory intentionally omits these, but validation must still be able to
    select and stamp the server-owned active connection. Built by the server, which
    owns `session_metadata`, and merged here with the original skip-if-present rule.
    """

    probe_id: str
    probe_uid: str | None
    description: str
    probe_family: str


@dataclass(frozen=True, slots=True)
class VendorUartRow:
    """A serial endpoint reported by a legacy vendor helper (`SERIAL_FALLBACKS`)."""

    provider_id: str
    port_path: str
    description: str
    usb_serial: str | None = None
    vid: int | None = None
    pid: int | None = None

    @property
    def provenance(self) -> str:
        return f"vendor:{self.provider_id}"


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    """One atomic view of the hardware. Never mix rows from two of these."""

    snapshot_id: str
    probes: tuple[ProbeRow, ...]
    uarts: tuple[UartRow, ...]
    native_probe_diagnostics: NativeProbeListing
    native_uart_available: bool
    hook_diagnostics: tuple[HookExecution, ...]
    hook_manifest_sha256: str = ""

    @property
    def native_probes(self) -> tuple[ProbeRow, ...]:
        return tuple(row for row in self.probes if row.native)

    @property
    def native_uarts(self) -> tuple[UartRow, ...]:
        return tuple(row for row in self.uarts if row.native)

    @property
    def hook_failures(self) -> tuple[HookExecution, ...]:
        return tuple(execution for execution in self.hook_diagnostics if not execution.ok)

    def probe_by_row_id(self, row_id: str) -> ProbeRow | None:
        return next((row for row in self.probes if row.row_id == row_id), None)

    def hook_diagnostic_rows(self) -> list[dict[str, object]]:
        return [execution.diagnostic_row() for execution in self.hook_diagnostics]


EMPTY_INVENTORY_SNAPSHOT = InventorySnapshot(
    snapshot_id="",
    probes=(),
    uarts=(),
    native_probe_diagnostics=EMPTY_NATIVE_PROBE_LISTING,
    native_uart_available=False,
    hook_diagnostics=(),
)


# --------------------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------------------


HookRunner = Callable[[DiscoveryHookSnapshot, str], Sequence[HookExecution]]


@dataclass
class HardwareInventoryService:
    """Collect native rows, then supplement with hooks only where native came back empty."""

    native_probes: Callable[[], NativeProbeListing]
    native_uarts: Callable[[], list[SerialPortInfo] | None]
    active_connections: Callable[[], Sequence[ActiveConnectionRow]] = lambda: ()
    hook_snapshot: Callable[[], DiscoveryHookSnapshot] = lambda: EMPTY_SNAPSHOT
    vendor_uarts: Callable[[], Sequence[VendorUartRow]] = lambda: ()
    run_hooks: HookRunner = field(default=lambda snapshot, kind: execute_eligible_hooks(snapshot, kind))

    def snapshot(self) -> InventorySnapshot:
        """Take one atomic inventory view, running hooks only where needed."""

        snapshot_id = secrets.token_urlsafe(12)
        counter = _RowIds(snapshot_id)

        native_listing = self.native_probes()
        probe_rows = self._native_probe_rows(native_listing, counter)

        native_ports = self.native_uarts()
        native_uart_available = native_ports is not None
        uart_rows = self._native_uart_rows(native_ports or (), counter)

        hooks = self.hook_snapshot()
        diagnostics: list[HookExecution] = []

        # ---- The gating rule, in exactly one place -------------------------------
        # Evaluated fresh per snapshot, independently per kind. A combined flag would
        # pass the both-empty case and silently break the mixed one.
        run_probe_hooks = bool(probe_rows) is False and hooks.has_hooks_for("probe")
        run_uart_hooks = bool(uart_rows) is False and hooks.has_hooks_for("uart")

        if run_probe_hooks:
            executions = tuple(self.run_hooks(hooks, "probe"))
            diagnostics.extend(executions)
            probe_rows = self._merge_probe_rows(
                probe_rows, self._hook_probe_rows(executions, counter)
            )

        if not uart_rows:
            # Legacy vendor helpers are a third provenance source behind this layer,
            # and share the UART gate so they never reach the serial hot path either.
            vendor_rows = self._vendor_uart_rows(counter)
            uart_rows = self._merge_uart_rows(uart_rows, vendor_rows)

        if run_uart_hooks:
            executions = tuple(self.run_hooks(hooks, "uart"))
            diagnostics.extend(executions)
            uart_rows = self._merge_uart_rows(
                uart_rows, self._hook_uart_rows(executions, counter)
            )

        return InventorySnapshot(
            snapshot_id=snapshot_id,
            probes=tuple(probe_rows),
            uarts=tuple(uart_rows),
            native_probe_diagnostics=native_listing,
            native_uart_available=native_uart_available,
            hook_diagnostics=tuple(diagnostics),
            hook_manifest_sha256=hooks.manifest_sha256,
        )

    # -- native collection -------------------------------------------------------

    def _native_probe_rows(
        self,
        listing: NativeProbeListing,
        counter: _RowIds,
    ) -> list[ProbeRow]:
        rows: list[ProbeRow] = []
        seen: set[str] = set()
        for probe in listing.probes:
            if probe.uid in seen:
                continue
            seen.add(probe.uid)
            rows.append(
                ProbeRow(
                    provider=probe.family,
                    probe_id=probe.uid,
                    unique_id=probe.uid or None,
                    row_id=counter.next(),
                    description=probe.description or probe.raw,
                    stable_identity=probe.uid or None,
                    provenance=(NATIVE_PROVENANCE,),
                    hook_source_sha256=None,
                    identity_scope="stable",
                    snapshot_id=counter.snapshot_id,
                )
            )
        # pyOCD inventory intentionally omits probes already opened by this process.
        # Validation must still be able to select and stamp the server-owned active
        # connection. A hardware UID remains the stable inventory key; a UID-less
        # provider is represented only by its exact live, session-local connection ID.
        for active in self.active_connections():
            if active.probe_id in seen:
                continue
            seen.add(active.probe_id)
            rows.append(
                ProbeRow(
                    provider=active.probe_family or "unknown",
                    probe_id=active.probe_id,
                    unique_id=active.probe_uid or None,
                    row_id=counter.next(),
                    description=active.description,
                    stable_identity=active.probe_uid or None,
                    provenance=(NATIVE_PROVENANCE,),
                    hook_source_sha256=None,
                    identity_scope="stable" if active.probe_uid else "session",
                    snapshot_id=counter.snapshot_id,
                )
            )
        return rows

    def _native_uart_rows(
        self,
        ports: Sequence[SerialPortInfo],
        counter: _RowIds,
    ) -> list[UartRow]:
        rows: list[UartRow] = []
        for port in ports:
            usb_serial = port.serial_number or None
            rows.append(
                UartRow(
                    port_path=port.device,
                    description=port.description or port.product or "Serial connection",
                    usb_serial=usb_serial,
                    vid=port.vid,
                    pid=port.pid,
                    provenance=(NATIVE_PROVENANCE,),
                    identity_scope=_uart_scope(usb_serial, port.vid, port.pid),
                    row_id=counter.next(),
                    snapshot_id=counter.snapshot_id,
                )
            )
        return rows

    def _vendor_uart_rows(self, counter: _RowIds) -> list[UartRow]:
        rows: list[UartRow] = []
        for vendor in self.vendor_uarts():
            rows.append(
                UartRow(
                    port_path=vendor.port_path,
                    description=vendor.description,
                    usb_serial=vendor.usb_serial,
                    vid=vendor.vid,
                    pid=vendor.pid,
                    provenance=(vendor.provenance,),
                    identity_scope=_uart_scope(vendor.usb_serial, vendor.vid, vendor.pid),
                    row_id=counter.next(),
                    snapshot_id=counter.snapshot_id,
                )
            )
        return rows

    # -- hook collection ---------------------------------------------------------

    @staticmethod
    def _hook_probe_rows(
        executions: Sequence[HookExecution],
        counter: _RowIds,
    ) -> list[ProbeRow]:
        rows: list[ProbeRow] = []
        for execution in executions:
            if execution.output is None:
                continue
            for probe in execution.output.probes:
                rows.append(
                    ProbeRow(
                        provider=probe.provider,
                        probe_id=probe.unique_id,
                        unique_id=probe.unique_id,
                        row_id=counter.next(),
                        description=probe.description,
                        stable_identity=probe.unique_id,
                        provenance=(f"hook:{execution.hook_id}",),
                        hook_source_sha256=execution.file_sha256,
                        identity_scope="stable",
                        snapshot_id=counter.snapshot_id,
                    )
                )
        return rows

    @staticmethod
    def _hook_uart_rows(
        executions: Sequence[HookExecution],
        counter: _RowIds,
    ) -> list[UartRow]:
        rows: list[UartRow] = []
        for execution in executions:
            if execution.output is None:
                continue
            for uart in execution.output.uarts:
                rows.append(
                    UartRow(
                        port_path=uart.port_path,
                        description=uart.description,
                        usb_serial=uart.serial_number,
                        vid=uart.vid,
                        pid=uart.pid,
                        provenance=(f"hook:{execution.hook_id}",),
                        identity_scope=_uart_scope(uart.serial_number, uart.vid, uart.pid),
                        row_id=counter.next(),
                        snapshot_id=counter.snapshot_id,
                        hook_source_sha256=execution.file_sha256,
                    )
                )
        return rows

    # -- merge -------------------------------------------------------------------

    @staticmethod
    def _merge_probe_rows(
        existing: list[ProbeRow],
        additions: Sequence[ProbeRow],
    ) -> list[ProbeRow]:
        """Dedupe only within one provider; never merge across providers.

        Two providers reporting identical UID text stay distinct rows: the text is a
        provider-scoped selector, not a global identity.
        """

        merged = list(existing)
        for addition in additions:
            index = _matching_probe_index(merged, addition)
            if index is None:
                merged.append(addition)
                continue
            merged[index] = _combine_probe_rows(merged[index], addition)
        return merged

    @staticmethod
    def _merge_uart_rows(
        existing: list[UartRow],
        additions: Sequence[UartRow],
    ) -> list[UartRow]:
        merged = list(existing)
        for addition in additions:
            index = _matching_uart_index(merged, addition)
            if index is None:
                merged.append(addition)
                continue
            merged[index] = _combine_uart_rows(merged[index], addition)
        return merged

    # -- adapters ----------------------------------------------------------------

    def validation_inventory(self) -> ValidationInventory:
        """Adapter preserving the exact shape existing callers already consume."""

        return validation_inventory_from(self.snapshot())


# --------------------------------------------------------------------------------------
# Probe selection records
# --------------------------------------------------------------------------------------


class SelectionDisappeared(RuntimeError):
    """A recorded probe selection is no longer derivable from a fresh snapshot.

    Carries the typed code the response layer reports. Never resolved by substituting a
    different probe: the assignment is cleared and the agent is routed back through
    setup instead.
    """

    code = "discovery/selection-disappeared"

    def __init__(self, connection_id: str, reason: str) -> None:
        super().__init__(reason)
        self.connection_id = connection_id
        self.reason = reason


class SelectionNotRecorded(SelectionDisappeared):
    """No selection was ever recorded for this connection ID in this run."""


@dataclass(frozen=True, slots=True)
class ProbeSelection:
    """What an opaque `connection_id` handed to an agent actually refers to."""

    connection_id: str
    provider: str
    unique_id: str | None
    stable_identity: str | None
    provenance: tuple[str, ...]
    hook_source_sha256: str | None
    identity_scope: IdentityScope

    @property
    def durable(self) -> bool:
        """Session-scope selections are usable this run and refused for anything durable."""

        return self.identity_scope == "stable"

    @classmethod
    def from_row(cls, connection_id: str, row: ProbeRow) -> ProbeSelection:
        return cls(
            connection_id=connection_id,
            provider=row.provider,
            unique_id=row.unique_id,
            stable_identity=row.stable_identity,
            provenance=row.provenance,
            hook_source_sha256=row.hook_source_sha256,
            identity_scope=row.identity_scope,
        )


class ProbeSelectionStore:
    """Run-scoped, memory-only map from opaque connection ID to what it selected.

    Not authority: it records what an identifier already meant, and grants nothing. A
    selection is only ever *re-derived* against a fresh snapshot, never trusted as a
    standing claim that the hardware is still there.
    """

    __slots__ = ("_guard", "_selections")

    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._selections: dict[str, ProbeSelection] = {}

    @staticmethod
    def _key(connection_id: str) -> str:
        return connection_id.strip().casefold()

    def record(self, selection: ProbeSelection) -> None:
        with self._guard:
            self._selections[self._key(selection.connection_id)] = selection

    def recorded(self, connection_id: str) -> ProbeSelection | None:
        with self._guard:
            return self._selections.get(self._key(connection_id))

    def forget(self, connection_id: str) -> None:
        with self._guard:
            self._selections.pop(self._key(connection_id), None)

    def clear(self) -> None:
        with self._guard:
            self._selections.clear()

    def resolve(self, connection_id: str, snapshot: InventorySnapshot) -> ProbeSelection:
        """Re-derive a recorded selection against a fresh snapshot.

        Raises `SelectionDisappeared` when the row is absent or its hook source changed.
        Never falls back to a similarly described probe.
        """

        recorded = self.recorded(connection_id)
        if recorded is None:
            raise SelectionNotRecorded(
                connection_id,
                "no probe selection was recorded for this connection in this run; "
                "rerun setup_overview to choose the current physical connection",
            )
        row = find_selected_row(recorded, snapshot)
        if row is None:
            raise SelectionDisappeared(
                connection_id,
                "the selected probe is no longer present; rerun setup routing to choose "
                "the current physical connection",
            )
        if (
            recorded.hook_source_sha256 is not None
            and row.hook_source_sha256 is not None
            and row.hook_source_sha256 != recorded.hook_source_sha256
        ):
            raise SelectionDisappeared(
                connection_id,
                "the hook that discovered this probe changed since it was selected; "
                "call refresh_discovery_hooks and rerun setup routing",
            )
        return ProbeSelection.from_row(connection_id, row)


def find_selected_row(selection: ProbeSelection, snapshot: InventorySnapshot) -> ProbeRow | None:
    """Locate the row a recorded selection still refers to, within its provider."""

    for row in snapshot.probes:
        if row.provider != selection.provider:
            continue
        if selection.identity_scope == "session":
            # A session token identifies only that live worker, so only an exact match
            # counts; it is deliberately not stable across reconnects.
            if row.identity_scope == "session" and row.probe_id == selection.connection_id:
                return row
            continue
        if stable_identity_equal(row.stable_identity, selection.stable_identity):
            return row
    return None


def validation_inventory_from(snapshot: InventorySnapshot) -> ValidationInventory:
    """Project a snapshot onto the legacy `ValidationInventory` shape.

    Probe ordering is sorted by `probe_id`, exactly as the dict-of-probes it replaces
    produced, so callers that depended on that order are unaffected.
    """

    probes = tuple(
        ValidationProbe(
            row.probe_id,
            row.description,
            row.provider,
            row.unique_id,
        )
        for row in sorted(snapshot.probes, key=lambda row: row.probe_id)
    )
    serial = tuple(
        ValidationSerial(
            row.usb_serial or row.port_path,
            row.port_path,
            row.description,
            row.usb_serial,
            row.vid,
            row.pid,
        )
        for row in snapshot.uarts
    )
    return ValidationInventory(probes, serial)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


class _RowIds:
    """Snapshot-scoped opaque row identifiers.

    Every row carries its snapshot's ID so one operation can be proven never to pair
    probes from one scan with UART rows from another.
    """

    __slots__ = ("snapshot_id", "_next", "_guard")

    def __init__(self, snapshot_id: str) -> None:
        self.snapshot_id = snapshot_id
        self._next = 0
        self._guard = threading.Lock()

    def next(self) -> str:
        with self._guard:
            ordinal = self._next
            self._next += 1
        return f"{self.snapshot_id}-{ordinal:03d}"


def snapshot_id_of(row_id: str) -> str:
    """Recover the snapshot a row was minted in."""

    return row_id.rsplit("-", 1)[0]


def _uart_scope(usb_serial: str | None, vid: int | None, pid: int | None) -> IdentityScope:
    """Stable only when serial number, VID, and PID are all present.

    The same predicate `SerialEndpoint.has_stable_identity` applies; anything weaker is
    session-local and must never reach `AttachmentCache`.
    """

    if not (usb_serial and usb_serial.strip()):
        return "session"
    for value in (vid, pid):
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFFFF:
            return "session"
    return "stable"


def _matching_probe_index(rows: Sequence[ProbeRow], candidate: ProbeRow) -> int | None:
    for index, row in enumerate(rows):
        if row.provider != candidate.provider:
            continue
        if row.probe_id == candidate.probe_id:
            return index
        if stable_identity_equal(row.stable_identity, candidate.stable_identity):
            return index
    return None


def _matching_uart_index(rows: Sequence[UartRow], candidate: UartRow) -> int | None:
    candidate_key = candidate.stable_key()
    for index, row in enumerate(rows):
        if candidate_key is not None:
            if row.stable_key() == candidate_key:
                return index
            continue
        # Session-local endpoints dedupe within one snapshot only, by normalized port
        # path plus source. `normalize_port_name` already strips the Windows `\\.\`
        # prefix and lowercases -- reused rather than reimplemented.
        if row.stable_key() is not None:
            continue
        if normalize_port_name(row.port_path) == normalize_port_name(candidate.port_path) and (
            row.provenance == candidate.provenance
        ):
            return index
    return None


def _union_provenance(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    combined = list(left)
    for token in right:
        if token not in combined:
            combined.append(token)
    return tuple(combined)


def _combine_probe_rows(kept: ProbeRow, addition: ProbeRow) -> ProbeRow:
    """One row for one device, recording every source that saw it.

    The earlier row wins on descriptive fields, so a hook row can never overwrite a
    native description or selector.
    """

    return ProbeRow(
        provider=kept.provider,
        probe_id=kept.probe_id,
        unique_id=kept.unique_id or addition.unique_id,
        row_id=kept.row_id,
        description=kept.description,
        stable_identity=kept.stable_identity or addition.stable_identity,
        provenance=_union_provenance(kept.provenance, addition.provenance),
        hook_source_sha256=kept.hook_source_sha256 or addition.hook_source_sha256,
        identity_scope=kept.identity_scope,
        snapshot_id=kept.snapshot_id,
    )


def _combine_uart_rows(kept: UartRow, addition: UartRow) -> UartRow:
    return UartRow(
        port_path=kept.port_path,
        description=kept.description,
        usb_serial=kept.usb_serial or addition.usb_serial,
        vid=kept.vid if kept.vid is not None else addition.vid,
        pid=kept.pid if kept.pid is not None else addition.pid,
        provenance=_union_provenance(kept.provenance, addition.provenance),
        identity_scope=kept.identity_scope,
        row_id=kept.row_id,
        snapshot_id=kept.snapshot_id,
        hook_source_sha256=kept.hook_source_sha256 or addition.hook_source_sha256,
    )
