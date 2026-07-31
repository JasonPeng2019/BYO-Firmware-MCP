"""The monitor facade: one entry point per observation occasion.

Observation is strictly passive. Every public method here swallows its own
failures, because if monitoring cannot run, tool execution must proceed unchanged.
Nothing here catches an exception dispatch needs to propagate, delays a deadline,
holds a lock, or reorders the guarded dispatch sequence. The single exception is
``check_block``, which is the one sanctioned place monitoring refuses dispatch.

A failure inside the monitor never produces a report about itself -- that would
recurse -- and is only counted.
"""

from __future__ import annotations

import json
import platform
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.monitor import paths
from pyocd_debug_mcp.monitor.block import BlockState, StalenessBlock
from pyocd_debug_mcp.monitor.build_profile import NARRATIVE_LOGGING, profile_name
from pyocd_debug_mcp.monitor.classify import (
    TRIAGE_FOR_SIGNAL,
    Outcome,
    Signal,
    TriageClass,
    classify_exception,
    classify_result,
    error_signature,
)
from pyocd_debug_mcp.monitor.counters import (
    CHECKIN_CADENCE,
    USAGE_SNAPSHOT_CADENCE,
    CountersSnapshot,
    RunCounters,
)
from pyocd_debug_mcp.monitor.delivery import DeliveryService
from pyocd_debug_mcp.monitor.ledger import SegmentLedger, verify_prior_runs
from pyocd_debug_mcp.monitor.redaction import fingerprint, result_text, scrub_mechanical
from pyocd_debug_mcp.monitor.reports import (
    Deduper,
    Origin,
    ReportEnvironment,
    build_report,
    build_summary,
    grouping_key,
)
from pyocd_debug_mcp.monitor.thrash import ThrashDetector
from pyocd_debug_mcp.monitor.trail import BoardTrail
from pyocd_debug_mcp.monitor.transport import (
    NullTransport,
    SimulatedRemoteTransport,
    Transport,
)

_MAX_BUFFERED_RECORDS = 2000

CHECKIN_PROMPT = (
    "\n[routine check-in due] Please write and submit a routine check-in with "
    "submit_routine_checkin, summarizing the work you have done since the last "
    "check-in. This is a routine activity record, not an error report."
)


@dataclass(frozen=True, slots=True)
class MonitorContext:
    """Read-only callables injected by the composition root.

    The monitor never imports the server; the server hands it these instead, the
    same way every other tool module here receives its services.
    """

    run_id: str
    run_started_at: datetime
    server_version: str
    advertised_tools: Callable[[], tuple[str, ...]]
    list_revision: Callable[[], int]
    active_plan: Callable[[str, str], Any]
    active_grant: Callable[[str, str], Any]
    gate_snapshot: Callable[[str], Any]
    live_identity: Callable[[str], Any]
    connection_id: Callable[[str], str | None]


class Observation:
    """One in-flight tool call. Records on the way past; changes nothing."""

    __slots__ = ("_monitor", "_tool", "_board", "_args_fp", "_started", "_guard_fp")

    def __init__(
        self, monitor: IssueMonitor, tool: str, board: str | None, args_fp: str, guard_fp: str
    ) -> None:
        self._monitor = monitor
        self._tool = tool
        self._board = board
        self._args_fp = args_fp
        self._guard_fp = guard_fp
        self._started = time.monotonic()

    def _duration_ms(self) -> int:
        return max(0, int(round((time.monotonic() - self._started) * 1000)))

    def completed(self, result: object) -> None:
        try:
            text = result_text(result)
            outcome, error_class, remedy = classify_result(text)
            self._monitor._observe(
                tool=self._tool,
                board=self._board,
                args_fp=self._args_fp,
                guard_fp=self._guard_fp,
                outcome=outcome,
                triage=TriageClass.NONE,
                error_class=error_class,
                remedy=remedy,
                duration_ms=self._duration_ms(),
                exc=None,
            )
        except BaseException:  # noqa: BLE001 - observation must never alter dispatch
            self._monitor._note_internal_failure()

    def failed(self, exc: BaseException) -> None:
        try:
            outcome, triage, error_class = classify_exception(exc)
            self._monitor._observe(
                tool=self._tool,
                board=self._board,
                args_fp=self._args_fp,
                guard_fp=self._guard_fp,
                outcome=outcome,
                triage=triage,
                error_class=error_class,
                remedy=None,
                duration_ms=self._duration_ms(),
                exc=exc,
            )
        except BaseException:  # noqa: BLE001
            self._monitor._note_internal_failure()


class NullMonitor:
    """Stand-in used when the monitor cannot be constructed.

    Monitoring failing closed would be worse than monitoring being absent, so a
    construction failure degrades to this rather than preventing the server from
    starting.
    """

    def __init__(self, reason: str = "monitor unavailable") -> None:
        self.reason = reason

    def begin(self, tool: str, arguments: Mapping[str, Any], board: str | None) -> None:
        del tool, arguments, board
        return None

    def bind_workspace(self, path: Path | None) -> None:
        del path

    def boot(self) -> None:
        return None

    def closeout(self, reason: str) -> None:
        del reason

    def check_block(self) -> None:
        return None

    def consume_checkin_prompt(self) -> str | None:
        return None

    def health(self) -> dict[str, Any]:
        return {"monitoring": "unavailable", "reason": self.reason}

    def submit_report(self, form: Mapping[str, Any]) -> dict[str, Any]:
        del form
        return {"status": "monitor_unavailable", "reason": self.reason}

    def submit_checkin(self, form: Mapping[str, Any]) -> dict[str, Any]:
        del form
        return {"status": "monitor_unavailable", "reason": self.reason}


class IssueMonitor:
    """Owns the trail, counters, ledger, detectors, and delivery for one run."""

    def __init__(
        self,
        context: MonitorContext,
        *,
        transport: Transport | None = None,
        usage_snapshot_every: int = USAGE_SNAPSHOT_CADENCE,
        checkin_every: int = CHECKIN_CADENCE,
    ) -> None:
        self._ctx = context
        self._store = paths.resolve_store_root()
        self._workspace = paths.UNBOUND_WORKSPACE
        self._workspace_token: str | None = None
        self._counters = RunCounters()
        self._trail = BoardTrail()
        self._thrash = ThrashDetector()
        self._deduper = Deduper()
        self._ledger = SegmentLedger(self._store, self._workspace, context.run_id)
        self._block = StalenessBlock(self._store.server_data)
        self._usage_snapshot_every = usage_snapshot_every
        self._checkin_every = checkin_every
        self._guard = threading.Lock()
        # Serializes bind_workspace() only. Dispatch (_append) never takes this,
        # so a slow bind can never stall a tool call the way holding _guard for
        # the whole bind used to risk.
        self._bind_lock = threading.Lock()
        self._buffered: list[dict[str, Any]] = []
        self._bound = False
        self._checkin_due = False
        self._internal_failures = 0
        self._verification: dict[str, str] = {}
        self._transport = transport or self._default_transport()
        self._delivery = DeliveryService(
            self._store, self._workspace, context.run_id, self._transport, self._block
        )

    # -- construction helpers -------------------------------------------

    def _default_transport(self, workspace: str | None = None) -> Transport:
        destination = self._store.simulated_remote
        if destination is None:
            return NullTransport()
        return SimulatedRemoteTransport(
            destination,
            workspace if workspace is not None else self._workspace,
            server_version=self._ctx.server_version,
        )

    def _note_internal_failure(self) -> None:
        with self._guard:
            self._internal_failures += 1

    def _refresh_advertised(self) -> None:
        """Re-read the advertised tool set before reporting coverage.

        Discovery here is dynamic -- visibility changes as plans are accepted --
        so a set captured once at boot would go stale and under-report which
        tools were never exercised.
        """

        advertised = self._safe(self._ctx.advertised_tools, ())
        if advertised:
            self._counters.set_advertised(tuple(advertised))

    def _refreshed_snapshot(self) -> CountersSnapshot:
        """Refresh the advertised set, then snapshot. Always in that order.

        Every caller that needs a snapshot whose ``never_exercised``/``exercised``
        fields are correct must go through this, never call ``_counters.snapshot()``
        directly: discovery is dynamic (F-42), so a snapshot taken before the
        refresh answers the coverage question with stale data. This is the fix
        for the exact bug class already found once (the usage-snapshot tick was
        computing coverage against a pre-refresh set) -- centralising the
        refresh-then-snapshot order here means a future call site cannot get the
        order backwards the way ``submit_checkin`` once did.
        """

        self._refresh_advertised()
        return self._counters.snapshot()

    # -- lifecycle -------------------------------------------------------

    def boot(self) -> None:
        """Write the boot record and schedule recovery. Must not block startup."""

        try:
            self._counters.set_advertised(self._ctx.advertised_tools())
            self._verification = verify_prior_runs(
                self._store, self._workspace, self._ctx.run_id
            )
            self._append(
                "boot",
                detail={
                    "server_version": self._ctx.server_version,
                    "store_state": self._store.state.value,
                    "workspace": self._workspace,
                    "transport": self._transport.name,
                    "narrative_logging": profile_name(),
                    "prior_run_verification": self._verification,
                },
            )
            self._delivery.start()
            self._delivery.enqueue_bootup()
        except BaseException:  # noqa: BLE001
            self._note_internal_failure()

    def bind_workspace(self, path: Path | None) -> None:
        """Bind workspace identity and flush everything buffered before it arrived.

        ``_bound`` must never become visible to a concurrent ``_append`` before
        ``_ledger`` and ``_delivery`` are the bound ones -- otherwise a call
        landing in that window would see ``_bound is True`` and write straight
        to the pre-bind (unbound-workspace) ledger instead of buffering, silently
        splitting one run's occasions across two chains. So the new ledger,
        transport, and delivery service are built *first*, off to the side, and
        ``_bound`` is flipped in the same guarded block that publishes them --
        one atomic swap, not two.

        The slow parts -- constructing the new services, verifying prior runs,
        stopping the old delivery thread, starting the new one -- all happen
        outside ``self._guard``, the lock ``_append`` (the dispatch path) takes.
        Holding that lock across a thread stop/start would let a slow bind stall
        a concurrent tool call, which is the failure this must not reintroduce.
        ``_bind_lock`` (never touched by dispatch) serializes concurrent binds
        instead.
        """

        try:
            if self._bound:
                return
            if not self._bind_lock.acquire(blocking=False):
                # Another bind is already in flight (or just finished); this
                # call has nothing to do. Never block waiting for it here --
                # bind_workspace can itself be called from the dispatch path.
                return
            try:
                if self._bound:
                    return
                wid = paths.workspace_id(path)
                token = paths.workspace_token(self._store, wid)
                new_ledger = SegmentLedger(self._store, wid, self._ctx.run_id)
                new_transport = self._default_transport(wid)
                new_delivery = DeliveryService(
                    self._store, wid, self._ctx.run_id, new_transport, self._block
                )
                verification = verify_prior_runs(self._store, wid, self._ctx.run_id)
                with self._guard:
                    previous = self._delivery
                    self._workspace = wid
                    self._workspace_token = token
                    self._ledger = new_ledger
                    self._transport = new_transport
                    self._delivery = new_delivery
                    self._verification = verification
                    buffered = list(self._buffered)
                    self._buffered.clear()
                    # Published last, and only here: by the time this is True,
                    # _ledger and _delivery are already the bound ones.
                    self._bound = True
            finally:
                self._bind_lock.release()
            # Stop the pre-bind service before it's forgotten, or its daemon
            # thread outlives the run pointed at a workspace we no longer use.
            # Neither this nor the start below holds _guard.
            previous.stop(timeout=0.1)
            new_delivery.start()
            for record in buffered:
                self._write_record(dict(record))
            new_delivery.enqueue_bootup()
        except BaseException:  # noqa: BLE001
            self._note_internal_failure()

    def closeout(self, reason: str) -> None:
        """Write the close record first, then attempt a bounded send."""

        try:
            # A run that never saw a handshake still gets its records on disk.
            self._flush_unbound()
            # Refresh-then-snapshot: the close record's usage block carries
            # never_exercised too, so this needs the live advertised set for the
            # same reason every other snapshot-producing call site does (C-1).
            snapshot = self._refreshed_snapshot()
            self._append(
                "close",
                detail={
                    "reason": reason,
                    "uptime_seconds": self._uptime(),
                    **self._usage(snapshot),
                },
            )
            info = self._ledger.seal()
            segments = list(self._ledger.sealed_segments())
            if info is not None and info not in segments:
                segments.append(info)
            # Recording is the durable act; sending is best-effort, so the close
            # record is already on disk before any of this can fail or hang.
            # One shared deadline across drain-then-stop, not two independent
            # budgets stacked -- see DeliveryService.close_for_shutdown.
            self._delivery.close_for_shutdown(segments)
        except BaseException:  # noqa: BLE001
            self._note_internal_failure()

    # -- the block (the one place monitoring holds authority) ------------

    def check_block(self) -> None:
        """Refuse guarded hardware dispatch when remote logging has gone stale.

        Deliberately not wrapped in a swallow: this is the sanctioned exception to
        fail-open. It compares a cached timestamp and does no I/O, because it runs
        inside a held board lock on every guarded call.
        """

        if self._block.state() is not BlockState.TRIPPED:
            return
        from pyocd_debug_mcp.services.session_runtime import PolicyRefusal

        raise PolicyRefusal("monitor/logging-stale", self._block.refusal_message())

    # -- observation -----------------------------------------------------

    def _guard_fingerprint(self, tool: str, board: str | None) -> str:
        if board is None:
            return f"rev={self._safe(self._ctx.list_revision, 0)}"
        plan = self._safe(lambda: self._ctx.active_plan(tool, board), None)
        grant = self._safe(lambda: self._ctx.active_grant(tool, board), None)
        gate = self._safe(lambda: self._ctx.gate_snapshot(board), None)
        return "|".join(
            [
                f"rev={self._safe(self._ctx.list_revision, 0)}",
                f"plan={getattr(plan, 'plan_id', None)}",
                f"left={getattr(plan, 'remaining_calls', None)}",
                f"grant={getattr(grant, 'grant_id', None)}",
                f"gate={gate is not None}",
            ]
        )

    @staticmethod
    def _safe(fn: Callable[[], Any], default: Any) -> Any:
        try:
            return fn()
        except Exception:  # noqa: BLE001 - guard-state reads are observational
            return default

    def begin(
        self, tool: str, arguments: Mapping[str, Any], board: str | None
    ) -> Observation | None:
        """Start observing one call. Retains a fingerprint, never the arguments."""

        try:
            args_fp = fingerprint(arguments)
            guard_fp = self._guard_fingerprint(tool, board)
            return Observation(self, tool, board, args_fp, guard_fp)
        except BaseException:  # noqa: BLE001
            self._note_internal_failure()
            return None

    def _append(self, kind: str, **fields: Any) -> None:
        """Record one ledger entry, buffering until a workspace is known.

        Everything is buffered before binding, not only calls: the boot record is
        produced before the handshake arrives, and writing it to the unbound area
        would orphan it there once the real workspace is bound.
        """

        with self._guard:
            if not self._bound:
                if len(self._buffered) < _MAX_BUFFERED_RECORDS:
                    self._buffered.append({"kind": kind, **fields})
                return
        self._write_record({"kind": kind, **fields})

    def _write_record(self, record: dict[str, Any]) -> None:
        kind = record.pop("kind", "unknown")
        if self._ledger.append(kind, **record):
            self._counters.note_appended()
            return
        error = self._ledger.last_error
        if error:
            self._counters.note_write_failure(error)

    def _flush_unbound(self) -> None:
        """Write anything still buffered under the literal unbound workspace.

        Honestly labelled, never discarded: a run where no workspace was ever
        supplied still produces its records.
        """

        with self._guard:
            if self._bound:
                return
            self._bound = True
            buffered = list(self._buffered)
            self._buffered.clear()
        for record in buffered:
            self._write_record(dict(record))

    def _observe(
        self,
        *,
        tool: str,
        board: str | None,
        args_fp: str,
        guard_fp: str,
        outcome: Outcome,
        triage: TriageClass,
        error_class: str | None,
        remedy: str | None,
        duration_ms: int,
        exc: BaseException | None,
    ) -> None:
        connection = self._safe(
            lambda: self._ctx.connection_id(board) if board else None, None
        )
        self._trail.append(
            tool=tool,
            board=board,
            connection=connection,
            args_fp=args_fp,
            outcome=outcome.value,
            error_class=error_class,
            remedy=remedy,
            duration_ms=duration_ms,
        )
        # Counting and the trail are the whole per-call cost: both in memory, no
        # ledger write. The durable record is the periodic usage snapshot, whose
        # cumulative counts answer the same question without an entry per call.
        total = self._counters.record(tool, outcome.value, error_class)
        if outcome is Outcome.UNEXPECTED_ERROR and exc is not None:
            signal = (
                Signal.ENVIRONMENT_FAULT
                if triage is TriageClass.ENVIRONMENT_FAULT
                else Signal.RUNTIME_ERROR
            )
            self._file_report(
                signal=signal,
                origin=Origin.SERVER_AUTO,
                tool=tool,
                board=board,
                anchor=error_signature(exc),
                title=f"{tool}: {type(exc).__name__}",
                description=str(exc)[:500],
                refusal_code=None,
                named_remedy=None,
                args_fp=args_fp,
            )
        elif self._thrash.observe(
            board=board,
            tool=tool,
            args_fp=args_fp,
            outcome=outcome.value,
            error_class=error_class,
            guard_fp=guard_fp,
        ):
            self._file_report(
                signal=Signal.THRASHING,
                origin=Origin.SERVER_THRASH,
                tool=tool,
                board=board,
                anchor=f"thrash:{tool}:{error_class or outcome.value}",
                title=f"{tool} repeated with no change in outcome or state",
                description=(
                    f"{tool} recurred with equivalent arguments and an identical "
                    f"outcome ({outcome.value}) without any state transition."
                ),
                refusal_code=error_class,
                named_remedy=remedy,
                args_fp=args_fp,
            )
        # Two independent cadences. At call 500 both fire, which is correct: every
        # fifth usage snapshot coincides with a check-in prompt.
        if self._usage_snapshot_every > 0 and total % self._usage_snapshot_every == 0:
            self._usage_snapshot_tick()
        if (
            NARRATIVE_LOGGING
            and self._checkin_every > 0
            and total % self._checkin_every == 0
        ):
            with self._guard:
                self._checkin_due = True

    def _usage_snapshot_tick(self) -> None:
        """Record a cumulative usage snapshot, then roll and hand off the segment.

        The append is synchronous and local -- that is the durability obligation.
        Everything past it is a non-blocking handoff to the background sender, so
        a boundary landing inside a plan sequence, a batch, or a flash neither
        delays nor interleaves with the operation that tripped it.
        """

        try:
            # Refresh-then-snapshot via the shared helper: see its docstring for
            # why the order is load-bearing here.
            snapshot = self._refreshed_snapshot()
            summary = self._build_summary(
                trigger=str(snapshot.total), snapshot=snapshot
            )
            self._append(
                "usage_snapshot",
                detail={"summary_id": summary["summary_id"], **self._usage(snapshot)},
            )
            # Seal on the same beat as the snapshot: the roll boundary is the
            # snapshot cadence, not a second number that could drift from it.
            rolled = self._ledger.roll()
            segments = list(self._ledger.sealed_segments())
            if rolled is not None and rolled not in segments:
                segments.append(rolled)
            self._delivery.enqueue_segments(segments)
            # Durable local copy before the handoff, same as a problem report:
            # a dropped or interrupted send still leaves a file bootup recovery
            # can find.
            self._write_artifact_file(summary)
            self._delivery.enqueue_report(summary)
        except BaseException:  # noqa: BLE001
            self._note_internal_failure()

    @staticmethod
    def _usage(snapshot: CountersSnapshot) -> dict[str, Any]:
        """The run's cumulative counts, as carried by snapshots and every report.

        Cumulative and monotonic, never a per-window delta: a snapshot that is
        dropped, withheld, or never delivered cannot understate the total, because
        the next one still carries the true running total.
        """

        return {
            "cumulative": True,
            "total_calls": snapshot.total,
            "per_tool": snapshot.per_tool,
            "per_outcome": snapshot.per_outcome,
            "per_error_class": snapshot.per_error_class,
            "never_exercised": list(snapshot.never_exercised),
        }

    def consume_checkin_prompt(self) -> str | None:
        """Return the one-shot check-in prompt, clearing it when emitted.

        Cleared on emission rather than on receipt: compliance is behavioural, and
        nothing may key off whether a check-in actually arrived.
        """

        if not NARRATIVE_LOGGING:
            return None
        with self._guard:
            if not self._checkin_due:
                return None
            self._checkin_due = False
        return CHECKIN_PROMPT

    # -- reports ---------------------------------------------------------

    def _environment(self, provider: str | None = None) -> ReportEnvironment:
        return ReportEnvironment(
            server_version=self._ctx.server_version,
            python_version=platform.python_version(),
            platform=sys.platform,
            narrative_logging=profile_name(),
            provider=provider,
        )

    def _guard_state(self, tool: str | None, board: str | None) -> dict[str, Any]:
        if not tool or not board:
            return {"list_revision": self._safe(self._ctx.list_revision, None)}
        plan = self._safe(lambda: self._ctx.active_plan(tool, board), None)
        grant = self._safe(lambda: self._ctx.active_grant(tool, board), None)
        gate = self._safe(lambda: self._ctx.gate_snapshot(board), None)
        return {
            "list_revision": self._safe(self._ctx.list_revision, None),
            "gate_open": gate is not None,
            "active_plan_id": getattr(plan, "plan_id", None),
            "plan_remaining_calls": getattr(plan, "remaining_calls", None),
            "permission_mode": getattr(getattr(grant, "mode", None), "value", None),
        }

    def _board_scope(self, board: str | None) -> dict[str, Any]:
        if board is None:
            return {"board_id": None}
        identity = self._safe(lambda: self._ctx.live_identity(board), None)
        probe = getattr(identity, "probe_identity", None)
        return scrub_mechanical(
            {
                "board_id": board,
                "connection_id": self._safe(lambda: self._ctx.connection_id(board), None),
                "probe_uid": probe,
                "identity_kind": "hardware_stable" if probe else "session_local",
            }
        )

    def _file_report(
        self,
        *,
        signal: Signal,
        origin: str,
        tool: str | None,
        board: str | None,
        anchor: str,
        title: str,
        description: str,
        refusal_code: str | None,
        named_remedy: str | None,
        args_fp: str | None,
        narrative: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        triage = TRIAGE_FOR_SIGNAL.get(signal, TriageClass.SERVER_DEFECT)
        key = grouping_key(signal, triage, tool, anchor)
        emit, suppressed = self._deduper.admit(key)
        if not emit:
            return None
        # Same reason as the snapshot tick: coverage is computed against the live
        # advertised set, not one captured at boot.
        snapshot = self._refreshed_snapshot()
        report = build_report(
            signal=signal,
            origin=origin,
            tool=tool,
            board=board,
            anchor=anchor,
            title=title,
            description=description,
            run_id=self._ctx.run_id,
            run_started_at=self._ctx.run_started_at.isoformat().replace("+00:00", "Z"),
            workspace_token=self._workspace_token,
            usage=self._usage(snapshot),
            trail=self._trail.records_for(board),
            guard_state=self._guard_state(tool, board),
            board_scope=self._board_scope(board),
            environment=self._environment(),
            refusal_code=refusal_code,
            named_remedy=named_remedy,
            args_fp=args_fp,
            narrative=narrative,
            suppressed_since_last=suppressed,
        )
        self._write_artifact_file(report)
        # A filed report is one of the ledger's occasions, so the chain carries the
        # fact that it happened alongside the snapshots. The narrative and the
        # trail stay in the report itself; the ledger keeps the mechanical anchor.
        self._append(
            "report",
            detail={
                "report_id": report["report_id"],
                "signal_type": signal.value,
                "triage_class": triage.value,
                "origin": origin,
                "tool": tool,
                "error_signature": anchor,
                "grouping_key": key,
                **self._usage(snapshot),
            },
        )
        self._delivery.enqueue_report(report)
        return report

    def _write_artifact_file(self, artifact: Mapping[str, Any]) -> None:
        """Durably persist a report or summary body next to the ledger.

        This is what bootup recovery's ``_prior_report_files`` finds and resends
        if ``enqueue_report``'s handoff to the background sender is ever dropped
        (a full queue) or never completes before the process exits -- the same
        drop-the-handoff-not-the-record contract F-167 already gives sealed
        ledger segments, extended to the report/summary bodies delivered
        alongside them. Identity is ``report_id`` for a problem report or
        ``summary_id`` for a usage-snapshot/checkin summary -- both are stable,
        collision-free, and already used as the delivery identity in
        ``DeliveryService``.
        """

        server_data = self._store.server_data
        if server_data is None:
            return
        identity = artifact.get("report_id") or artifact.get("summary_id")
        if not identity:
            return
        # Reports live in the per-user store, never inside the workspace project
        # directory and never under the safety-evidence root.
        directory = server_data / self._workspace / "reports"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{identity}.json").write_text(
                json.dumps(dict(artifact), ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except (OSError, KeyError):
            return

    # -- summaries and health --------------------------------------------

    def _uptime(self) -> float:
        return max(
            0.0, (datetime.now(timezone.utc) - self._ctx.run_started_at).total_seconds()
        )

    def _build_summary(
        self,
        *,
        trigger: str,
        narrative: dict[str, Any] | None = None,
        snapshot: CountersSnapshot | None = None,
    ) -> dict[str, Any]:
        # A caller that already took a snapshot passes it in (via the shared
        # refresh-then-snapshot helper), so the delivered summary and the ledger
        # record it is named in carry the same numbers. Only refresh-and-snapshot
        # here as a fallback for a caller that did not -- never re-refresh a
        # snapshot that was already taken, since the refresh happening *after*
        # is exactly the bug this must not reintroduce.
        if snapshot is None:
            snapshot = self._refreshed_snapshot()
        segments = self._ledger.sealed_segments()
        return build_summary(
            run_id=self._ctx.run_id,
            run_started_at=self._ctx.run_started_at.isoformat().replace("+00:00", "Z"),
            uptime_seconds=self._uptime(),
            trigger=trigger,
            counters={
                "total": snapshot.total,
                "per_tool": snapshot.per_tool,
                "per_outcome": snapshot.per_outcome,
                "per_error_class": snapshot.per_error_class,
                "first_at": snapshot.first_at,
                "last_at": snapshot.last_at,
            },
            coverage={
                "exercised": list(snapshot.exercised),
                "never_exercised": list(snapshot.never_exercised),
            },
            ledger={
                "total_appended": snapshot.total_appended,
                "resident_files": len(self._ledger.resident_files()),
                "chain_head": self._ledger.head,
                "hardening": self._ledger.hardening,
                "verification": self._verification,
                "last_write_error": snapshot.last_write_error,
            },
            delivery={
                **self._delivery.describe(),
                "undelivered_files": self._delivery.undelivered(segments),
                "store_state": self._store.state.value,
                "workspace_bound": self._bound,
                "block": self._block.describe(),
            },
            environment=self._environment(),
            narrative=narrative,
        )

    def health(self) -> dict[str, Any]:
        """Return the live readout. Side-effect free: no send, no write, no mutation."""

        try:
            snapshot = self._refreshed_snapshot()
            segments = self._ledger.sealed_segments()
            return {
                "run_id": self._ctx.run_id,
                "run_started_at": self._ctx.run_started_at.isoformat().replace(
                    "+00:00", "Z"
                ),
                "uptime_seconds": round(self._uptime(), 3),
                "narrative_logging": profile_name(),
                "counters": {
                    "total": snapshot.total,
                    "per_tool": snapshot.per_tool,
                    "per_outcome": snapshot.per_outcome,
                    "per_error_class": snapshot.per_error_class,
                    "total_appended": snapshot.total_appended,
                    "first_at": snapshot.first_at,
                    "last_at": snapshot.last_at,
                },
                "coverage": {
                    "exercised": list(snapshot.exercised),
                    "never_exercised": list(snapshot.never_exercised),
                },
                "ledger": {
                    "resident_files": len(self._ledger.resident_files()),
                    "chain_head": self._ledger.head,
                    "hardening": self._ledger.hardening,
                    "verification": self._verification,
                },
                "recording": {
                    "store_state": self._store.state.value,
                    "workspace_bound": self._bound,
                    # A counter ahead of total_appended means calls are happening
                    # and nothing is reaching disk. A counter ahead of the resident
                    # file count is expected: delivered files delete themselves.
                    "counter_minus_appended": snapshot.total - snapshot.total_appended,
                    "write_failures": snapshot.write_failures,
                    "last_write_error": snapshot.last_write_error,
                    "internal_failures": self._internal_failures,
                },
                "delivery": {
                    **self._delivery.describe(),
                    "undelivered_files": self._delivery.undelivered(segments),
                },
                "block": self._block.describe(),
            }
        except BaseException:  # noqa: BLE001
            self._note_internal_failure()
            return {"monitoring": "degraded"}

    # -- agent submissions ------------------------------------------------

    def submit_report(self, form: Mapping[str, Any]) -> dict[str, Any]:
        from pyocd_debug_mcp.monitor.narrative import validate_issue_form

        try:
            validated, signal = validate_issue_form(form)
        except ValueError as exc:
            return {"status": "report_rejected", "message": str(exc)}
        report = self._file_report(
            signal=signal,
            origin=Origin.MODEL_SKILL,
            tool=str(validated.get("failure_point", {}).get("named_step") or "") or None,
            board=None,
            anchor=f"model:{signal.value}:{validated.get('signal_subcase') or '-'}",
            title=str(validated.get("goal", "agent-reported issue"))[:120],
            description=str(validated.get("hypothesis", ""))[:500],
            refusal_code=None,
            named_remedy=None,
            args_fp=None,
            narrative=validated,
        )
        if report is None:
            return {
                "status": "report_grouped",
                "message": "An equivalent report was already filed recently.",
            }
        return {"status": "report_recorded", "report_id": report["report_id"]}

    def submit_checkin(self, form: Mapping[str, Any]) -> dict[str, Any]:
        from pyocd_debug_mcp.monitor.narrative import validate_checkin_form

        try:
            validated = validate_checkin_form(form)
        except ValueError as exc:
            return {"status": "checkin_rejected", "message": str(exc)}
        # Refresh-then-snapshot via the shared helper -- this call site was the
        # one place that took the snapshot before the refresh, so a checkin's
        # coverage answer could be stale (C-1).
        snapshot = self._refreshed_snapshot()
        summary = self._build_summary(
            trigger="agent-invoked", narrative=validated, snapshot=snapshot
        )
        # A check-in is its own record kind, distinct from a usage snapshot and
        # from a report: it is a health record, never an issue.
        self._append(
            "checkin",
            detail={"summary_id": summary["summary_id"], **self._usage(snapshot)},
        )
        segments = self._ledger.sealed_segments()
        self._delivery.enqueue_segments(segments)
        self._write_artifact_file(summary)
        self._delivery.enqueue_report(summary)
        return {"status": "checkin_recorded", "summary_id": summary["summary_id"]}


__all__ = [
    "CHECKIN_PROMPT",
    "IssueMonitor",
    "MonitorContext",
    "NullMonitor",
    "Observation",
]
