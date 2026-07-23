"""Per-process state for one Server Run."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{timestamp}-{secrets.token_hex(4)}"


@dataclass(slots=True)
class ServerRun:
    """Process-local identity, assignment, and diagnostics for one server lifetime."""

    run_id: str = field(default_factory=_new_run_id)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Board-to-physical-connection assignments prevent cross-board session reuse;
    # they are correctness state, never a prerequisite for another operation.
    assignments: dict[object, object] = field(default_factory=dict)

    @property
    def started_at_text(self) -> str:
        """Return an unambiguous UTC timestamp for diagnostics."""

        return self.started_at.isoformat().replace("+00:00", "Z")


def create_server_run() -> ServerRun:
    """Create a fresh, empty process-local Server Run."""

    return ServerRun()
