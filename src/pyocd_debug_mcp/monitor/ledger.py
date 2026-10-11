"""Append-only, per-run activity ledger with a hash chain per segment.

**This ledger records occasions, not calls.** There is deliberately no per-call
record. The only record kinds are ``boot``, ``usage_snapshot`` (every
``counters.USAGE_SNAPSHOT_CADENCE`` calls), ``checkin`` (every
``counters.CHECKIN_CADENCE`` calls, personal builds only), ``report``, and
``close``. Per-call sequence context lives only in the bounded in-memory trail
and is attached to problem reports: problem-watching needs the run-up to a
failure, not a permanent record of every individual call. Each usage snapshot
carries the run's **cumulative** counts, so the latest delivered snapshot alone
answers "how much was used, by which tools, with what outcomes" -- no replay of
earlier snapshots is required.

What the chain does and does not detect, stated plainly because over-claiming it
is the likeliest way this gets misused:

The hash function is public and there is no secret, so anyone with the file, the
algorithm, and the server stopped can edit a record and recompute every subsequent
link, producing a chain that verifies perfectly. They can equally truncate the tail
and leave a valid shorter chain. **The chain detects localized modification -- a
line edited in a text editor, a partial write, disk corruption -- and nothing
more.** It is an accident-and-corruption detector, not a defense against a person
who wants to alter the record and can stop the server first.

A secret key held on this machine would not fix that: the server runs as the user,
so a local key is readable by exactly the party a keyed chain would need to
exclude. Only an external witness upgrades this. Publishing each sealed file's head
off-box makes offline rewriting detectable by someone other than whoever made the
edit, and whole-file delivery publishes that head by construction -- but no real
remote exists yet, so today's honest guarantee is corruption detection only.

The file permissions applied here are hardening, not a boundary. The file's owner
can rewrite them and the server runs as the owner. They stop stray scripts,
accidental clobbering, and any party that is not the owner; they do not stop the
owner.

What the snapshot chain does and does not do against under-reporting
--------------------------------------------------------------------
The motivating threat is a user -- a personal user in particular, who owns their
machine -- pretending they used the tool less than they did. Stated honestly, in
three tiers:

1. **Casual under-reporting is defeated.** Cumulative counts plus the two-week
   staleness block mean staying offline or dropping snapshots cannot lower the
   total: the next snapshot the block forces out still carries the true running
   total, and the per-run chain makes a decrease, or a gap in the snapshot
   sequence, detectable.
2. **Deliberate post-hoc editing is detection-not-prevention, and only once a
   real remote exists.** A machine owner can edit a snapshot's count down and
   recompute the chain. That is caught only by an off-box witness that recorded
   the delivered head, which does not exist in the filler era -- so the real
   under-report defense turns on at OAuth cutover, not before.
3. **Source-level forgery is neither prevented nor detected, in any era.** A
   user who tampers with the counter or the server binary can emit a false-low
   count at the source, and no chain over the output can catch a lie told before
   the record was written.

Any adversarial use of these counts -- usage-based billing, for instance -- must
be documented with this ceiling. The number is **not** unforgeable by the
machine's owner.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import blake2b
from pathlib import Path
from typing import Any, TextIO

from pyocd_debug_mcp.monitor.paths import StoreRoot

# Bounds a pathological worst case, not the typical cost: measured hardening
# (icacls on Windows, chmod on POSIX) runs at a median ~26 ms and a max ~32 ms,
# once per segment (i.e. once per USAGE_SNAPSHOT_CADENCE calls) -- amortised,
# well under a millisecond per tool call, and this path runs after dispatch
# returns, outside every board lock. This timeout exists only to cap a wedged
# subprocess (a hung icacls, AV interception, a contended disk), not to budget
# for the normal case.
_HARDENING_TIMEOUT_SECONDS = 0.5
_SEGMENT_DIGITS = 4
_RUN_LEASE_SUFFIX = ".active.lock"


class Hardening(str, Enum):
    APPLIED = "applied"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    NOT_ATTEMPTED = "not_attempted"


class VerificationOutcome(str, Enum):
    """Verification states, reported distinctly rather than collapsed."""

    VERIFIED = "verified"
    CHAIN_INVALID = "chain_invalid"
    TRUNCATED = "truncated_vs_published_head"
    RUN_ABSENT = "run_absent"
    IMPOSSIBLE = "verification_impossible"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _link(prev: str, record: dict[str, Any]) -> str:
    body = _canonical(record)
    return blake2b((prev + body).encode("utf-8"), digest_size=32).hexdigest()


def file_identity(workspace: str, run_id: str, segment: int) -> str:
    """Return the stable identity used for delivery, ACK, and receiver dedup."""

    return f"{workspace}:{run_id}:{segment:0{_SEGMENT_DIGITS}d}"


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return bool(attributes & marker) or bool(getattr(metadata, "st_reparse_tag", 0))


def _fsync_directory(directory: Path) -> None:
    """Persist newly created ledger directory entries on POSIX."""

    if os.name != "posix":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_durable_directory(directory: Path) -> None:
    """Create missing components and persist each entry in its parent."""

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


def _run_lease_path(directory: Path, run_id: str) -> Path:
    """Return a fixed-width filename even if a caller supplied an odd run ID."""

    token = blake2b(run_id.encode("utf-8", "surrogatepass"), digest_size=16).hexdigest()
    return directory / f".{token}{_RUN_LEASE_SUFFIX}"


def _lock_run_lease(descriptor: int, *, blocking: bool) -> bool:
    """Acquire a process-scoped lease without writing shared sentinel bytes."""

    try:
        if os.name == "posix":
            import fcntl

            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(descriptor, operation)
        elif os.name == "nt":  # pragma: no cover - native Windows path
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            msvcrt.locking(descriptor, mode, 1)
        else:  # pragma: no cover - supported runtimes are POSIX/Windows
            return False
    except (BlockingIOError, OSError):
        return False
    return True


def _unlock_run_lease(descriptor: int) -> None:
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        elif os.name == "nt":  # pragma: no cover - native Windows path
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _open_run_lease(directory: Path, run_id: str, *, create: bool) -> int | None:
    """Open and validate the private lease inode without following a link."""

    path = _run_lease_path(directory, run_id)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_ISLNK(current.st_mode)
            or _is_reparse_point(current)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise OSError("run lease is not a private regular file")
        if create:
            # The lock only becomes a crash-durable ownership marker after its
            # directory entry is persisted. Syncing an existing entry is cheap
            # and avoids a racy pre-open existence probe.
            _fsync_directory(directory)
        # Creation already requests 0600. Do not chmod a pre-existing path:
        # even after the single-link check, a concurrent hard-link insertion
        # could otherwise redirect a metadata change onto an external peer.
        return descriptor
    except OSError:
        os.close(descriptor)
        return None


def run_is_active(directory: Path, run_id: str) -> bool:
    """Return true unless a prior run's lease can be proved inactive.

    Missing leases are legacy files and are recoverable. An existing but unsafe
    or unreadable lease is treated conservatively as active: skipping recovery is
    preferable to deleting a segment that may still have a writer.
    """

    lease = _run_lease_path(directory, run_id)
    if not os.path.lexists(lease):
        return False
    descriptor = _open_run_lease(directory, run_id, create=False)
    if descriptor is None:
        return True
    try:
        if not _lock_run_lease(descriptor, blocking=False):
            return True
        _unlock_run_lease(descriptor)
        return False
    finally:
        os.close(descriptor)


def _harden(path: Path) -> Hardening:
    """Apply the most restrictive access that still permits append.

    Called *after* the append handle is open. Windows grants file access at open
    time, so an already-open handle survives the deny -- but ``open(path, "a")``
    requests GENERIC_WRITE, which includes WRITE_DATA, so hardening first would
    make the ledger unwritable.
    """

    if sys.platform == "win32":
        user = os.environ.get("USERNAME") or ""
        if not user:
            return Hardening.FAILED
        try:
            completed = subprocess.run(
                [
                    "icacls",
                    str(path),
                    "/deny",
                    f"{user}:(WD)",
                    "/grant",
                    f"{user}:(AD)",
                ],
                timeout=_HARDENING_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return Hardening.FAILED
        return Hardening.APPLIED if completed.returncode == 0 else Hardening.FAILED
    try:
        os.chmod(path, 0o600)
    except OSError:
        return Hardening.FAILED
    # POSIX append-only requires chattr +a, i.e. root. Do not claim otherwise.
    return Hardening.UNSUPPORTED


@dataclass(frozen=True, slots=True)
class SegmentInfo:
    path: Path
    identity: str
    segment: int


class SegmentLedger:
    """One sealed file per run segment; append and unlink are the only writes.

    A record is durable once appended -- nothing depends on a later flush or on any
    shutdown path. Records are never removed from a file and a file is never
    rewritten in place, so a deleted file leaves no dangling back-link behind and
    every file that remains verifies completely on its own.
    """

    def __init__(self, store: StoreRoot, workspace: str, run_id: str) -> None:
        self._store = store
        self._workspace = workspace
        self._run_id = run_id
        self._guard = threading.Lock()
        self._segment = 0
        self._seq = 0
        self._head = ""
        self._handle: TextIO | None = None
        self._path: Path | None = None
        self._hardening = Hardening.NOT_ATTEMPTED
        self._total_appended = 0
        self._sealed: list[SegmentInfo] = []
        self._last_error: str | None = None
        self._lease_descriptor: int | None = None
        if self._store.server_data is not None:
            self.activate()

    # -- introspection ---------------------------------------------------

    @property
    def head(self) -> str:
        with self._guard:
            return self._head

    @property
    def total_appended(self) -> int:
        with self._guard:
            return self._total_appended

    @property
    def hardening(self) -> str:
        with self._guard:
            return self._hardening.value

    @property
    def last_error(self) -> str | None:
        with self._guard:
            return self._last_error

    @property
    def current_segment(self) -> int:
        with self._guard:
            return self._segment

    def sealed_segments(self) -> tuple[SegmentInfo, ...]:
        """Return segments that are closed and therefore safe to deliver."""

        with self._guard:
            return tuple(self._sealed)

    def resident_files(self) -> tuple[Path, ...]:
        with self._guard:
            paths = [info.path for info in self._sealed]
            if self._path is not None:
                paths.append(self._path)
            return tuple(p for p in paths if p.exists())

    # -- writing ---------------------------------------------------------

    def _directory(self) -> Path | None:
        server_data = self._store.server_data
        return None if server_data is None else server_data / self._workspace

    def _activate_locked(self) -> bool:
        if self._lease_descriptor is not None:
            return True
        directory = self._directory()
        if directory is None:
            return False
        try:
            _ensure_durable_directory(directory)
        except OSError as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            return False
        descriptor = _open_run_lease(directory, self._run_id, create=True)
        if descriptor is None or not _lock_run_lease(descriptor, blocking=False):
            if descriptor is not None:
                os.close(descriptor)
            self._last_error = "OSError: another process owns this run ledger"
            return False
        self._lease_descriptor = descriptor
        return True

    def activate(self) -> bool:
        """Claim this run before it creates reports or writable segments."""

        with self._guard:
            return self._activate_locked()

    def _open_segment_locked(self) -> bool:
        directory = self._directory()
        if directory is None:
            return False
        if not self._activate_locked():
            return False
        self._segment += 1
        name = f"{self._run_id}.{self._segment:0{_SEGMENT_DIGITS}d}.jsonl"
        path = directory / name
        handle: TextIO | None = None
        try:
            handle = path.open("a", encoding="utf-8")
            # A file fsync below persists record bytes, not necessarily the new
            # filename. Persist the directory entry before append can succeed.
            _fsync_directory(directory)
        except OSError as exc:
            if handle is not None:
                handle.close()
            self._segment -= 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            descriptor = self._lease_descriptor
            self._lease_descriptor = None
            if descriptor is not None:
                _unlock_run_lease(descriptor)
                os.close(descriptor)
            return False
        self._handle = handle
        self._path = path
        if self._segment == 1:
            self._head = f"genesis:{self._run_id}"
        # Segment N>1 keeps the predecessor's head in its opening link, so the
        # run's chain stays verifiable end to end wherever the full set exists.
        return True

    def _write_locked(self, record: dict[str, Any]) -> bool:
        handle = self._handle
        path = self._path
        if handle is None or path is None:
            return False
        try:
            handle.write(_canonical(record))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        except (OSError, ValueError) as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            return False
        if self._hardening is Hardening.NOT_ATTEMPTED:
            # Harden only after the handle exists and has written, so the deny
            # cannot lock out our own writer. Synchronous and in-lock, and
            # deliberately so: AC-45 requires the file be created with in-place
            # overwrite denied, and the only way to make that true rather than
            # eventually-true is to not return from the write that creates the
            # window until hardening has landed. Measured cost is a median
            # ~26 ms (max ~32 ms observed), once per segment -- i.e. once per
            # USAGE_SNAPSHOT_CADENCE calls, well under a millisecond amortised
            # per call -- and this runs after dispatch returns, outside every
            # board lock, so N-3's hard latency clause is untouched.
            # _HARDENING_TIMEOUT_SECONDS bounds the pathological case (a wedged
            # icacls), not the typical one.
            self._hardening = _harden(path)
        return True

    def append(self, kind: str, *, detail: dict[str, Any] | None = None) -> bool:
        """Append one occasion record. Returns whether it reached disk.

        A generic envelope plus a kind-specific ``detail``. There are no
        per-call columns here (no tool, no arguments, no outcome) because there
        is no per-call record -- what a snapshot carries instead is the run's
        cumulative counts, which answer the same question without an entry per
        call.
        """

        with self._guard:
            if self._handle is None and not self._open_segment_locked():
                return False
            self._seq += 1
            record: dict[str, Any] = {
                "seq": self._seq,
                "run_id": self._run_id,
                "workspace": self._workspace,
                "segment": self._segment,
                "ts": _timestamp(),
                "kind": kind,
                "detail": detail or {},
                "prev": self._head,
            }
            record["hash"] = _link(self._head, record)
            if not self._write_locked(record):
                self._seq -= 1
                return False
            self._head = record["hash"]
            self._total_appended += 1
            return True

    def _close_locked(self) -> SegmentInfo | None:
        handle = self._handle
        path = self._path
        if handle is None or path is None:
            return None
        try:
            handle.close()
        except OSError:
            pass
        info = SegmentInfo(
            path=path,
            identity=file_identity(self._workspace, self._run_id, self._segment),
            segment=self._segment,
        )
        self._sealed.append(info)
        self._handle = None
        self._path = None
        self._hardening = Hardening.NOT_ATTEMPTED
        return info

    def roll(self) -> SegmentInfo | None:
        """Seal the current segment and open its successor.

        The roll cadence is the only-local window: a file still being appended to
        cannot be delivered or deleted, so without rolling the window would equal
        the whole run.

        Called from the usage-snapshot tick, so the roll boundary *is*
        ``counters.USAGE_SNAPSHOT_CADENCE`` rather than a second number that
        could drift away from it. Every seal therefore aligns with a snapshot: a
        sealed file is pushed and its acknowledgement re-anchors the staleness
        interval on the same beat.
        """

        with self._guard:
            info = self._close_locked()
            self._open_segment_locked()
            return info

    def seal(self) -> SegmentInfo | None:
        """Close the current segment with no successor."""

        with self._guard:
            info = self._close_locked()
            descriptor = self._lease_descriptor
            self._lease_descriptor = None
            if descriptor is not None:
                _unlock_run_lease(descriptor)
                os.close(descriptor)
            return info

    def forget(self, identity: str) -> None:
        """Drop a sealed segment from the resident set after its file was unlinked."""

        with self._guard:
            self._sealed = [info for info in self._sealed if info.identity != identity]


def verify_file(path: Path) -> VerificationOutcome:
    """Recompute one file's chain.

    A segment whose opening link names a head that is not present locally is
    normal, not a finding: its predecessor was delivered and deleted. Missing
    files are never a finding either -- deletion after acknowledgement is the
    system's only cleanup mechanism, so absence is the expected steady state.
    """

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return VerificationOutcome.RUN_ABSENT
    except UnicodeError:
        return VerificationOutcome.CHAIN_INVALID
    prev: str | None = None
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError, RecursionError):
            return VerificationOutcome.CHAIN_INVALID
        if not isinstance(record, dict) or "hash" not in record or "prev" not in record:
            return VerificationOutcome.CHAIN_INVALID
        claimed = record.pop("hash")
        previous = record["prev"]
        if not isinstance(claimed, str) or not isinstance(previous, str):
            return VerificationOutcome.CHAIN_INVALID
        try:
            recomputed = _link(previous, record)
        except (TypeError, ValueError, UnicodeError):
            return VerificationOutcome.CHAIN_INVALID
        if recomputed != claimed:
            return VerificationOutcome.CHAIN_INVALID
        if prev is not None and previous != prev:
            return VerificationOutcome.CHAIN_INVALID
        prev = claimed
    # Internally consistent. Without a published head off-box we cannot rule out a
    # wholesale offline rewrite, so this is *not* reported as verified.
    return VerificationOutcome.IMPOSSIBLE


def verify_prior_runs(store: StoreRoot, workspace: str, current_run_id: str) -> dict[str, str]:
    """Verify every resident file from a previous run of this workspace."""

    server_data = store.server_data
    if server_data is None:
        return {}
    directory = server_data / workspace
    if not directory.is_dir():
        return {}
    outcomes: dict[str, str] = {}
    try:
        candidates = sorted(directory.glob("*.jsonl"))
    except OSError:
        return {}
    for path in candidates:
        if path.name.startswith(f"{current_run_id}."):
            continue
        stem = path.name[: -len(".jsonl")]
        run_id, separator, _segment = stem.rpartition(".")
        if separator and run_id and run_is_active(directory, run_id):
            continue
        outcomes[path.name] = verify_file(path).value
    return outcomes


__all__ = [
    "Hardening",
    "SegmentInfo",
    "SegmentLedger",
    "VerificationOutcome",
    "file_identity",
    "run_is_active",
    "verify_file",
    "verify_prior_runs",
]
