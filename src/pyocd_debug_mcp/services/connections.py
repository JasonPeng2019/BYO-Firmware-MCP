"""In-memory ownership and serialization for active board connections."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from urllib.parse import quote, unquote

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle, session_metadata
from pyocd_debug_mcp.services.session_runtime import SessionRecord


class ConnectionAssignmentError(RuntimeError):
    """Raised when a board or physical connection is already assigned."""


class BoardNotConnectedError(RuntimeError):
    """Raised when an operation names a board without an active connection."""


PROBE_CONNECTION_PREFIX = "probe:"


def probe_connection_id(provider: str, probe_uid: str) -> str:
    """Return the single canonical setup/connection identity for one probe.

    FIX 8 (C7/D8): provider-qualified. Two different providers can report identical
    UID text -- `HardwareInventoryService` correctly keeps such rows distinct (the
    guide's own mandated merge rule: "never merge across providers, even on identical
    UID text"), but a `connection_id` minted from UID text alone throws that away, so
    `_setup_overview` would silently drop one of two real, simultaneously-attached
    debuggers. Both fields are percent-encoded before joining, so a literal `:` inside
    either a provider name or a UID string can never be mistaken for the delimiter --
    the *new* format's own round trip is unambiguous regardless of content.

    (Real pyOCD/hook UIDs are alphanumeric in every provider this server has ever
    seen; a *legacy* two-part `probe:{uid}` token whose raw uid happens to contain a
    colon is a separate, pre-existing, and vanishingly unlikely edge case -- see
    `parse_probe_connection_id`.)
    """

    encoded_provider = quote(provider.strip().casefold(), safe="")
    encoded_uid = quote(probe_uid.strip().casefold(), safe="")
    return f"{PROBE_CONNECTION_PREFIX}{encoded_provider}:{encoded_uid}"


def parse_probe_connection_id(connection_id: str) -> tuple[str, str] | None:
    """Split a canonical provider-qualified token back into `(provider, uid)`.

    Returns `None` for a legacy two-part `probe:{uid}` token (exactly one colon after
    the prefix, so nothing to split), for a non-probe token (`session:...` or
    anything else), or for malformed input. Callers must handle the legacy shape
    themselves -- see the module-level comparison helpers and
    `hardware_inventory.derive_selection_from_token`, all of which tolerate it on
    read but never guess a provider for it.
    """

    if not connection_id.casefold().startswith(PROBE_CONNECTION_PREFIX):
        return None
    rest = connection_id[len(PROBE_CONNECTION_PREFIX) :]
    provider_part, separator, uid_part = rest.partition(":")
    if not separator:
        return None
    try:
        return unquote(provider_part), unquote(uid_part)
    except ValueError:
        return None


def stable_connection_identity(handle: TargetSessionHandle) -> str:
    """Return an immutable identity for a live connection.

    A probe UID is the preferred physical identity, qualified by the connection's own
    provider so two different providers reporting the same UID text never collide.
    When a provider exposes no UID, the frozen runtime token identifies only this live
    worker/session and is deliberately not stable across reconnects -- and is never
    provider-qualified, since it already names exactly one live session.
    """

    metadata = session_metadata(handle)
    probe_uid = (metadata.probe_uid or "").strip()
    if probe_uid:
        return probe_connection_id(str(metadata.probe_family or "unknown"), probe_uid)
    return f"session:{metadata.runtime_token}"


@dataclass(frozen=True, slots=True)
class ManagedConnection:
    """One live hardware connection assigned to one logical board."""

    board_id: str
    connection_id: str
    handle: TargetSessionHandle
    runtime_session: SessionRecord


class ConnectionManager:
    """Own active connections and a persistent serialization lock per board."""

    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._connections: dict[str, ManagedConnection] = {}
        self._boards_by_connection: dict[str, str] = {}
        self._locks: dict[str, threading.RLock] = {}

    @staticmethod
    def _normalize_board_id(board_id: str) -> str:
        normalized = board_id.strip()
        if not normalized:
            raise ValueError("board_id must be a non-empty string")
        return normalized

    def lock_for(self, board_id: str) -> threading.RLock:
        """Return the stable lock used to serialize one board's operations."""

        normalized = self._normalize_board_id(board_id)
        with self._guard:
            return self._locks.setdefault(normalized, threading.RLock())

    def assign(
        self,
        board_id: str,
        handle: TargetSessionHandle,
        runtime_session: SessionRecord,
        *,
        connection_id: str | None = None,
    ) -> ManagedConnection:
        """Assign an unowned live connection to an unconnected logical board."""

        normalized = self._normalize_board_id(board_id)
        identity = connection_id or stable_connection_identity(handle)
        with self._guard:
            if normalized in self._connections:
                raise ConnectionAssignmentError(
                    f"Board '{normalized}' already has an active connection."
                )
            owner = self._boards_by_connection.get(identity)
            if owner is not None:
                raise ConnectionAssignmentError(
                    f"Connection '{identity}' is already assigned to board '{owner}'."
                )
            connection = ManagedConnection(
                board_id=normalized,
                connection_id=identity,
                handle=handle,
                runtime_session=runtime_session,
            )
            self._connections[normalized] = connection
            self._boards_by_connection[identity] = normalized
            self._locks.setdefault(normalized, threading.RLock())
            return connection

    def connection_for(self, board_id: str) -> ManagedConnection:
        normalized = self._normalize_board_id(board_id)
        with self._guard:
            connection = self._connections.get(normalized)
        if connection is None:
            raise BoardNotConnectedError(
                f"Board '{normalized}' is not connected. "
                f"Call connect(board_id='{normalized}') first."
            )
        return connection

    def maybe_connection(self, board_id: str) -> ManagedConnection | None:
        normalized = self._normalize_board_id(board_id)
        with self._guard:
            return self._connections.get(normalized)

    def handle_for(self, board_id: str) -> TargetSessionHandle:
        return self.connection_for(board_id).handle

    def runtime_for(self, board_id: str) -> SessionRecord:
        return self.connection_for(board_id).runtime_session

    def clear(self, board_id: str) -> ManagedConnection | None:
        """Remove only the named assignment, leaving other boards untouched."""

        normalized = self._normalize_board_id(board_id)
        with self._guard:
            connection = self._connections.pop(normalized, None)
            if connection is not None:
                self._boards_by_connection.pop(connection.connection_id, None)
            return connection

    def clear_if_current(
        self,
        board_id: str,
        expected: ManagedConnection,
    ) -> ManagedConnection | None:
        """Remove the assignment only when it is still the exact captured object."""

        normalized = self._normalize_board_id(board_id)
        with self._guard:
            current = self._connections.get(normalized)
            if current is not expected:
                return None
            connection = self._connections.pop(normalized)
            self._boards_by_connection.pop(connection.connection_id, None)
            return connection

    def assigned_board_ids(self) -> tuple[str, ...]:
        with self._guard:
            return tuple(sorted(self._connections))
