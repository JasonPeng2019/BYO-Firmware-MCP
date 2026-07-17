from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from pyocd_debug_mcp.firmstore.reports import ReportError, ReportWriter
from pyocd_debug_mcp.firmstore.store import (
    FirmStore,
    ImmutableArtifactError,
    PersistedAuthorityError,
)


def test_setup_report_is_immutable_while_its_log_is_append_only(tmp_path: Path) -> None:
    reports = ReportWriter(FirmStore(tmp_path))
    paths = reports.create_setup("setup-001", {"board_id": "bench_board", "status": "failed"})
    original = paths.report.read_bytes()

    reports.append_setup_event("setup-001", {"phase": "probe", "status": "started"})
    reports.append_setup_event("setup-001", {"phase": "probe", "status": "failed"})
    with pytest.raises(ImmutableArtifactError, match="already exists"):
        reports.create_setup("setup-001", {"board_id": "other", "status": "passed"})

    assert paths.report.read_bytes() == original
    report = json.loads(original)
    assert report["report_type"] == "setup"
    assert report["attempt_id"] == "setup-001"
    assert (
        datetime.fromisoformat(report["created_at"].replace("Z", "+00:00")).utcoffset()
        is not None
    )
    events = [json.loads(line) for line in paths.events.read_text(encoding="utf-8").splitlines()]
    assert [event["status"] for event in events] == ["started", "failed"]
    assert all("recorded_at" in event for event in events)


def test_validation_report_uses_separate_immutable_attempt_directory(tmp_path: Path) -> None:
    reports = ReportWriter(FirmStore(tmp_path))
    setup = reports.create_setup("attempt-001", {"status": "complete"})
    validation = reports.create_validation("attempt-001", {"status": "passed"})
    reports.append_validation_event("attempt-001", {"check": "identity", "status": "passed"})

    assert setup.report.parent.parent.name == "setup"
    assert validation.report.parent.parent.name == "validation"
    assert json.loads(validation.report.read_text(encoding="utf-8"))["report_type"] == "validation"
    assert validation.events.exists()


def test_reports_reject_reserved_and_authority_fields_before_writing(tmp_path: Path) -> None:
    reports = ReportWriter(FirmStore(tmp_path))

    with pytest.raises(ReportError, match="reserved fields"):
        reports.create_setup("setup-001", {"created_at": "caller-controlled"})
    with pytest.raises(PersistedAuthorityError, match="permission_grant"):
        reports.create_validation("validation-001", {"permission_grant": True})
    with pytest.raises(PersistedAuthorityError, match="active_gate"):
        reports.append_setup_event("setup-001", {"active_gate": True})

    assert not reports.paths("setup", "setup-001").report.exists()
    assert not reports.paths("setup", "setup-001").events.exists()
    assert not reports.paths("validation", "validation-001").report.exists()
