"""Background delivery: bootup recovery, periodic, and closeout.

The three occasions are deliberately unbalanced. **Bootup recovery** is the
workhorse: it is not capped by a client kill grace, it is a deterministic point,
and running it after readiness means it neither charges the startup deadline nor
blocks the new session's early calls. **Closeout** is a cheap best-effort attempt,
bounded by whatever grace the client allows, retained for the one thing only it
can reach -- the current run's tail, including the force-killed session that is
precisely the one worth having. **Periodic** is kept light and fails silently.

Durability never depends on any of them. A record is safe once appended; delivery
decides where the durable copy lives, not whether one exists.

Delivery is at-least-once with stable identity. Crash timing can always produce a
resend, so the receiver deduplicates; an implementation reaching for exactly-once
would lose records instead.

**The sender is decoupled from the request path.** The server's obligation for any
record ends at the local append; once a file is sealed it is handed to this worker
and the server moves on. Nothing on the request path ever waits on a send, and a
stuck sender -- a hung socket, a wedged retry, an unreachable endpoint -- must stay
invisible to tool execution. When the queue is full the *handoff* is dropped, never
the record: the sealed file stays in ``server_data`` and the next boot's recovery
ships it. The one place a send's outcome legitimately reaches the server is the
staleness backstop, and even there it is a local timestamp comparison against the
delivery anchor, never a network wait.

This drop-the-handoff-not-the-record contract covers report and summary bodies
too, not only ledger segments: every report and summary is written to disk
(``reports/<report_id|summary_id>.json``) before it is ever enqueued, and bootup
recovery resends whatever of those was never acknowledged, the same way it
resends undelivered segments (``_prior_report_files``). Once acknowledged, the
local copy is deleted -- symmetric with segments, and for the same reason: the
delivered copy is the durable one, so local storage need only hold what has not
yet been pushed.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.monitor.block import StalenessBlock
from pyocd_debug_mcp.monitor.ledger import _SEGMENT_DIGITS, SegmentInfo, file_identity
from pyocd_debug_mcp.monitor.paths import StoreRoot
from pyocd_debug_mcp.monitor.transport import (
    DeliveryResult,
    DeliveryState,
    NullTransport,
    Transport,
)

STATE_FILENAME = "delivery_state.json"
QUEUE_MAX = 256

# Measured, not tunable. The client dictates the kill grace -- observed at ~500 ms
# for the tightest target -- and this budget is fit *inside* it. Raising it does not
# buy more time; past the grace the process is force-killed mid-send regardless, and
# whatever did not finish is carried by bootup recovery.
CLIENT_KILL_GRACE_SECONDS = 0.5
CLOSEOUT_BUDGET_SECONDS = 0.4

# ``_acked`` only ever grows within a single save; this is what keeps
# ``delivery_state.json`` from becoming permanent dead weight. Triggered by the
# *total* size of ``_acked``, not adds-this-process, so a short-lived server that
# adds far fewer than this many entries in one run still gets pruned across many
# restarts -- see the sweep in ``_prune_acked``. Deliberately its own
# constant, not a reuse of ``USAGE_SNAPSHOT_CADENCE`` or any other cadence value:
# this governs receipt-book housekeeping, not counter/report cadence, and the two
# must be free to change independently.
DELIVERY_STATE_PRUNE_INTERVAL = 200


@dataclass(frozen=True, slots=True)
class _Job:
    kind: str  # "files" | "report" | "bootup" | "prior_reports"
    files: tuple[tuple[str, Path], ...] = ()
    report: dict[str, Any] | None = None


class DeliveryService:
    """One daemon thread, one bounded queue. Producers never block."""

    def __init__(
        self,
        store: StoreRoot,
        workspace: str,
        run_id: str,
        transport: Transport | None = None,
        block: StalenessBlock | None = None,
    ) -> None:
        self._store = store
        self._workspace = workspace
        self._run_id = run_id
        self._transport: Transport = transport or NullTransport()
        self._block = block
        self._queue: queue.Queue[_Job | None] = queue.Queue(maxsize=QUEUE_MAX)
        self._guard = threading.Lock()
        self._last_state = DeliveryState.NOT_CONFIGURED
        self._last_at: datetime | None = None
        self._acked: set[str] = set()
        self._dropped = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._load_state()

    # -- persistence -----------------------------------------------------

    def _state_path(self) -> Path | None:
        server_data = self._store.server_data
        return None if server_data is None else server_data / STATE_FILENAME

    def _load_state(self) -> None:
        path = self._state_path()
        if path is None:
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        acked = raw.get("acked") if isinstance(raw, dict) else None
        if isinstance(acked, list):
            self._acked = {str(item) for item in acked}

    def _save_state(self) -> None:
        """Persist ``_acked``, then sweep it if this save crossed a prune boundary.

        The prune check lives here, not in a separate wrapper, so that *every*
        call site which adds to ``_acked`` and saves gets the sweep for free --
        there is no second method to remember to call instead. Triggered by
        the *total* size of ``_acked``, not by adds made this process's
        lifetime: a short-lived process might add far fewer than
        ``DELIVERY_STATE_PRUNE_INTERVAL`` entries in one run, and a
        session-scoped trigger would mean the file never gets pruned across
        many short restarts.

        ``_prune_acked`` calls back into this method to persist what it
        removed; that second pass rechecks the same boundary condition on the
        now-smaller set, which in the overwhelmingly common case is no longer
        a multiple of the interval and simply returns. On the rare exact
        coincidence where it is, the recursive prune has strictly fewer
        candidates than the last pass (nothing new went missing on disk in
        between), so it terminates in at most a few extra passes, never
        indefinitely.
        """

        path = self._state_path()
        if path is None:
            # No persisted store: nothing was written, so there is nothing on
            # disk for a prune sweep to reason about either. Leave the
            # in-memory set alone rather than pruning it against a store that
            # cannot confirm anything is actually absent.
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"acked": sorted(self._acked)}, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            pass
        with self._guard:
            size = len(self._acked)
        if size and size % DELIVERY_STATE_PRUNE_INTERVAL == 0:
            self._prune_acked()

    def _prune_acked(self) -> None:
        """Drop ``_acked`` entries whose backing file is confirmed absent.

        The receipt book exists only to survive the crash window between "ACK
        recorded and saved" and "file actually unlinked" (see the module
        docstring). Once a file is gone the receipt has done its one job and
        provides no further value -- but an entry whose file *still exists*
        (the crash-window case itself) must never be dropped. Age and set size
        are only what *triggers* a sweep, never grounds for dropping an entry
        by themselves; only confirmed absence is.
        """

        with self._guard:
            acked_snapshot = set(self._acked)
        prunable = {
            identity
            for identity in acked_snapshot
            if not self._identity_file_exists(identity)
        }
        if not prunable:
            return
        with self._guard:
            self._acked -= prunable
        self._save_state()

    def _identity_file_exists(self, identity: str) -> bool:
        """Return whether the file an ``_acked`` identity names is still on disk.

        Two identity shapes, two reconstructions:

        * A segment identity (``workspace:run_id:segment``, 3 colon-separated
          parts -- see ``file_identity`` in ``ledger.py``) embeds its
          workspace, so the path is direct:
          ``server_data/<workspace>/<run_id>.<segment>.jsonl``, the same
          naming ``_prior_run_files`` reconstructs.
        * A report/summary identity (``rpt-*`` / ``sum-*``) has no embedded
          workspace -- ``delivery_state.json`` is one file shared across every
          workspace, but report bodies live per-workspace
          (``server_data/<workspace>/reports/<identity>.json``). Every
          workspace directory under ``server_data`` is checked for a matching
          file; only if it is absent from *all* of them is the identity
          prunable.

        Absence cannot always be confirmed (``server_data`` unavailable, a
        workspace listing failing, a malformed segment identity) -- those
        cases report "exists" so a live entry is never wrongly dropped; the
        one clean way to report "confirmed absent" is to actually not find it.
        """

        server_data = self._store.server_data
        if server_data is None:
            # An unavailable store tells us nothing about whether the file
            # exists -- it only means we cannot check. Ambiguous, not
            # confirmed-absent, so report "exists" per the contract above.
            return True

        parts = identity.split(":")
        if len(parts) == 3:
            workspace, run_id, segment_text = parts
            try:
                segment = int(segment_text)
            except ValueError:
                return True
            path = (
                server_data
                / workspace
                / f"{run_id}.{segment:0{_SEGMENT_DIGITS}d}.jsonl"
            )
            try:
                return path.exists()
            except OSError:
                return True

        try:
            workspace_dirs = [entry for entry in server_data.iterdir() if entry.is_dir()]
        except OSError:
            return True
        for workspace_dir in workspace_dirs:
            try:
                found = (workspace_dir / "reports" / f"{identity}.json").exists()
            except OSError:
                return True
            if found:
                return True
        return False

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        with self._guard:
            if self._thread is not None:
                return
            thread = threading.Thread(
                target=self._run, name="byo-monitor-delivery", daemon=True
            )
            self._thread = thread
        thread.start()

    def stop(self, timeout: float = CLOSEOUT_BUDGET_SECONDS) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        try:
            self._transport.close()
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass

    def close_for_shutdown(
        self, segments: Sequence[SegmentInfo], budget: float = CLOSEOUT_BUDGET_SECONDS
    ) -> None:
        """Drain and stop inside one shared deadline, not two independent ones.

        ``drain_for_closeout`` and ``stop`` each block on their own join with a
        default of ``CLOSEOUT_BUDGET_SECONDS``; composing them naively (call one,
        then the other, each with the full default) lets the two timeouts add,
        so the combined wait can run to ~2x the constant -- comfortably past
        ``CLIENT_KILL_GRACE_SECONDS``, which is exactly the invariant the
        constant exists to hold. This spends one ``budget`` across both steps:
        whatever the drain does not use is what is left for the stop, and if the
        drain alone exhausts it the stop still runs, just with no time left to
        wait -- the daemon thread is a daemon precisely so an unjoined exit here
        is safe, and anything unfinished is carried by the next boot's recovery.
        """

        deadline = time.monotonic() + budget
        self.drain_for_closeout(segments, budget=max(0.0, deadline - time.monotonic()))
        self.stop(timeout=max(0.0, deadline - time.monotonic()))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if job is None:
                break
            try:
                self._perform(job)
            except Exception:  # noqa: BLE001 - delivery never crashes the server
                pass

    # -- producers (never block a caller) --------------------------------

    def _enqueue(self, job: _Job) -> None:
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            # Dropping the handoff is not dropping the record. The sealed file is
            # still on disk and un-ACKed, so bootup recovery finds and ships it.
            # Blocking here instead would put a stalled sender on the request path.
            with self._guard:
                self._dropped += 1

    def enqueue_segments(self, segments: Sequence[SegmentInfo]) -> None:
        with self._guard:
            acked = set(self._acked)
        pending = tuple(
            (info.identity, info.path)
            for info in segments
            if info.identity not in acked and info.path.exists()
        )
        if pending:
            self._enqueue(_Job("files", files=pending))

    def enqueue_report(self, report: dict[str, Any]) -> None:
        self._enqueue(_Job("report", report=report))

    def enqueue_bootup(self) -> None:
        """Deliver prior runs' sealed files and undelivered report/summary
        bodies, after readiness, asynchronously.
        """

        self._enqueue(_Job("bootup"))
        self._enqueue(_Job("prior_reports"))

    # -- work ------------------------------------------------------------

    def _prior_run_files(self) -> tuple[tuple[str, Path], ...]:
        server_data = self._store.server_data
        if server_data is None:
            return ()
        directory = server_data / self._workspace
        if not directory.is_dir():
            return ()
        found: list[tuple[str, Path]] = []
        try:
            candidates = sorted(directory.glob("*.jsonl"))
        except OSError:
            return ()
        with self._guard:
            acked = set(self._acked)
        for path in candidates:
            stem = path.name[: -len(".jsonl")]
            run_id, _, segment_text = stem.rpartition(".")
            if not run_id or run_id == self._run_id:
                continue
            try:
                segment = int(segment_text)
            except ValueError:
                continue
            identity = file_identity(self._workspace, run_id, segment)
            if identity in acked:
                continue
            found.append((identity, path))
        return tuple(found)

    def _prior_report_files(self) -> tuple[tuple[str, Path], ...]:
        """Report/summary bodies on disk that have not yet been acknowledged.

        Mirrors ``_prior_run_files``: a body left in ``reports/`` whose identity
        (``report_id`` or ``summary_id``, both already used as the delivery
        identity) is not in ``_acked`` is a dropped or interrupted handoff, and
        bootup recovery resends it -- the same contract F-167 gives sealed
        ledger segments, extended to report/summary bodies (C-4). Same as a
        segment, the local copy *is* unlinked once ACKed (see ``_send_report``):
        the delivered copy is the durable one, so a body only lingers here while
        it is still un-acknowledged -- which is exactly the set this scans for.
        """

        server_data = self._store.server_data
        if server_data is None:
            return ()
        directory = server_data / self._workspace / "reports"
        if not directory.is_dir():
            return ()
        try:
            candidates = sorted(directory.glob("*.json"))
        except OSError:
            return ()
        with self._guard:
            acked = set(self._acked)
        return tuple(
            (path.stem, path) for path in candidates if path.stem not in acked
        )

    def _send_report(self, report: dict[str, Any]) -> None:
        """Send one report/summary body and, if acknowledged, remember and delete it.

        Recorded the same way a segment's ACK is: added to the shared ``_acked``
        set and persisted to ``delivery_state.json`` *before* the local copy is
        removed, so a report already delivered is never resent by a later
        bootup recovery pass, and the same crash-window safety segments get
        (ack recorded and saved, then delete) applies here too.
        """

        identity = str(report.get("report_id") or report.get("summary_id") or "")
        result = self._transport.send_report(report)
        self._record(result)
        if not identity or identity not in result.acked:
            return
        with self._guard:
            self._acked.add(identity)
        self._save_state()
        # Delete-on-acknowledgement, mirroring the segment path in ``_perform``:
        # the delivered copy is the durable one, so the local copy is retained
        # only while un-acknowledged.
        server_data = self._store.server_data
        if server_data is None:
            return
        path = server_data / self._workspace / "reports" / f"{identity}.json"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return

    def _perform(self, job: _Job) -> None:
        if job.kind == "report" and job.report is not None:
            self._send_report(job.report)
            return
        if job.kind == "prior_reports":
            for _, path in self._prior_report_files():
                try:
                    report = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(report, dict):
                    self._send_report(report)
            return
        items = self._prior_run_files() if job.kind == "bootup" else job.files
        if not items:
            return
        result = self._transport.send_files(items)
        self._record(result)
        if not result.acked:
            return
        with self._guard:
            self._acked |= set(result.acked)
        self._save_state()
        for identity, path in items:
            if identity not in result.acked:
                continue
            # Delete-on-acknowledgement: the delivered copy is the durable one, so
            # local storage holds only what has not yet been pushed. This is the
            # system's only deletion mechanism.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue

    def _record(self, result: DeliveryResult) -> None:
        with self._guard:
            self._last_state = result.state
            self._last_at = result.at
        if result.at is None or self._block is None:
            return
        if result.state in (DeliveryState.SENT, DeliveryState.FILLER_SIMULATED):
            # A filler acknowledgement anchors the interval exactly as a real one
            # will, so the whole staleness machine runs for real before cutover --
            # tagged as filler so nobody concludes a real off-box copy exists.
            self._block.refresh(self._transport.name, self._transport.origin, result.at)

    # -- introspection ---------------------------------------------------

    def undelivered(self, segments: Sequence[SegmentInfo]) -> int:
        with self._guard:
            acked = set(self._acked)
        return sum(1 for info in segments if info.identity not in acked)

    def describe(self) -> dict[str, Any]:
        with self._guard:
            return {
                "transport": self._transport.name,
                "origin": self._transport.origin,
                "state": self._last_state.value,
                "last_at": (
                    self._last_at.isoformat().replace("+00:00", "Z")
                    if self._last_at
                    else None
                ),
                "acked_files": len(self._acked),
                "dropped_jobs": self._dropped,
                "durable_off_box": self._last_state is DeliveryState.SENT,
                "off_box_note": (
                    "no off-box copy exists; local permanence is not provided"
                    if self._last_state is not DeliveryState.SENT
                    else "delivered off-box"
                ),
            }

    def drain_for_closeout(
        self, segments: Sequence[SegmentInfo], budget: float = CLOSEOUT_BUDGET_SECONDS
    ) -> None:
        """Best-effort flush inside the client's kill grace.

        Bounded by the client, not chosen by us: past that grace the process is
        force-killed mid-send regardless, so a larger budget buys nothing but the
        risk of being killed anyway. Anything unfinished is carried by bootup
        recovery on the next start, which is why exceeding the budget is safe.

        The send runs on a short-lived worker so a hung or slow transport cannot
        hold the exit past the budget. Abandoning it is harmless: durability came
        from the append, not from this.
        """

        with self._guard:
            acked = set(self._acked)
        pending = tuple(
            (info.identity, info.path)
            for info in segments
            if info.identity not in acked and info.path.exists()
        )
        if not pending:
            return

        def _send() -> None:
            try:
                self._perform(_Job("files", files=pending))
            except Exception:  # noqa: BLE001 - closeout failure changes nothing
                pass

        worker = threading.Thread(target=_send, name="byo-monitor-closeout", daemon=True)
        worker.start()
        worker.join(timeout=budget)


__all__ = [
    "CLIENT_KILL_GRACE_SECONDS",
    "CLOSEOUT_BUDGET_SECONDS",
    "DELIVERY_STATE_PRUNE_INTERVAL",
    "STATE_FILENAME",
    "DeliveryService",
]
