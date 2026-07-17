"""Per-process state for one Server Run."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{timestamp}-{secrets.token_hex(4)}"


@dataclass(slots=True)
class ServerRun:
    """Authority-bearing state that must never survive a process restart."""

    run_id: str = field(default_factory=_new_run_id)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    plans: dict[object, Any] = field(default_factory=dict)
    permissions: dict[object, Any] = field(default_factory=dict)
    assignments: dict[object, Any] = field(default_factory=dict)
    gates: dict[object, Any] = field(default_factory=dict)

    @property
    def started_at_text(self) -> str:
        """Return an unambiguous UTC timestamp for diagnostics."""

        return self.started_at.isoformat().replace("+00:00", "Z")

    def clear_authority(self) -> None:
        """Clear all run-scoped authority without touching durable evidence."""

        self.plans.clear()
        self.permissions.clear()
        self.assignments.clear()
        self.gates.clear()


def create_server_run() -> ServerRun:
    """Create a fresh, empty process-local Server Run."""

    return ServerRun()
