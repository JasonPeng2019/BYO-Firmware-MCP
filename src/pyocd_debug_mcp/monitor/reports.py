"""Report construction, restart-stable grouping, and noise control.

Every field except the model-authored narrative is supplied by the server. Model
output is untrusted: it may be malformed, oversized, or simply wrong, and it never
supplies the trail, the guard state, the board scope, the grouping identity, or
the environment.

Grouping deliberately excludes the run id -- it changes on every restart, so
including it would defeat deduplication entirely -- and also the board id, so a
single unplugged probe produces one grouped report rather than one per board.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from datetime import datetime, timezone
from hashlib import blake2b
from typing import Any

from pyocd_debug_mcp.monitor.classify import (
    SEVERITY_FOR_SIGNAL,
    TRIAGE_FOR_SIGNAL,
    Signal,
    TriageClass,
)
from pyocd_debug_mcp.monitor.redaction import report_fingerprint

REPORT_SCHEMA_VERSION = 1
DEDUPE_WINDOW_SECONDS = 300.0


def report_identity(report: Mapping[str, Any]) -> str | None:
    """Return a filesystem-safe report/summary identity, or ``None``.

    Durable bodies can survive crashes and are therefore untrusted when read
    back. Keep their identity a single portable filename component before any
    transport constructs a destination path from it.
    """

    raw = report.get("report_id") or report.get("summary_id")
    if not isinstance(raw, str) or not raw or len(raw) > 128:
        return None
    if raw in {".", ".."} or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in raw
    ):
        return None
    return raw


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def grouping_key(signal: Signal, triage: TriageClass, tool: str | None, anchor: str) -> str:
    """Return a fingerprint that is stable across Server Runs.

    Restart independence is the requirement: the run id changes every restart and
    must not enter the key, or duplicates of one underlying issue would flood the
    sink instead of collapsing.
    """

    material = "|".join([signal.value, triage.value, tool or "-", anchor])
    return blake2b(material.encode("utf-8", "replace"), digest_size=16).hexdigest()


class Origin(str, Enum):
    """Which detection path produced a report."""

    SERVER_AUTO = "server-auto"
    SERVER_THRASH = "server-thrash-detector"
    MODEL_SKILL = "model-skill"


class Deduper:
    """Debounce per grouping key so one loop cannot storm the sink."""

    def __init__(self, window_seconds: float = DEDUPE_WINDOW_SECONDS) -> None:
        self._window = window_seconds
        self._guard = threading.Lock()
        self._last_emit: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

    def admit(self, key: str) -> tuple[bool, int]:
        """Return whether to emit, plus how many were suppressed since last emit."""

        now = time.monotonic()
        with self._guard:
            last = self._last_emit.get(key)
            if last is None or (now - last) >= self._window:
                suppressed = self._suppressed.pop(key, 0)
                self._last_emit[key] = now
                return True, suppressed
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return False, self._suppressed[key]

    def withdraw(self, key: str, suppressed: int) -> None:
        """Undo an emission reservation whose body could not be persisted."""

        with self._guard:
            self._last_emit.pop(key, None)
            if suppressed:
                self._suppressed[key] = self._suppressed.get(key, 0) + suppressed


@dataclass(frozen=True, slots=True)
class ReportEnvironment:
    server_version: str
    python_version: str
    platform: str
    narrative_logging: str
    provider: str | None = None


def build_report(
    *,
    signal: Signal,
    origin: str,
    tool: str | None,
    board: str | None,
    anchor: str,
    title: str,
    description: str,
    run_id: str,
    run_started_at: str,
    workspace_token: str | None,
    trail: list[dict[str, Any]],
    guard_state: dict[str, Any],
    board_scope: dict[str, Any],
    environment: ReportEnvironment,
    usage: dict[str, Any],
    refusal_code: str | None = None,
    named_remedy: str | None = None,
    args_fp: str | None = None,
    narrative: dict[str, Any] | None = None,
    suppressed_since_last: int = 0,
) -> dict[str, Any]:
    """Assemble one report. Content only -- serialization belongs to the transport."""

    triage = TRIAGE_FOR_SIGNAL.get(signal, TriageClass.SERVER_DEFECT)
    report_id = f"rpt-{secrets.token_hex(8)}"
    scoped_trail: list[dict[str, Any]] = []
    for entry in trail:
        scoped = dict(entry)
        entry_fingerprint = scoped.get("args_fp")
        if isinstance(entry_fingerprint, str):
            scoped["args_fp"] = report_fingerprint(entry_fingerprint, report_id)
        connection_fingerprint = scoped.get("connection")
        if isinstance(connection_fingerprint, str):
            scoped["connection"] = report_fingerprint(connection_fingerprint, report_id)
        scoped_trail.append(scoped)
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_id": report_id,
        "timestamp": _timestamp(),
        "session_id": run_id,
        "run_started_at": run_started_at,
        "signal_type": signal.value,
        "severity": SEVERITY_FOR_SIGNAL.get(signal, "warning"),
        "origin": origin,
        "triage_class": triage.value,
        "title": title,
        "description": description,
        "tool_name": tool,
        "error_signature": anchor,
        "refusal_code": refusal_code,
        "named_remedy": named_remedy,
        "args_fingerprint": (
            report_fingerprint(args_fp, report_id) if args_fp is not None else None
        ),
        "board_scope": board_scope,
        "workspace_token": workspace_token,
        "guard_state": guard_state,
        # The run's cumulative counts at the moment of the report, so a report is
        # self-describing about how much activity surrounded the failure. Same
        # numbers the periodic usage snapshot carries, read from the same live
        # counter -- not a second accumulator kept for reports.
        "usage": usage,
        "trail": scoped_trail,
        "grouping_key": grouping_key(signal, triage, tool, anchor),
        "suppressed_since_last": suppressed_since_last,
        "environment": {
            "server_version": environment.server_version,
            "python_version": environment.python_version,
            "platform": environment.platform,
            "narrative_logging": environment.narrative_logging,
            "provider": environment.provider,
        },
    }
    if narrative is not None:
        report["narrative"] = narrative
    return report


def build_summary(
    *,
    run_id: str,
    run_started_at: str,
    uptime_seconds: float,
    trigger: str,
    counters: dict[str, Any],
    coverage: dict[str, Any],
    ledger: dict[str, Any],
    delivery: dict[str, Any],
    environment: ReportEnvironment,
    narrative: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a health record.

    A summary carries no severity, no signal type, and no grouping identity,
    because it is not an issue. Anything a summary reveals that *is* an issue is
    filed separately through the report path.
    """

    summary: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "summary_id": f"sum-{secrets.token_hex(8)}",
        "record_type": "summary",
        "timestamp": _timestamp(),
        "session_id": run_id,
        "run_started_at": run_started_at,
        "uptime_seconds": round(uptime_seconds, 3),
        "trigger": trigger,
        "activity": counters,
        "coverage": coverage,
        "ledger": ledger,
        "delivery": delivery,
        "environment": {
            "server_version": environment.server_version,
            "narrative_logging": environment.narrative_logging,
        },
    }
    if narrative is not None:
        summary["agent_narrative"] = narrative
    return summary


__all__ = [
    "DEDUPE_WINDOW_SECONDS",
    "REPORT_SCHEMA_VERSION",
    "Deduper",
    "Origin",
    "ReportEnvironment",
    "build_report",
    "build_summary",
    "grouping_key",
]
