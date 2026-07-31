"""Deterministic repetition detection, with this server's legitimate loops excluded.

A model that is thrashing cannot reliably notice it is thrashing -- behavioural
self-detection misses loops exactly when one is happening -- so this runs
server-side and does not depend on the agent's self-awareness.

The risk is the mirror image: this server has many *correct* repetition patterns,
and a naive detector would become a false-positive engine. Repetition alone is not
thrashing. Repetition with an identical outcome and no state transition is.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

THRESHOLD = 4
WINDOW_SECONDS = 60.0

# Polling and waiting are how an agent legitimately watches hardware. The
# monitoring actions are here too: they touch no hardware, so calling them
# repeatedly is never a hardware loop, and reporting it would be noise about the
# reporter itself.
_POLLING_TOOLS = frozenset(
    {
        "get_state",
        "read_execution_state",
        "get_setup_status",
        "wait",
        "setup_overview",
        "server_health_check",
        "report_agent_issue",
        "submit_routine_checkin",
        "initialization_handshake",
    }
)

# A board-busy or deadline outcome is a retry invitation, not a loop.
_RETRY_INVITING = frozenset(
    {
        "runtime/BoardBusyError",
        "runtime/OperationTimeoutError",
    }
)

# The deliberate refresh-then-validate sequence.
_PAIRED_SEQUENCE = ("board_safety_refresh", "board_validate")


@dataclass
class _Bucket:
    events: deque[tuple[float, str, str, str]] = field(default_factory=deque)
    reported_at: float | None = None


class ThrashDetector:
    """Fire once per genuine loop, and never on a legitimate repetition pattern."""

    def __init__(
        self, threshold: int = THRESHOLD, window_seconds: float = WINDOW_SECONDS
    ) -> None:
        self._threshold = threshold
        self._window = window_seconds
        self._guard = threading.Lock()
        self._buckets: dict[tuple[str | None, str, str], _Bucket] = {}
        self._last_tool: dict[str | None, str] = {}
        self._accepted_response_fp: dict[str | None, str] = {}

    def note_accepted_response(self, board: str | None, args_fp: str) -> None:
        """Record the fingerprint the server itself told the agent to resubmit."""

        with self._guard:
            self._accepted_response_fp[board] = args_fp

    def _excluded_locked(
        self, board: str | None, tool: str, args_fp: str, error_class: str | None
    ) -> bool:
        if tool in _POLLING_TOOLS:
            return True
        if tool.endswith("-plan"):
            # The all-NULL call and the populated submission are the same tool
            # twice by design; their arguments differ, but exclude explicitly so a
            # later change cannot regress this.
            return True
        if error_class in _RETRY_INVITING:
            return True
        if tool == "board_validate" and self._accepted_response_fp.get(board) == args_fp:
            return True
        previous = self._last_tool.get(board)
        if (previous, tool) == _PAIRED_SEQUENCE:
            return True
        return False

    def observe(
        self,
        *,
        board: str | None,
        tool: str,
        args_fp: str,
        outcome: str,
        error_class: str | None,
        guard_fp: str,
    ) -> bool:
        """Return True exactly once when a genuine loop crosses the threshold."""

        now = time.monotonic()
        with self._guard:
            excluded = self._excluded_locked(board, tool, args_fp, error_class)
            self._last_tool[board] = tool
            if excluded:
                return False
            key = (board, tool, args_fp)
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket()
                self._buckets[key] = bucket
            cutoff = now - self._window
            while bucket.events and bucket.events[0][0] < cutoff:
                bucket.events.popleft()
            signature = f"{outcome}|{error_class or '-'}"
            bucket.events.append((now, signature, guard_fp, args_fp))
            if len(bucket.events) < self._threshold:
                return False
            signatures = {event[1] for event in bucket.events}
            guards = {event[2] for event in bucket.events}
            if len(signatures) != 1 or len(guards) != 1:
                # The outcome changed or state moved: progress, not a loop.
                return False
            if bucket.reported_at is not None and (now - bucket.reported_at) < self._window:
                return False
            bucket.reported_at = now
            return True


__all__ = ["THRESHOLD", "WINDOW_SECONDS", "ThrashDetector"]
