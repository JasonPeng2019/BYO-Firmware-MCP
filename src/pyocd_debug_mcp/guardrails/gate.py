"""Run-scoped, default-closed validation and write-gate lifecycle."""

from __future__ import annotations

import threading
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

_PASSED_RESULTS: Final = frozenset(
    {"validation_passed", "validation_passed_uart_not_configured"}
)


class GateRefusal(RuntimeError):
    """A validated session or fresh write gate is unavailable."""

    def __init__(self, code: str, message: str, *, remedy: tuple[str, ...]) -> None:
        self.code = code
        self.remedy = remedy
        rendered = " then ".join(remedy)
        super().__init__(f"{message} Required remedy: {rendered}.")


@dataclass(frozen=True, slots=True)
class ValidationStamp:
    """One run-scoped live identity proof bound to one current safety map.

    ``aggregate_fingerprint`` remains as a read-only compatibility alias while callers migrate to
    the truthful ``map_digest`` name.  Refresh may replace only ``map_digest``; it may never create
    the live identity fields in this record.
    """

    board_id: str
    connection_id: str
    hardware_result: str
    probe_identity: str
    observed_identity: str
    map_digest: str
    validated_at: str

    @property
    def aggregate_fingerprint(self) -> str:
        return self.map_digest


class GateManager:
    """Own validation stamps for one Server Run; no state is serializable by this API."""

    def __init__(
        self,
        state: MutableMapping[object, object] | None = None,
    ) -> None:
        self._state = state if state is not None else {}
        self._guard = threading.RLock()
        self._closure_reasons: dict[str, str] = {}

    @staticmethod
    def _required(value: str, label: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{label} must be non-empty")
        return normalized

    def stamp_validation(
        self,
        *,
        board_id: str,
        connection_id: str,
        hardware_result: str,
        probe_identity: str,
        aggregate_fingerprint: str,
        observed_identity: str | None = None,
    ) -> ValidationStamp:
        """Create or replace a stamp only for a completed successful validation."""

        board = self._required(board_id, "board_id")
        connection = self._required(connection_id, "connection_id")
        probe = self._required(probe_identity, "probe_identity")
        fingerprint = self._required(aggregate_fingerprint, "aggregate_fingerprint")
        identity = self._required(observed_identity or "reviewed_identity_match", "observed_identity")
        if hardware_result not in _PASSED_RESULTS:
            raise ValueError("only a successful board_validate result may stamp a gate")
        stamp = ValidationStamp(
            board,
            connection,
            hardware_result,
            probe,
            identity,
            fingerprint,
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        with self._guard:
            self._state[board] = stamp
            self._closure_reasons.pop(board, None)
        return stamp

    def snapshot(self, board_id: str) -> ValidationStamp | None:
        board = self._required(board_id, "board_id")
        with self._guard:
            value = self._state.get(board)
            return value if isinstance(value, ValidationStamp) else None

    def clear(self, board_id: str, reason: str) -> ValidationStamp | None:
        board = self._required(board_id, "board_id")
        with self._guard:
            value = self._state.pop(board, None)
            self._closure_reasons[board] = reason.strip() or "gate closed"
            return value if isinstance(value, ValidationStamp) else None

    def clear_connection(self, connection_id: str, reason: str) -> tuple[str, ...]:
        connection = self._required(connection_id, "connection_id")
        with self._guard:
            boards = tuple(
                sorted(
                    str(board)
                    for board, value in self._state.items()
                    if isinstance(value, ValidationStamp)
                    and value.connection_id == connection
                )
            )
            for board in boards:
                self._state.pop(board, None)
                self._closure_reasons[board] = reason.strip() or "connection closed"
            return boards

    def require_validated(self, board_id: str, connection_id: str) -> ValidationStamp:
        board = self._required(board_id, "board_id")
        connection = self._required(connection_id, "connection_id")
        stamp = self.snapshot(board)
        if stamp is None:
            reason = self._closure_reasons.get(board, "this Server Run has no validation stamp")
            raise GateRefusal(
                "gate/validation-required",
                f"Board '{board}' is not validated for this connection ({reason}).",
                remedy=("board_validate",),
            )
        if stamp.connection_id != connection:
            self.clear(board, "connection identity changed")
            raise GateRefusal(
                "gate/connection-changed",
                f"Board '{board}' was validated on a different connection.",
                remedy=("board_validate",),
            )
        return stamp

    def require_write(
        self,
        board_id: str,
        connection_id: str,
        current_aggregate_fingerprint: str,
    ) -> ValidationStamp:
        stamp = self.require_validated(board_id, connection_id)
        current = self._required(
            current_aggregate_fingerprint, "current_aggregate_fingerprint"
        )
        if stamp.aggregate_fingerprint != current:
            self.clear(board_id, "stable memory map changed")
            raise GateRefusal(
                "gate/configuration-stale",
                f"Board '{board_id}' stable memory map changed after validation.",
                remedy=("board_safety_refresh",),
            )
        return stamp

    def refresh_fingerprint(
        self,
        board_id: str,
        connection_id: str,
        aggregate_fingerprint: str,
    ) -> ValidationStamp | None:
        """Update an already-valid stamp; never create or reopen one."""

        try:
            current = self.require_validated(board_id, connection_id)
        except GateRefusal:
            return None
        refreshed = ValidationStamp(
            current.board_id,
            current.connection_id,
            current.hardware_result,
            current.probe_identity,
            current.observed_identity,
            self._required(aggregate_fingerprint, "aggregate_fingerprint"),
            current.validated_at,
        )
        with self._guard:
            if self._state.get(current.board_id) == current:
                self._state[current.board_id] = refreshed
                return refreshed
        return None
