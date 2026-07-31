"""Live per-run call counters, held in the server rather than read back from logs.

The direction matters: the log is a record *of* the counter, not a summary of the
log. That is what keeps counting correct when the store is unbound or buffering,
and what keeps the health check honest when delivery is broken.

These counts are not authority-bearing and nothing clears them. A disconnect, a
gate closure, a plan expiry, and a run-scoped authority reset all leave them
intact -- the activity still happened, and a report produced after a disconnect
must still reflect it. There is deliberately no ``reset`` method; its absence is
the enforcement.

Because nothing resets them mid-run, every read is a **cumulative run-to-date
total**, never a since-the-last-tick delta. That is the anti-under-report
property: a snapshot that is dropped, delayed, or never delivered cannot lower
the total the next one carries. See ``ledger`` for the honest bound on what that
does and does not defend against.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Two cadences, deliberately separate constants even though a third quantity --
# the trail buffer size in ``trail.TRAIL_MAX_EVENTS`` -- happens to share the
# value 100 today. They are different knobs: raising the snapshot cadence must
# never silently resize the trail, so the values are never collapsed into one
# name because they currently coincide.
USAGE_SNAPSHOT_CADENCE = 100  # usage snapshot + segment roll, every build
CHECKIN_CADENCE = 500  # agent check-in prompt, personal builds only

# Test-only overrides. These exist for the same reason the delivery seam accepts
# an injected test transport: an end-to-end test has to be able to reach the
# snapshot and check-in paths against a real server process without making 100 or
# 500 real tool calls. The constants above remain the values; an absent, unparsable,
# or non-positive override changes nothing.
SNAPSHOT_CADENCE_ENV = "BYO_MCP_SNAPSHOT_CADENCE"
CHECKIN_CADENCE_ENV = "BYO_MCP_CHECKIN_CADENCE"


def _resolve(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def resolve_snapshot_cadence() -> int:
    """Return the usage-snapshot cadence, honouring the test-only override."""

    return _resolve(SNAPSHOT_CADENCE_ENV, USAGE_SNAPSHOT_CADENCE)


def resolve_checkin_cadence() -> int:
    """Return the check-in cadence, honouring the test-only override."""

    return _resolve(CHECKIN_CADENCE_ENV, CHECKIN_CADENCE)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class CountersSnapshot:
    """An immutable copy-out. Reading counters must never block a tool call."""

    total: int
    per_tool: dict[str, int]
    per_outcome: dict[str, int]
    per_error_class: dict[str, int]
    first_at: str | None
    last_at: str | None
    total_appended: int
    exercised: tuple[str, ...]
    never_exercised: tuple[str, ...]
    last_write_error: str | None
    write_failures: int


@dataclass
class RunCounters:
    """Process-local counts for one Server Run, counted from zero after a restart."""

    _guard: threading.Lock = field(default_factory=threading.Lock)
    _total: int = 0
    _per_tool: dict[str, int] = field(default_factory=dict)
    _per_outcome: dict[str, int] = field(default_factory=dict)
    _per_error: dict[str, int] = field(default_factory=dict)
    _first_at: str | None = None
    _last_at: str | None = None
    _total_appended: int = 0
    _advertised: tuple[str, ...] = ()
    _last_write_error: str | None = None
    _write_failures: int = 0

    def record(self, tool: str, outcome: str, error_class: str | None) -> int:
        """Count one observed call and return the new running total."""

        with self._guard:
            self._total += 1
            self._per_tool[tool] = self._per_tool.get(tool, 0) + 1
            self._per_outcome[outcome] = self._per_outcome.get(outcome, 0) + 1
            if error_class:
                self._per_error[error_class] = self._per_error.get(error_class, 0) + 1
            now = _timestamp()
            if self._first_at is None:
                self._first_at = now
            self._last_at = now
            return self._total

    def note_appended(self) -> None:
        """Bump the durable monotonic total of records that reached disk.

        Reconciliation keys off this, never off the number of files currently
        resident: delivered files delete themselves, so a large gap between the
        counter and the resident record count is expected and is not degradation.
        """

        with self._guard:
            self._total_appended += 1

    def note_write_failure(self, detail: str) -> None:
        with self._guard:
            self._write_failures += 1
            self._last_write_error = detail

    def set_advertised(self, names: tuple[str, ...]) -> None:
        with self._guard:
            self._advertised = tuple(names)

    def snapshot(self) -> CountersSnapshot:
        with self._guard:
            exercised = tuple(sorted(self._per_tool))
            never = tuple(sorted(set(self._advertised) - set(self._per_tool)))
            return CountersSnapshot(
                total=self._total,
                per_tool=dict(self._per_tool),
                per_outcome=dict(self._per_outcome),
                per_error_class=dict(self._per_error),
                first_at=self._first_at,
                last_at=self._last_at,
                total_appended=self._total_appended,
                exercised=exercised,
                never_exercised=never,
                last_write_error=self._last_write_error,
                write_failures=self._write_failures,
            )


__all__ = [
    "CHECKIN_CADENCE",
    "CHECKIN_CADENCE_ENV",
    "SNAPSHOT_CADENCE_ENV",
    "USAGE_SNAPSHOT_CADENCE",
    "CountersSnapshot",
    "RunCounters",
    "resolve_checkin_cadence",
    "resolve_snapshot_cadence",
]
