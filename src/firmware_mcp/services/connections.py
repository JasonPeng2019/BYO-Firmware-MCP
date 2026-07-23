"""In-memory ownership and serialization for active board connections."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from firmware_mcp.adapters.debug_interface import TargetSessionHandle, session_metadata
from firmware_mcp.services.session_runtime import SessionRecord


class ConnectionAssignmentError(RuntimeError):
    """Raised when a board or physical connection is already assigned."""


class BoardNotConnectedError(RuntimeError):
    """Raised when an operation names a board without an active connection."""


def stable_connection_identity(handle: TargetSessionHandle) -> str:
    """Return an immutable identity for a live connection.

    A probe UID is the preferred physical identity. When a provider exposes no
    UID, the frozen runtime token identifies only this live worker/session and
    is deliberately not stable across reconnects.
    """

    metadata = session_metadata(handle)
    probe_uid = (metadata.probe_uid or "").strip()
    if probe_uid:
        return f"probe:{probe_uid.casefold()}"
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
                f"Call connect_board(board_id='{normalized}') first."
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
