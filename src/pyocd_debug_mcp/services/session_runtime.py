"""Runtime session, structured event logging, and refusal primitives for R10."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNS_ROOT = REPO_ROOT / "runs"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_text() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def generate_session_id() -> str:
    timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(4)}"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


class ToolOutcome(str, Enum):
    SUCCESS = "success"
    REFUSED = "refused"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ActionContext:
    source: str
    action_name: str
    session_id: str | None = None


class PolicyRefusal(RuntimeError):
    """Raised when a shared policy refuses a mutation attempt."""

    def __init__(self, code: str, message: str, *, session_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.session_id = session_id


class WatcherBlocked(RuntimeError):
    """Raised when the convergence watcher blocks a repeated mutation pattern."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        action_family: str,
        session_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.action_family = action_family
        self.session_id = session_id


@dataclass(frozen=True)
class ToolEvent:
    event_id: str
    session_id: str | None
    timestamp: str
    tool_name: str
    board_id: str | None
    probe_uid: str | None
    route_used: str | None
    normalized_args: dict[str, Any]
    outcome_kind: ToolOutcome
    error_code: str | None
    duration_ms: int
    details: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "tool_name": self.tool_name,
            "board_id": self.board_id,
            "probe_uid": self.probe_uid,
            "route_used": self.route_used,
            "normalized_args": _jsonable(self.normalized_args),
            "outcome_kind": self.outcome_kind.value,
            "error_code": self.error_code,
            "duration_ms": self.duration_ms,
            "details": _jsonable(self.details),
        }


@dataclass
class SessionRecord:
    session_id: str
    board_id: str | None
    probe_uid: str | None
    route_used: str | None
    created_at: str
    run_root: Path
    log_path: Path
    summary_path: Path
    events: list[ToolEvent] = field(default_factory=list)
    blocked_actions: dict[str, dict[str, str]] = field(default_factory=dict)
    recover_completed: bool = False
    closed_at: str | None = None

    @property
    def event_count(self) -> int:
        return len(self.events)


class SessionStore(Protocol):
    def start_session(
        self,
        *,
        board_id: str | None,
        probe_uid: str | None,
        route_used: str | None,
    ) -> SessionRecord: ...

    def append_event(self, session: SessionRecord, event: ToolEvent) -> None: ...

    def append_global_event(self, event: ToolEvent) -> None: ...

    def set_block(
        self, session: SessionRecord, action_family: str, code: str, message: str
    ) -> None: ...

    def clear_block(self, session: SessionRecord, action_family: str) -> None: ...

    def mark_recover_completed(self, session: SessionRecord) -> None: ...

    def close_session(self, session: SessionRecord) -> None: ...


class InMemorySessionStore:
    """Single-process runtime store plus on-disk JSONL/session-summary persistence."""

    def __init__(self, runs_root: Path = RUNS_ROOT) -> None:
        self._runs_root = runs_root
        self._global_events_path = self._runs_root / "server-events.jsonl"
        self._sessions: dict[str, SessionRecord] = {}
        self._global_event_count = 0

    def start_session(
        self,
        *,
        board_id: str | None,
        probe_uid: str | None,
        route_used: str | None,
    ) -> SessionRecord:
        session_id = generate_session_id()
        run_root = self._runs_root / session_id
        for relative in ("logs", "captured-serial", "applied-patches", "run-metadata"):
            (run_root / relative).mkdir(parents=True, exist_ok=True)
        log_path = run_root / "logs" / "events.jsonl"
        summary_path = run_root / "run-metadata" / "session.json"
        record = SessionRecord(
            session_id=session_id,
            board_id=board_id,
            probe_uid=probe_uid,
            route_used=route_used,
            created_at=utc_now_text(),
            run_root=run_root,
            log_path=log_path,
            summary_path=summary_path,
        )
        self._sessions[session_id] = record
        self._write_summary(record)
        return record

    def append_event(self, session: SessionRecord, event: ToolEvent) -> None:
        session.events.append(event)
        self._append_jsonl(session.log_path, event.to_record())
        self._write_summary(session)

    def append_global_event(self, event: ToolEvent) -> None:
        self._global_event_count += 1
        self._append_jsonl(self._global_events_path, event.to_record())

    def set_block(
        self, session: SessionRecord, action_family: str, code: str, message: str
    ) -> None:
        session.blocked_actions[action_family] = {"code": code, "message": message}
        self._write_summary(session)

    def clear_block(self, session: SessionRecord, action_family: str) -> None:
        if action_family in session.blocked_actions:
            session.blocked_actions.pop(action_family, None)
            self._write_summary(session)

    def mark_recover_completed(self, session: SessionRecord) -> None:
        session.recover_completed = True
        self._write_summary(session)

    def close_session(self, session: SessionRecord) -> None:
        session.closed_at = utc_now_text()
        self._write_summary(session)
        self._sessions.pop(session.session_id, None)

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_jsonable(record), sort_keys=True))
            handle.write("\n")

    def _write_summary(self, session: SessionRecord) -> None:
        summary = {
            "session_id": session.session_id,
            "board_id": session.board_id,
            "probe_uid": session.probe_uid,
            "route_used": session.route_used,
            "created_at": session.created_at,
            "closed_at": session.closed_at,
            "event_count": session.event_count,
            "blocked_actions": session.blocked_actions,
            "recover_completed": session.recover_completed,
            "log_path": session.log_path,
        }
        session.summary_path.parent.mkdir(parents=True, exist_ok=True)
        session.summary_path.write_text(
            json.dumps(_jsonable(summary), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
