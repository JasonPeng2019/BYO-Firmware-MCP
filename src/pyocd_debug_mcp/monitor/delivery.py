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
import os
import queue
import stat
import tempfile
import threading
import time
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.monitor.block import StalenessBlock
from pyocd_debug_mcp.monitor.ledger import (
    _SEGMENT_DIGITS,
    SegmentInfo,
    file_identity,
    run_is_active,
)
from pyocd_debug_mcp.monitor.paths import StoreRoot
from pyocd_debug_mcp.monitor.reports import report_identity
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
_DELIVERY_STATE_GUARD = threading.RLock()


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return bool(attributes & marker) or bool(getattr(metadata, "st_reparse_tag", 0))


def _fsync_directory(directory: Path) -> None:
    """Persist an atomic replacement's directory entry where POSIX supports it."""

    if os.name != "posix":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_durable_directory(directory: Path) -> None:
    """Create each missing component and persist its entry in the parent."""

    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for component in reversed(missing):
        component.mkdir(exist_ok=True)
        _fsync_directory(component.parent)


@contextmanager
def _delivery_state_file_lock(path: Path):  # type: ignore[no-untyped-def]
    """Serialize receipt-book replacement across server processes."""

    lock_path = path.with_name(f".{path.name}.lock")
    _ensure_durable_directory(lock_path.parent)
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    locked = False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError(f"delivery-state lock is not a private regular file: {lock_path}")
        path_metadata = lock_path.lstat()
        if stat.S_ISLNK(path_metadata.st_mode) or _is_reparse_point(path_metadata):
            raise OSError(f"delivery-state lock must not be a link: {lock_path}")
        if (
            not stat.S_ISREG(path_metadata.st_mode)
            or path_metadata.st_nlink != 1
            or (path_metadata.st_dev, path_metadata.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise OSError(f"delivery-state lock changed while opening: {lock_path}")
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            try:
                fchmod(descriptor, 0o600)
            except OSError:
                pass
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
        elif os.name == "nt":  # pragma: no cover - native Windows path
            import msvcrt

            # Windows permits locking beyond EOF. Do not write a first-use
            # sentinel before acquiring the very lock that serializes writers.
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            locked = True
        current = lock_path.lstat()
        if (
            stat.S_ISLNK(current.st_mode)
            or _is_reparse_point(current)
            or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise OSError(f"delivery-state lock changed before acquisition: {lock_path}")
        yield
    finally:
        if locked and os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        elif locked and os.name == "nt":  # pragma: no cover - native Windows path
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)


def _read_acked_state(path: Path) -> set[str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
        return set()
    acked = raw.get("acked") if isinstance(raw, dict) else None
    return {str(item) for item in acked} if isinstance(acked, list) else set()


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
        self._persistence_guard = _DELIVERY_STATE_GUARD
        self._last_state = DeliveryState.NOT_CONFIGURED
        self._last_at: datetime | None = None
        self._acked: set[str] = set()
        self._report_relocations: dict[str, set[Path]] = {}
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
        with self._persistence_guard, _delivery_state_file_lock(path):
            self._acked = _read_acked_state(path)

    def _refresh_state(self) -> None:
        """Merge receipts written by another live delivery service."""

        path = self._state_path()
        if path is None:
            return
        try:
            with self._persistence_guard, _delivery_state_file_lock(path):
                persisted = _read_acked_state(path)
        except OSError:
            return
        with self._guard:
            self._acked |= persisted

    def _save_state(self, *, remove: frozenset[str] = frozenset()) -> bool:
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
        now-smaller set. A sweep triggers whenever one save crosses a multiple
        of the interval, including a batched jump such as 199 to 201.
        """

        path = self._state_path()
        if path is None:
            # No persisted store: nothing was written, so there is nothing on
            # disk for a prune sweep to reason about either. Leave the
            # in-memory set alone rather than pruning it against a store that
            # cannot confirm anything is actually absent.
            return False
        temporary: Path | None = None
        with self._persistence_guard:
            with _delivery_state_file_lock(path):
                with self._guard:
                    local_acked = set(self._acked)
                persisted = _read_acked_state(path)
                previous_size = len(persisted)
                merged = (persisted | local_acked) - set(remove)
                acked = sorted(merged)
                try:
                    _ensure_durable_directory(path.parent)
                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        dir=path.parent,
                        prefix=f".{path.name}.",
                        suffix=".tmp",
                        delete=False,
                    ) as handle:
                        temporary = Path(handle.name)
                        handle.write(json.dumps({"acked": acked}, sort_keys=True))
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, path)
                    temporary = None
                    _fsync_directory(path.parent)
                except OSError:
                    return False
                finally:
                    if temporary is not None:
                        try:
                            temporary.unlink(missing_ok=True)
                        except OSError:
                            pass
                with self._guard:
                    self._acked = merged
                size = len(acked)
        if size // DELIVERY_STATE_PRUNE_INTERVAL > previous_size // DELIVERY_STATE_PRUNE_INTERVAL:
            self._prune_acked()
        return True

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
            identity for identity in acked_snapshot if not self._identity_file_exists(identity)
        }
        if not prunable:
            return
        with self._guard:
            self._acked -= prunable
        self._save_state(remove=frozenset(prunable))

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
            path = server_data / workspace / f"{run_id}.{segment:0{_SEGMENT_DIGITS}d}.jsonl"
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
            thread = threading.Thread(target=self._run, name="byo-monitor-delivery", daemon=True)
            self._thread = thread
        thread.start()

    def stop(
        self,
        timeout: float = CLOSEOUT_BUDGET_SECONDS,
        *,
        close_transport: bool = True,
    ) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if close_transport:

            def close_best_effort() -> None:
                try:
                    self._transport.close()
                except Exception:  # noqa: BLE001 - teardown is best-effort
                    pass

            closer = threading.Thread(
                target=close_best_effort,
                name="byo-monitor-transport-close",
                daemon=True,
            )
            closer.start()
            closer.join(timeout=max(0.0, deadline - time.monotonic()))

    def retire(self, *, close_transport: bool) -> None:
        """Drain queued recovery work, then retire this service asynchronously.

        Binding must not abandon the unbound service's older recovery backlog,
        but it also must not put a slow sender on the request path. A sentinel
        after the existing jobs provides both properties. A daemon reaper owns
        the optional transport close after the queue has drained.
        """

        thread = self._thread
        if thread is None:
            if close_transport:
                threading.Thread(
                    target=self._transport.close,
                    name="byo-monitor-retired-transport-close",
                    daemon=True,
                ).start()
            return

        def enqueue_sentinel() -> None:
            self._queue.put(None)
            thread.join()
            if close_transport:
                try:
                    self._transport.close()
                except Exception:  # noqa: BLE001 - retired teardown is best-effort
                    pass

        threading.Thread(
            target=enqueue_sentinel,
            name="byo-monitor-delivery-retire",
            daemon=True,
        ).start()

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

    def track_report_relocation(self, identity: str, destination: Path) -> None:
        """Tell an in-flight pre-bind sender where its source body was moved."""

        with self._guard:
            self._report_relocations.setdefault(identity, set()).add(destination)

    def cancel_report_relocation(self, identity: str, destination: Path) -> None:
        """Forget a registered relocation when the corresponding move failed."""

        with self._guard:
            destinations = self._report_relocations.get(identity)
            if destinations is None:
                return
            destinations.discard(destination)
            if not destinations:
                self._report_relocations.pop(identity, None)

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
        self._refresh_state()
        with self._guard:
            acked = set(self._acked)
        active_runs: dict[str, bool] = {}
        for path in candidates:
            stem = path.name[: -len(".jsonl")]
            run_id, _, segment_text = stem.rpartition(".")
            if not run_id or run_id == self._run_id:
                continue
            try:
                segment = int(segment_text)
            except ValueError:
                continue
            active = active_runs.get(run_id)
            if active is None:
                active = run_is_active(directory, run_id)
                active_runs[run_id] = active
            if active:
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
        directories = [server_data / self._workspace / "reports"]
        unbound = server_data / "unbound" / "reports"
        if self._workspace != "unbound":
            directories.append(unbound)
        candidates: list[Path] = []
        for directory in directories:
            if not directory.is_dir():
                continue
            try:
                for path in sorted(directory.glob("*.json")):
                    try:
                        body = json.loads(path.read_text(encoding="utf-8"))
                    except (
                        OSError,
                        UnicodeError,
                        json.JSONDecodeError,
                        ValueError,
                        RecursionError,
                    ):
                        continue
                    identity = report_identity(body) if isinstance(body, dict) else None
                    if identity is None or identity != path.stem:
                        continue
                    session_id = body.get("session_id") if isinstance(body, dict) else None
                    if (
                        isinstance(session_id, str)
                        and session_id != self._run_id
                        and run_is_active(directory.parent, session_id)
                    ):
                        # A different live server still owns this body. Recovery
                        # may claim it only after that process exits and its OS
                        # lease is released.
                        continue
                    candidates.append(path)
            except OSError:
                continue
        self._refresh_state()
        with self._guard:
            acked = set(self._acked)
        found: dict[str, Path] = {}
        for path in candidates:
            if path.stem in acked:
                # Crash/race recovery: ACK persistence precedes deletion. If a
                # bind moved the body between those two acts, delete the moved
                # resident copy rather than excluding it forever.
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            found.setdefault(path.stem, path)
        return tuple(sorted(found.items()))

    def _send_report(self, report: dict[str, Any], *, source_path: Path | None = None) -> None:
        """Send one report/summary body and, if acknowledged, remember and delete it.

        Recorded the same way a segment's ACK is: added to the shared ``_acked``
        set and persisted to ``delivery_state.json`` *before* the local copy is
        removed, so a report already delivered is never resent by a later
        bootup recovery pass, and the same crash-window safety segments get
        (ack recorded and saved, then delete) applies here too.
        """

        identity = report_identity(report)
        if identity is None:
            return
        result = self._transport.send_report(report)
        self._record(result)
        if identity not in result.acked:
            return
        with self._guard:
            self._acked.add(identity)
        if not self._save_state():
            return
        # Delete-on-acknowledgement, mirroring the segment path in ``_perform``:
        # the delivered copy is the durable one, so the local copy is retained
        # only while un-acknowledged.
        server_data = self._store.server_data
        if server_data is None:
            return
        path = source_path or server_data / self._workspace / "reports" / f"{identity}.json"
        with self._guard:
            relocated = self._report_relocations.pop(identity, set())
        for resident in {path, *relocated}:
            try:
                resident.unlink(missing_ok=True)
            except OSError:
                continue

    def _perform(self, job: _Job) -> None:
        if job.kind == "report" and job.report is not None:
            self._send_report(job.report)
            return
        if job.kind == "prior_reports":
            for expected_identity, path in self._prior_report_files():
                try:
                    report = json.loads(path.read_text(encoding="utf-8"))
                except (
                    OSError,
                    UnicodeError,
                    json.JSONDecodeError,
                    ValueError,
                    RecursionError,
                ):
                    continue
                if isinstance(report, dict) and report_identity(report) == expected_identity:
                    try:
                        self._send_report(report, source_path=path)
                    except Exception:  # noqa: BLE001 - isolate one corrupt durable body
                        continue
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
        if not self._save_state():
            return
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
                    self._last_at.isoformat().replace("+00:00", "Z") if self._last_at else None
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
