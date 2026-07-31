"""Shared helpers for the monitor test suite.

Every test pins the monitoring store to a temporary directory. Without this a
test would write into the developer's real per-user store, because the store
resolves the application-data directory first and ignores the operator override
while that directory is usable.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.monitor import paths
from pyocd_debug_mcp.monitor.monitor import IssueMonitor, MonitorContext


class FakeGate:
    def __init__(self, probe: str | None = "probe-abc") -> None:
        self.probe_identity = probe


class FakePlan:
    def __init__(self, plan_id: str = "plan-1", remaining: int = 3) -> None:
        self.plan_id = plan_id
        self.remaining_calls = remaining


class FakeGrantMode:
    value = "one-time"


class FakeGrant:
    def __init__(self, grant_id: str = "grant-1") -> None:
        self.grant_id = grant_id
        self.mode = FakeGrantMode()


def make_context(
    *,
    run_id: str = "run-test-0001",
    advertised: tuple[str, ...] = ("get_state", "connect", "flash_application"),
    advertised_tools: Any = None,
    plan: Any = None,
    grant: Any = None,
    gate: Any = None,
    revision: int = 1,
) -> MonitorContext:
    """Build a monitor context. Pass ``advertised_tools`` for a *changing* set.

    Tool visibility is dynamic in the real server, so a test that needs the
    advertised set to change mid-run supplies a callable instead of the fixed
    tuple.
    """

    return MonitorContext(
        run_id=run_id,
        run_started_at=datetime.now(timezone.utc),
        server_version="test",
        advertised_tools=advertised_tools or (lambda: advertised),
        list_revision=lambda: revision,
        active_plan=lambda tool, board: plan,
        active_grant=lambda tool, board: grant,
        gate_snapshot=lambda board: gate,
        live_identity=lambda board: gate,
        connection_id=lambda board: f"conn-{board}",
    )


class MonitorTestCase(unittest.TestCase):
    """Base case that isolates the store and tears the monitor down cleanly."""

    def setUp(self) -> None:
        self.store_dir = Path(tempfile.mkdtemp(prefix="byo-monitor-test-"))
        paths._reset_cache(self.store_dir)
        self.addCleanup(self._teardown_store)
        self._monitors: list[IssueMonitor] = []

    def _teardown_store(self) -> None:
        for monitor in self._monitors:
            try:
                monitor.closeout("test-teardown")
            except Exception:
                pass
        paths._reset_cache(None)
        shutil.rmtree(self.store_dir, ignore_errors=True)

    def make_monitor(self, **kwargs: Any) -> IssueMonitor:
        context = kwargs.pop("context", None) or make_context()
        monitor = IssueMonitor(context, **kwargs)
        self._monitors.append(monitor)
        return monitor

    # -- store introspection -------------------------------------------

    @property
    def server_data(self) -> Path:
        return self.store_dir / "server_data"

    @property
    def simulated_remote(self) -> Path:
        return self.store_dir / "simulated_remote"

    def ledger_files(self) -> list[Path]:
        return sorted(self.server_data.rglob("*.jsonl"))

    def report_files(self) -> list[Path]:
        """Return one path per distinct report.

        A delivered report exists twice on disk -- the local copy and the copy
        the transport retained -- so counting files would double every report.
        """

        by_id: dict[str, Path] = {}
        for path in sorted(self.store_dir.rglob("rpt-*.json")):
            by_id.setdefault(path.name, path)
        return list(by_id.values())

    def ledger_records(self) -> list[dict]:
        """Return every distinct ledger record across the whole store.

        Deduplicated by chain hash for the same reason ``report_files`` dedupes by
        name: a delivered segment exists both where the transport retained it and,
        until its acknowledgement drains it, in ``server_data``. Counting lines
        would double every record that happened to be in flight.
        """

        import json

        by_hash: dict[str, dict] = {}
        ordered: list[dict] = []
        for path in sorted(self.store_dir.rglob("*.jsonl")):
            try:
                text = path.read_text(encoding="utf-8")
            except (FileNotFoundError, PermissionError):
                # File was deleted by ACK-driven delivery after glob, or hardened (Windows append-only ACL).
                continue
            for line in text.splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                key = str(record.get("hash") or f"{path.name}:{record.get('seq')}")
                if key in by_hash:
                    continue
                by_hash[key] = record
                ordered.append(record)
        ordered.sort(key=lambda r: (str(r.get("run_id")), int(r.get("seq") or 0)))
        return ordered

    def all_store_text(self) -> str:
        chunks: list[str] = []
        for path in self.store_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        return "\n".join(chunks)


__all__ = [
    "FakeGate",
    "FakeGrant",
    "FakePlan",
    "MonitorTestCase",
    "make_context",
]
