"""Shared helpers for the monitor test suite.

Every test pins the monitoring store to a temporary directory. Without this a
test would write into the developer's real per-user store, because the store
resolves the application-data directory first and ignores the operator override
while that directory is usable.
"""

from __future__ import annotations
from pyocd_debug_mcp.monitor.counters import USAGE_SNAPSHOT_CADENCE
from pyocd_debug_mcp.monitor.ledger import SegmentLedger

import shutil
import tempfile
import time
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


def settle_delivery(monitor: IssueMonitor, timeout: float = 10.0) -> None:
    """Finish the monitor's handed-off deliveries, then stop its sender.

    Delivery runs on a background thread that copies a sealed segment or report
    to the transport and then deletes the local copy once it is acknowledged.
    A store observation taken while that thread is mid-job can list a file in
    one place before it is copied and in the other just before it is deleted,
    then find it gone -- so a positive check undercounts and a negative check
    passes over a record it never read. After this returns, every record is
    stable: still resident locally if not yet acknowledged, or in the
    transport's retained copy if it was.

    ``stop`` abandons queued jobs, so wait for the queue to empty first; the job
    in flight then completes before the thread exits. The sender stays stopped:
    anything recorded afterwards remains local, which the store helpers count.
    Raises if the sender does not finish in time rather than reading anyway.
    """

    service = monitor._delivery
    thread = service._thread
    if thread is None or not thread.is_alive():
        return
    deadline = time.monotonic() + timeout
    while not service._queue.empty() and time.monotonic() < deadline:
        time.sleep(0.01)
    service.stop(timeout=max(0.0, deadline - time.monotonic()))
    if thread.is_alive():
        raise AssertionError(f"monitor delivery did not settle within {timeout}s")


class MonitorTestCase(unittest.TestCase):
    """Base case that isolates the store and tears the monitor down cleanly."""

    def setUp(self) -> None:
        self.store_dir = Path(tempfile.mkdtemp(prefix="byo-monitor-test-"))
        paths._reset_cache(self.store_dir)
        self.addCleanup(self._teardown_store)
        self._monitors: list[IssueMonitor] = []

    def _teardown_store(self) -> None:
        failures = []
        for monitor in self._monitors:
            try:
                monitor.closeout("test-teardown")
            except Exception as error:
                failures.append(error)
        paths._reset_cache(None)
        if failures:
            # unittest reports cleanup errors separately from the original
            # assertion; retain the store for the ordinary failure hook.
            raise RuntimeError(f"monitor fixture closeout failed: {failures!r}") from failures[0]
        shutil.rmtree(self.store_dir)

    def make_monitor(self, **kwargs: Any) -> IssueMonitor:
        context = kwargs.pop("context", None) or make_context()
        monitor = IssueMonitor(context, **kwargs)
        self._monitors.append(monitor)
        return monitor

    def settle(self) -> None:
        """Settle delivery for every monitor this test made; see ``settle_delivery``.

        Call before reading the store, for positive and negative checks alike.
        """

        for monitor in self._monitors:
            settle_delivery(monitor)

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
    "settle_delivery",
]


def add_snapshot(ledger: SegmentLedger, total: int = USAGE_SNAPSHOT_CADENCE, **extra) -> bool:
    """Append one usage-snapshot record, the ledger's most common occasion.

    There is no per-call form to append instead: the counts a snapshot carries are
    what answer "how much was used", so a test that wants a record in the file
    writes one of these.
    """

    detail = {"cumulative": True, "total_calls": total}
    detail.update(extra)
    return ledger.append("usage_snapshot", detail=detail)
