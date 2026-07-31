"""The remote-logging staleness backstop.

Invisible hardware work is the core harm this reporter exists to prevent: a board
worked on with no record hides exactly the issues that work may have caused. So
once remote logging has gone unconfirmed for too long, the system stops doing more
unauditable hardware work.

This is the one place monitoring holds authority, and it is granted deliberately.
Everywhere else monitoring is strictly passive and fails open; here, and only
here, monitoring state refuses dispatch.

Two guards keep it from misfiring:

* **No anchor means dormant.** With no delivery ever confirmed on this install
  there is nothing to measure against, so the block does not arm. Reading a
  missing anchor as infinitely stale would brick a fresh install on its first
  operation.
* **An unusable clock never trips it.** Failing open on a dead real-time clock is
  preferred to bricking a bench machine.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

STALENESS_THRESHOLD = timedelta(days=14)
ANCHOR_FILENAME = "delivery_anchor.json"

BLOCK_MESSAGE = (
    "Guarded hardware work is paused because this server has not been able to "
    "deliver its activity log for {days} days. Restore network connectivity so the "
    "pending log can be delivered; the next confirmed delivery clears this "
    "automatically. Reporting, check-in, and health-check actions remain available."
)


class BlockState(str, Enum):
    DORMANT = "dormant"
    ARMED = "armed"
    TRIPPED = "tripped"
    CLOCK_UNUSABLE = "clock_unusable"


@dataclass(frozen=True, slots=True)
class Anchor:
    """The most recent confirmed delivery, carried across runs."""

    at: datetime
    transport: str
    origin: str

    def to_record(self) -> dict[str, str]:
        return {
            "at": self.at.isoformat().replace("+00:00", "Z"),
            "transport": self.transport,
            "origin": self.origin,
        }


def _anchor_path(server_data: Path) -> Path:
    return server_data / ANCHOR_FILENAME


def load_anchor(server_data: Path | None) -> Anchor | None:
    if server_data is None:
        return None
    try:
        raw = json.loads(_anchor_path(server_data).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    text = raw.get("at")
    if not isinstance(text, str):
        return None
    try:
        at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return Anchor(
        at=at,
        transport=str(raw.get("transport", "unknown")),
        origin=str(raw.get("origin", "unknown")),
    )


def store_anchor(server_data: Path | None, anchor: Anchor) -> bool:
    if server_data is None:
        return False
    try:
        server_data.mkdir(parents=True, exist_ok=True)
        _anchor_path(server_data).write_text(
            json.dumps(anchor.to_record(), sort_keys=True), encoding="utf-8"
        )
    except OSError:
        return False
    return True


def evaluate(
    anchor: Anchor | None,
    now: datetime | None,
    threshold: timedelta = STALENESS_THRESHOLD,
) -> BlockState:
    """Return the block state. Checks for an anchor *before* computing elapsed time."""

    if anchor is None:
        # Bootstrap: nothing has ever been delivered on this install.
        return BlockState.DORMANT
    if now is None:
        return BlockState.CLOCK_UNUSABLE
    try:
        elapsed = now - anchor.at
    except (TypeError, OverflowError):
        return BlockState.CLOCK_UNUSABLE
    if elapsed < timedelta(0):
        # Clock behind the anchor: unusable, not stale.
        return BlockState.CLOCK_UNUSABLE
    return BlockState.TRIPPED if elapsed >= threshold else BlockState.ARMED


class StalenessBlock:
    """Holds the anchor in memory so the dispatch-path check does no file I/O.

    The check runs inside a held board lock on every guarded call, so reading the
    anchor from disk there would put file I/O on the hot path. The delivery thread
    updates this in-memory copy whenever it writes a new anchor.
    """

    def __init__(self, server_data: Path | None) -> None:
        self._server_data = server_data
        self._guard = threading.Lock()
        self._anchor = load_anchor(server_data)

    @property
    def anchor(self) -> Anchor | None:
        with self._guard:
            return self._anchor

    def refresh(self, transport: str, origin: str, at: datetime) -> None:
        """Re-anchor the interval after a confirmed delivery."""

        anchor = Anchor(at=at, transport=transport, origin=origin)
        with self._guard:
            self._anchor = anchor
        store_anchor(self._server_data, anchor)

    def inject(self, anchor: Anchor | None) -> None:
        """Test hook: drive the block without waiting real days."""

        with self._guard:
            self._anchor = anchor

    def state(self, now: datetime | None = None) -> BlockState:
        if now is None:
            try:
                now = datetime.now(timezone.utc)
            except (OSError, OverflowError, ValueError):
                now = None
        return evaluate(self.anchor, now)

    def describe(self) -> dict[str, object]:
        anchor = self.anchor
        state = self.state()
        stale_days: float | None = None
        if anchor is not None and state in (BlockState.ARMED, BlockState.TRIPPED):
            stale_days = round(
                (datetime.now(timezone.utc) - anchor.at).total_seconds() / 86400.0, 2
            )
        return {
            "state": state.value,
            "threshold_days": STALENESS_THRESHOLD.days,
            "anchor_at": anchor.at.isoformat().replace("+00:00", "Z") if anchor else None,
            "anchor_origin": anchor.origin if anchor else None,
            "anchor_transport": anchor.transport if anchor else None,
            "stale_days": stale_days,
        }

    def refusal_message(self) -> str:
        anchor = self.anchor
        days = STALENESS_THRESHOLD.days
        if anchor is not None:
            days = max(
                days, int((datetime.now(timezone.utc) - anchor.at).total_seconds() // 86400)
            )
        return BLOCK_MESSAGE.format(days=days)


__all__ = [
    "ANCHOR_FILENAME",
    "BLOCK_MESSAGE",
    "STALENESS_THRESHOLD",
    "Anchor",
    "BlockState",
    "StalenessBlock",
    "evaluate",
    "load_anchor",
    "store_anchor",
]
