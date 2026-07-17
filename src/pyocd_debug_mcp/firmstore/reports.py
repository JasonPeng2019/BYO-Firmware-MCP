"""Immutable setup and validation reports with append-only JSONL event logs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pyocd_debug_mcp.firmstore.store import FirmStore, ensure_no_persisted_authority

REPORT_SCHEMA_VERSION = 1
_RESERVED_REPORT_FIELDS = frozenset({"schema_version", "report_type", "attempt_id", "created_at"})
_RESERVED_EVENT_FIELDS = frozenset({"recorded_at"})


class ReportError(ValueError):
    """A report or event did not satisfy the artifact contract."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ReportPaths:
    report: Path
    events: Path


class ReportWriter:
    """Create one report per attempt while allowing only event-log appends."""

    def __init__(self, store: FirmStore) -> None:
        self.store = store

    def paths(
        self,
        report_type: Literal["setup", "validation", "target_unlock"],
        attempt_id: str,
    ) -> ReportPaths:
        if report_type == "setup":
            root = self.store.layout.setup_attempt(attempt_id)
        elif report_type == "validation":
            root = self.store.layout.validation_attempt(attempt_id)
        elif report_type == "target_unlock":
            root = self.store.layout.validation_attempt(attempt_id)
        else:  # pragma: no cover - protected by the public type and runtime validation
            raise ReportError(f"Unsupported report type: {report_type}")
        return ReportPaths(root / "report.json", root / "events.jsonl")

    def create(
        self,
        report_type: Literal["setup", "validation", "target_unlock"],
        attempt_id: str,
        fields: Mapping[str, Any],
    ) -> ReportPaths:
        conflicting = sorted(set(fields) & _RESERVED_REPORT_FIELDS)
        if conflicting:
            raise ReportError(f"Report fields must not replace reserved fields: {conflicting}")
        ensure_no_persisted_authority(fields, location=f"{report_type} report")
        paths = self.paths(report_type, attempt_id)
        document = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "report_type": report_type,
            "attempt_id": attempt_id,
            "created_at": _timestamp(),
            **dict(fields),
        }
        self.store.atomic_create_json(paths.report, document)
        return paths

    def append_event(
        self,
        report_type: Literal["setup", "validation", "target_unlock"],
        attempt_id: str,
        event: Mapping[str, Any],
    ) -> Path:
        conflicting = sorted(set(event) & _RESERVED_EVENT_FIELDS)
        if conflicting:
            raise ReportError(f"Event fields must not replace reserved fields: {conflicting}")
        ensure_no_persisted_authority(event, location=f"{report_type} report event")
        paths = self.paths(report_type, attempt_id)
        return self.store.append_jsonl(paths.events, {"recorded_at": _timestamp(), **dict(event)})

    def create_setup(self, attempt_id: str, fields: Mapping[str, Any]) -> ReportPaths:
        return self.create("setup", attempt_id, fields)

    def append_setup_event(self, attempt_id: str, event: Mapping[str, Any]) -> Path:
        return self.append_event("setup", attempt_id, event)

    def create_validation(self, attempt_id: str, fields: Mapping[str, Any]) -> ReportPaths:
        return self.create("validation", attempt_id, fields)

    def append_validation_event(self, attempt_id: str, event: Mapping[str, Any]) -> Path:
        return self.append_event("validation", attempt_id, event)

    def create_target_unlock(
        self, attempt_id: str, fields: Mapping[str, Any]
    ) -> ReportPaths:
        return self.create("target_unlock", attempt_id, fields)

    def append_target_unlock_event(
        self, attempt_id: str, event: Mapping[str, Any]
    ) -> Path:
        return self.append_event("target_unlock", attempt_id, event)
