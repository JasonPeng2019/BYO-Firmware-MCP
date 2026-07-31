"""Bounded, board-scoped record of recent tool activity, attached to every report.

Distinct from the ledger: this is *context for a report*, capped at a fixed number
of events, while the ledger records occasions -- snapshots, check-ins, reports,
boot and close -- and never one entry per call. Per-call sequence context lives
here and nowhere else, which is deliberate: problem-watching needs the run-up to a
failure, not a permanent record of every individual call.

``TRAIL_MAX_EVENTS`` is its own knob. It shares the value 100 with the usage
snapshot cadence in ``counters`` today, but they are different quantities and are
never collapsed into one constant: raising the snapshot cadence must not silently
resize this buffer.

Board scoping is not a nicety. Different boards execute concurrently by design, so
a global trail would attach board B's activity to board A's report and make both
untriageable.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

TRAIL_MAX_EVENTS = 100


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class TrailEntry:
    """One observed call, recorded as identifiers and fingerprints only."""

    ts: str
    tool: str
    board: str | None
    connection: str | None
    args_fp: str
    outcome: str
    error_class: str | None
    remedy: str | None
    guard_transition: str | None
    duration_ms: int | None

    def to_record(self) -> dict[str, object]:
        return asdict(self)


class BoardTrail:
    """Per-board ring buffers of recent activity."""

    def __init__(self, max_events: int = TRAIL_MAX_EVENTS) -> None:
        self._max = max_events
        self._guard = threading.Lock()
        self._buffers: dict[str | None, deque[TrailEntry]] = {}

    def append(
        self,
        *,
        tool: str,
        board: str | None,
        connection: str | None,
        args_fp: str,
        outcome: str,
        error_class: str | None = None,
        remedy: str | None = None,
        guard_transition: str | None = None,
        duration_ms: int | None = None,
    ) -> TrailEntry:
        entry = TrailEntry(
            ts=_timestamp(),
            tool=tool,
            board=board,
            connection=connection,
            args_fp=args_fp,
            outcome=outcome,
            error_class=error_class,
            remedy=remedy,
            guard_transition=guard_transition,
            duration_ms=duration_ms,
        )
        with self._guard:
            buffer = self._buffers.get(board)
            if buffer is None:
                buffer = deque(maxlen=self._max)
                self._buffers[board] = buffer
            buffer.append(entry)
        return entry

    def for_board(self, board: str | None) -> tuple[TrailEntry, ...]:
        """Return only the named board's activity, never another board's."""

        with self._guard:
            buffer = self._buffers.get(board)
            return tuple(buffer) if buffer is not None else ()

    def records_for(self, board: str | None) -> list[dict[str, object]]:
        return [entry.to_record() for entry in self.for_board(board)]


__all__ = ["TRAIL_MAX_EVENTS", "BoardTrail", "TrailEntry"]
