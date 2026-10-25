"""Owned subprocess groups with strict argv and recoverable marker records."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import tempfile
import uuid
from _thread import LockType
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, IO, Sequence

import psutil


def _default_runs_root() -> Path:
    if configured := os.environ.get("PYOCD_MCP_RUNS_ROOT"):
        return Path(configured).expanduser()
    try:
        return Path.home() / ".pyocd-debug-mcp" / "runs"
    except RuntimeError:
        return Path(tempfile.gettempdir()) / "pyocd-debug-mcp-runs"


DEFAULT_MARKER_ROOT = _default_runs_root() / "owned-processes"
_WINDOWS_JOB_HANDLES: dict[int, int] = {}
_WINDOWS_JOB_GUARD = threading.Lock()
_POSIX_RETAINED: dict[int, _OwnedPosixPopen] = {}
_POSIX_RETAINED_GUARD = threading.Lock()
DEFAULT_PROCESS_GROUP_CLEANUP_GRACE_SECONDS = 0.5
# Windows can consume one grace while waiting for Job active-zero and a second
# after that kernel proof while reaping the Python Popen leader. POSIX can
# consume one grace each while waiting after SIGTERM, waiting after SIGKILL,
# and confirming a terminal retained group (then its leader reap) or PGID absence.
# This exported maximum is max(Windows two phases, POSIX three phases), the
# implementation's real cross-platform worst-case cleanup budget for one
# default ``run_owned`` child, not an independent timeout policy.
MAX_OWNED_PROCESS_CLEANUP_SECONDS = (
    DEFAULT_PROCESS_GROUP_CLEANUP_GRACE_SECONDS
    + DEFAULT_PROCESS_GROUP_CLEANUP_GRACE_SECONDS
    + DEFAULT_PROCESS_GROUP_CLEANUP_GRACE_SECONDS
)


class ProcessIdentityUnavailable(RuntimeError):
    """Raised when process existence cannot be distinguished from inaccessibility."""


def validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, str | bytes) or not argv:
        raise ValueError("argv must be a non-empty sequence of explicit arguments")
    values = tuple(argv)
    if not isinstance(values[0], str) or not values[0] or "\x00" in values[0]:
        raise ValueError("argv executable must be a non-empty NUL-free string")
    if any(not isinstance(value, str) or "\x00" in value for value in values[1:]):
        raise ValueError("every argv argument must be a NUL-free string")
    return values


def process_group_options(platform: str | None = None) -> dict[str, object]:
    selected = platform or os.name
    if selected == "nt":
        suspended = getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP") | suspended}
    return {"start_new_session": True}


def _create_windows_kill_job(process: subprocess.Popen[Any]) -> int:
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = getattr(ctypes, "windll").kernel32
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(getattr(ctypes, "get_last_error")(), "CreateJobObjectW failed")
    information = EXTENDED_LIMIT_INFORMATION()
    information.BasicLimitInformation.LimitFlags = 0x00002000
    process_handle = wintypes.HANDLE(int(getattr(process, "_handle")))
    if not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(information), ctypes.sizeof(information)
    ) or not kernel32.AssignProcessToJobObject(job, process_handle):
        error = getattr(ctypes, "get_last_error")()
        kernel32.CloseHandle(job)
        raise OSError(error, "Unable to assign owned subprocess to a kill-on-close job")
    return int(job)


def _resume_windows_process(process: subprocess.Popen[Any]) -> None:
    import ctypes
    from ctypes import wintypes

    resume = getattr(ctypes, "windll").ntdll.NtResumeProcess
    resume.argtypes = (wintypes.HANDLE,)
    resume.restype = ctypes.c_long
    status = resume(wintypes.HANDLE(int(getattr(process, "_handle"))))
    if status != 0:
        raise OSError(status, "NtResumeProcess failed for owned subprocess")


def _close_windows_job(pid: int, handle: int, *, terminate: bool, deadline: float) -> bool:
    import ctypes
    from ctypes import wintypes

    class BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    kernel32 = getattr(ctypes, "windll").kernel32
    if terminate and not kernel32.TerminateJobObject(wintypes.HANDLE(handle), 1):
        return False
    while True:
        accounting = BASIC_ACCOUNTING_INFORMATION()
        if not kernel32.QueryInformationJobObject(
            wintypes.HANDLE(handle),
            1,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            return False
        if accounting.ActiveProcesses == 0:
            kernel32.CloseHandle(wintypes.HANDLE(handle))
            with _WINDOWS_JOB_GUARD:
                _WINDOWS_JOB_HANDLES.pop(pid, None)
            return True
        if not terminate:
            terminate = True
            if not kernel32.TerminateJobObject(wintypes.HANDLE(handle), 1):
                return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _start_token(pid: int) -> str | None:
    if os.name == "nt":
        return _windows_start_token(pid)
    return _posix_start_token(pid)


def _windows_start_token(pid: int, kernel32: Any | None = None) -> str | None:
    try:
        import ctypes
        from ctypes import wintypes

        selected = getattr(ctypes, "windll").kernel32 if kernel32 is None else kernel32
        open_process = selected.OpenProcess
        if kernel32 is None:
            open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            open_process.restype = wintypes.HANDLE
        handle = open_process(0x0400, False, pid)
        if not handle:
            error = int(selected.GetLastError())
            if error in (87, 1168):
                return None
            raise ProcessIdentityUnavailable(
                f"Cannot check process identity for PID {pid}; Windows error {error}"
            )
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        exit_code = wintypes.DWORD()
        try:
            if not selected.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                error = int(selected.GetLastError())
                raise ProcessIdentityUnavailable(
                    f"Cannot check process liveness for PID {pid}; Windows error {error}"
                )
            if exit_code.value != 259:
                return None
            if not selected.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                error = int(selected.GetLastError())
                raise ProcessIdentityUnavailable(
                    f"Cannot read process birth time for PID {pid}; Windows error {error}"
                )
            return f"win:{creation.dwHighDateTime:08x}{creation.dwLowDateTime:08x}"
        finally:
            selected.CloseHandle(handle)
    except ProcessIdentityUnavailable:
        raise
    except (AttributeError, OSError) as exc:
        raise ProcessIdentityUnavailable(
            f"Cannot check Windows process identity for PID {pid}"
        ) from exc


def _posix_start_token(pid: int) -> str | None:
    try:
        process = psutil.Process(pid)
        return f"psutil:{process.create_time():.9f}"
    except psutil.AccessDenied as exc:
        raise ProcessIdentityUnavailable(
            f"Access denied while checking process identity: {pid}"
        ) from exc
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None


@dataclass(frozen=True, slots=True)
class ProcessMarker:
    schema_version: int
    marker_id: str
    owner_pid: int
    owner_start_token: str
    pid: int
    start_token: str
    argv_sha256: str
    executable: str
    created_at: str


class ProcessMarkerStore:
    def __init__(self, root: Path = DEFAULT_MARKER_ROOT) -> None:
        self.root = root
        self._guard = threading.Lock()

    def create(self, process: subprocess.Popen[Any], argv: tuple[str, ...]) -> Path | None:
        owner_pid = os.getpid()
        owner_token = _start_token(owner_pid)
        token = _start_token(process.pid)
        if owner_token is None or token is None:
            raise ProcessIdentityUnavailable(
                "Owned subprocess identities disappeared before marker creation."
            )
        marker_id = uuid.uuid4().hex
        marker = ProcessMarker(
            2,
            marker_id,
            owner_pid,
            owner_token,
            process.pid,
            token,
            hashlib.sha256(json.dumps(argv).encode("utf-8")).hexdigest(),
            argv[0],
            datetime.now(timezone.utc).isoformat(),
        )
        with self._guard:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / f"{process.pid}-{marker_id}.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(asdict(marker), sort_keys=True), encoding="utf-8")
            os.replace(temporary, path)
            return path

    @staticmethod
    def remove(path: Path | None) -> None:
        if path is not None:
            path.unlink(missing_ok=True)


def identity_matches(pid: int, expected_start_token: str) -> bool:
    return _start_token(pid) == expected_start_token


def _terminate_suspended_windows_leader(process: subprocess.Popen[Any]) -> bool:
    """Terminate and reap a pre-resume leader that cannot have descendants."""

    try:
        process.terminate()
    except ProcessLookupError:
        return process.poll() is not None
    except OSError:
        return False
    try:
        process.wait(timeout=MAX_OWNED_PROCESS_CLEANUP_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return process.returncode is not None


class _OwnedPosixPopen(subprocess.Popen[Any]):
    """Plain group ownership: wait/communicate observe; confirmed cleanup reaps.

    A successful new-session spawn and WNOWAIT retain the exact direct child,
    including after normal caller completion. This pins its group number until
    every current member is proven terminal. No detached-child policy is added.
    """

    _waitpid_lock: LockType

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("start_new_session") is not True:
            raise ValueError("Owned POSIX processes require start_new_session=True")
        self._leader_reaped = False
        self._cleanup_confirmed = False
        self._wait_error: str | None = None
        super().__init__(*args, **kwargs)
        try:
            self._leader_start_token = _posix_start_token(self.pid)
        except ProcessIdentityUnavailable:
            self._leader_start_token = None
        # Keep an unconfirmed ownership anchor available even if a caller drops
        # its reference. Failed cleanup retains both this object and its marker.
        with _POSIX_RETAINED_GUARD:
            _POSIX_RETAINED[self.pid] = self

    def _internal_poll(self, _deadstate: Any = None, **kwargs: Any) -> int | None:
        if self.returncode is not None or not hasattr(self, "pid"):
            return self.returncode
        if not self._waitpid_lock.acquire(False):
            return None
        try:
            from pyocd_debug_mcp.kernel._analysis_posix import _exit_status

            self.returncode = _exit_status(self.pid)
        except InterruptedError:
            pass
        except (OSError, RuntimeError) as exc:
            self._wait_error = str(exc)
        finally:
            self._waitpid_lock.release()
        return self.returncode

    def _wait(self, timeout: float | None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            result = self._internal_poll()
            if result is not None:
                return result
            if self._wait_error is not None:
                raise RuntimeError("Owned child exit observation unavailable: " + self._wait_error)
            if timeout is not None and deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(
                0.005 if deadline is None else min(0.005, max(0, deadline - time.monotonic()))
            )


def terminate_process_group(
    process: subprocess.Popen[Any],
    *,
    grace_seconds: float = DEFAULT_PROCESS_GROUP_CLEANUP_GRACE_SECONDS,
) -> bool:
    if os.name == "nt":
        with _WINDOWS_JOB_GUARD:
            handle = _WINDOWS_JOB_HANDLES.get(process.pid)
        if handle is None:
            return process.poll() is not None
        deadline = time.monotonic() + grace_seconds
        if not _close_windows_job(
            process.pid,
            handle,
            terminate=process.poll() is None,
            deadline=deadline,
        ):
            return False
        # Job active-zero proves that no owned process remains live. Reaping the
        # leader is separate Python/OS bookkeeping and gets its own bounded
        # phase; coupling it to the already-consumed Job deadline creates false
        # unconfirmed-cleanup results under scheduler contention.
        try:
            process.wait(timeout=grace_seconds)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return process.returncode is not None

    from pyocd_debug_mcp.kernel._analysis_posix import AnalysisPopen

    if isinstance(process, AnalysisPopen):
        return process.cleanup(grace_seconds)

    return _terminate_retained_posix_group(process, grace_seconds)


def _terminate_retained_posix_group(process: subprocess.Popen[Any], grace_seconds: float) -> bool:
    """Signal only a PGID pinned by the exact unreaped leader; prove, then reap.

    While the direct child is unreaped its PID, and so its group and session
    number, cannot be reused, and every group member shares that session.
    Signal success or denial, a zombie leader, leader exit or a numeric PGID
    alone never confirms cleanup: success needs the retained exit, a stable
    complete group whose members are all terminal, then the actual leader reap.
    An unproven group keeps its anchor and reports unconfirmed cleanup.
    """
    if os.name == "nt":
        # Windows cleanup is owned by terminate_process_group's Job branch.
        return False
    from pyocd_debug_mcp.kernel import _analysis_posix

    process_group = process.pid
    if not isinstance(process, _OwnedPosixPopen):
        # An arbitrary/already-reaped Popen carries no retained new-session
        # proof. Only a reaped leader AND an absent group can confirm it.
        return process.returncode is not None and _released_group_absent(
            process_group, grace_seconds
        )
    if process._cleanup_confirmed:
        return True  # Recorded terminal membership and actual reap; no new signal.
    with _POSIX_RETAINED_GUARD:
        if _POSIX_RETAINED.get(process_group) is not process:
            return False
    total_deadline = time.monotonic() + 3 * grace_seconds
    # Hold CPython's reaping lock across native ownership, signals, proof and
    # reap. No concurrent Popen wait/poll can release this number in between.
    if not process._waitpid_lock.acquire(timeout=max(0.0, grace_seconds)):
        return False

    def leader_exit() -> int | None:
        # The lock protects cooperating Popen reapers; fresh birth identity
        # additionally rejects a stale object or externally released/reused PID.
        token = process._leader_start_token
        if token is None or _posix_start_token(process_group) != token:
            raise ProcessIdentityUnavailable("retained leader birth cannot be confirmed")
        return _analysis_posix._exit_status(process_group)

    try:
        if process._cleanup_confirmed:
            return True
        if process._leader_reaped:
            return False
        for selected in (signal.SIGTERM, signal.SIGKILL, None):
            deadline = min(total_deadline, time.monotonic() + grace_seconds)
            signalled = False
            while time.monotonic() < deadline:
                # WNOWAIT verifies the retained direct-child relationship on
                # every iteration; cached returncode alone is never authority.
                status = leader_exit()
                state = _retained_group_state(process_group, status, deadline)
                if state == "terminal" and status is not None:
                    # Do not call wait(): this Popen's public waits deliberately
                    # observe without reaping. Final waitpid must actually reap.
                    if time.monotonic() >= deadline:
                        break
                    if leader_exit() != status:
                        return False
                    pid, native_status = os.waitpid(process_group, os.WNOHANG)
                    if pid != process_group:
                        return False
                    process._leader_reaped = True
                    process.returncode = os.waitstatus_to_exitcode(native_status)
                    process._cleanup_confirmed = process.returncode == status
                    with _POSIX_RETAINED_GUARD:
                        _POSIX_RETAINED.pop(process_group, None)
                    return process._cleanup_confirmed
                if state == "unknown":
                    return False  # Incomplete/faulted observation grants no signal.
                if state == "expired":
                    break  # The next bounded phase must acquire its own fresh proof.
                if state == "changed":
                    continue  # A newly observed exit needs a fresh stable proof.
                if selected is not None and not signalled:
                    leader_exit()
                    if time.monotonic() >= deadline:
                        break
                    try:
                        os.killpg(process_group, selected)
                    except (ProcessLookupError, PermissionError):
                        # Neither signal acceptance nor denial proves cleanup.
                        # A fresh complete terminal proof and reap are required.
                        pass
                    signalled = True
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(0.02, remaining))
        return False
    except (psutil.Error, OSError, RuntimeError):
        return False
    finally:
        process._waitpid_lock.release()


def _released_group_absent(process_group: int, budget_seconds: float) -> bool:
    """Confirm only proven absence once no unreaped leader pins this number.

    A reaped leader no longer reserves its PID, so a non-empty group with this
    number may belong to a later session. It never receives a signal; only
    ESRCH, proving that no process at all still has this PGID, confirms it.
    """
    if os.name == "nt":
        return False  # Windows has no POSIX group; absence is never proven here.
    deadline = time.monotonic() + budget_seconds
    while True:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False  # Present or unprobeable is never proven absence.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.02, remaining))


def _current_group_members(process_group: int, deadline: float) -> set[int]:
    """Complete current PGID membership including zombies; errors never mean empty."""
    if os.name == "nt":
        raise RuntimeError("POSIX process-group membership is unavailable on Windows")
    from pyocd_debug_mcp.kernel import _analysis_posix

    if sys.platform == "darwin":
        return _analysis_posix._darwin_group_members(process_group, deadline)
    members: set[int] = set()
    for pid in psutil.pids():
        if time.monotonic() >= deadline:
            raise _analysis_posix._ObservationDeadline("group snapshot exceeded bounded phase")
        try:
            if os.getpgid(pid) == process_group:
                if os.getsid(pid) != process_group:
                    raise RuntimeError("group member is outside the owned session")
                members.add(pid)
        except ProcessLookupError:
            continue  # Released before its group could be read.
    if process_group not in members:
        raise RuntimeError("group snapshot lacks retained leader")
    return members


def _retained_group_state(process_group: int, leader_exit: int | None, deadline: float) -> str:
    """Distinguish a completely observed owned live/terminal group from unknown.

    Two equal complete snapshots bracket fresh same-birth zombie observations
    of every non-leader member, and the leader's retained exit is unchanged. A
    live member is never terminal proof. Unknown/reused/newly listed identities
    cannot authorize a signal or confirm cleanup.
    """
    if os.name == "nt":
        return "unknown"
    from pyocd_debug_mcp.kernel import _analysis_posix

    try:
        members = _current_group_members(process_group, deadline)
        if process_group not in members:
            return "unknown"
        births: dict[int, str | None] = {}
        while time.monotonic() < deadline:
            states: dict[int, str] = {}
            for pid in sorted(members - {process_group}):
                if pid not in births:
                    try:
                        births[pid] = f"psutil:{psutil.Process(pid).create_time():.9f}"
                    except psutil.ZombieProcess:
                        return "unknown"  # Unreadable birth is never exit proof.
                    except psutil.NoSuchProcess:
                        births[pid] = None  # Released; it must leave the next snapshot.
                        continue
                birth = births[pid]
                if birth is not None:
                    state = _analysis_posix._process_state(pid, birth)
                    if state not in {"gone", "zombie", "live"}:
                        return "unknown"
                    if state == "live" and (
                        os.getpgid(pid) != process_group or os.getsid(pid) != process_group
                    ):
                        return "unknown"
                    states[pid] = state
            # Final membership must FOLLOW the liveness reads. Otherwise a live
            # branch can fork after this snapshot, then look terminal while its
            # new leaf is absent from the purported complete terminal proof.
            following = _current_group_members(process_group, deadline)
            if process_group not in following or not following <= members:
                return "unknown"
            live = leader_exit is None
            for pid in sorted(following - {process_group}):
                birth = births[pid]
                if birth is None:
                    return "unknown"
                state = states[pid]
                if state not in {"live", "zombie"}:
                    return "unknown"
                live = live or state == "live"
            if following == members:
                if _analysis_posix._exit_status(process_group) != leader_exit:
                    return "changed"
                return "live" if live else "terminal"
            # A reaper released a terminal orphan; require a fresh stable pair.
            members = following
        return "expired"
    except _analysis_posix._ObservationDeadline:
        return "expired"
    except (psutil.Error, OSError, RuntimeError):
        return "unknown"


def terminate_marked_group(pid: int, start_token: str) -> bool:
    try:
        current_token = _start_token(pid)
    except ProcessIdentityUnavailable:
        return False
    if current_token is not None and current_token != start_token:
        return False
    if current_token is None:
        # Windows Job Objects kill descendants when the owning helper closes.
        if os.name == "nt":
            return True
        # An empty POSIX group leaves nothing to recover. A surviving
        # leaderless group has no birth identity: the original group may have
        # ended and its numeric PGID been reused before this startup.
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        except (AttributeError, OSError):
            return False
        return False
    try:
        if os.name == "nt":
            if current_token is None:
                return True
            system_root = os.environ.get("SystemRoot")
            if not system_root:
                return False
            completed = subprocess.run(
                [
                    str(Path(system_root) / "System32" / "taskkill.exe"),
                    "/PID",
                    str(pid),
                    "/T",
                    "/F",
                ],
                check=False,
                capture_output=True,
                timeout=1.0,
            )
            if completed.returncode != 0:
                return False
        else:
            if (
                not identity_matches(pid, start_token)
                or os.getpgid(pid) != pid
                or os.getsid(pid) != pid
            ):
                return False
            os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if os.name == "nt" and not identity_matches(pid, start_token):
                return True
            if os.name != "nt":
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    return True
            time.sleep(0.02)
        if os.name == "nt" and not identity_matches(pid, start_token):
            return True
        if os.name != "nt":
            # Recovery has no retained Popen anchor. Recheck exact leader birth
            # and session/group before escalating; a reaped/recycled group
            # number cannot authorize SIGKILL of an unrelated process.
            if (
                not identity_matches(pid, start_token)
                or os.getpgid(pid) != pid
                or os.getsid(pid) != pid
            ):
                return False
            os.killpg(pid, signal.SIGKILL)
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    return True
                time.sleep(0.02)
        return False
    except ProcessLookupError:
        return False
    except (AttributeError, OSError, ProcessIdentityUnavailable):
        return False


def popen_owned(
    argv: Sequence[str],
    *,
    marker_store: ProcessMarkerStore | None = None,
    analysis_ownership: bool = False,
    **kwargs: Any,
) -> tuple[subprocess.Popen[Any], Path | None]:
    validated = validate_argv(argv)
    if kwargs.get("shell"):
        raise ValueError("shell execution is forbidden; pass explicit argv")
    kwargs.update(process_group_options())
    begin_analysis_ownership: Callable[[], None] | None = None
    if analysis_ownership and os.name != "nt":
        from pyocd_debug_mcp.kernel._analysis_posix import AnalysisPopen

        process = AnalysisPopen(validated, **kwargs)
        begin_analysis_ownership = process.begin_ownership
    else:
        process = (
            subprocess.Popen(validated, **kwargs)
            if os.name == "nt"
            else _OwnedPosixPopen(validated, **kwargs)
        )
    store = marker_store or ProcessMarkerStore()
    job_handle: int | None = None
    marker: Path | None = None
    analysis_setup_cleaned: bool | None = None
    try:
        if begin_analysis_ownership is not None:
            begin_analysis_ownership()
        # Establish recoverable identity before Windows Job assignment. The
        # leader is still suspended here and therefore cannot have descendants.
        try:
            marker = store.create(process, validated)
        except ProcessIdentityUnavailable:
            if begin_analysis_ownership is not None and process.poll() is not None:
                from pyocd_debug_mcp.kernel._analysis_posix import analysis_cleanup_scope_confirmed

                # No invented recovery token: only an already-terminal analysis
                # child can avoid a marker, after full scoped retirement/reap.
                analysis_setup_cleaned = False
                analysis_setup_cleaned = terminate_process_group(process)
                if analysis_setup_cleaned and analysis_cleanup_scope_confirmed(
                    getattr(process, "cleanup_receipt", None)
                ):
                    return process, None
            raise
        if os.name == "nt":
            job_handle = _create_windows_kill_job(process)
            with _WINDOWS_JOB_GUARD:
                _WINDOWS_JOB_HANDLES[process.pid] = job_handle
            _resume_windows_process(process)
    except BaseException as primary:
        if job_handle is not None:
            with _WINDOWS_JOB_GUARD:
                _WINDOWS_JOB_HANDLES.setdefault(process.pid, job_handle)
        cleaned = (
            analysis_setup_cleaned
            if analysis_setup_cleaned is not None
            else (
                _terminate_suspended_windows_leader(process)
                if os.name == "nt" and job_handle is None
                else terminate_process_group(process)
            )
        )
        if begin_analysis_ownership is not None:
            setattr(
                primary, "cleanup_receipt", copy.deepcopy(getattr(process, "cleanup_receipt", None))
            )
        if cleaned:
            try:
                store.remove(marker)
            except BaseException as cleanup:
                raise primary from cleanup
            raise
        recovery_error: BaseException | None = None
        if marker is None:
            try:
                marker = store.create(process, validated)
            except BaseException as exc:
                recovery_error = exc
        if marker is not None:
            cleanup = RuntimeError(
                "Suspended subprocess cleanup could not be confirmed; recovery marker retained "
                f"at {marker}."
            )
        else:
            cleanup = RuntimeError(
                "Suspended subprocess cleanup could not be confirmed and recovery marker "
                "creation also failed: "
                f"{type(recovery_error).__name__}: {recovery_error}"
            )
        raise primary from cleanup
    return process, marker


def run_owned(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    capture_output: bool = False,
    text: bool = False,
    check: bool = False,
    timeout: float,
    marker_store: ProcessMarkerStore | None = None,
    stdin: int | IO[Any] | None = None,
) -> subprocess.CompletedProcess[Any]:
    if timeout <= 0 or not math.isfinite(timeout):
        raise ValueError("subprocess timeout must be positive and finite")
    stdout = subprocess.PIPE if capture_output else None
    stderr = subprocess.PIPE if capture_output else None
    process, marker = popen_owned(
        argv,
        marker_store=marker_store,
        cwd=cwd,
        env=env,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
    )
    remove_marker = False
    try:
        try:
            # Plain POSIX Popen ownership retains the leader through this wait;
            # descendant cleanup below proves terminal membership, then reaps.
            output, errors = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            remove_marker = terminate_process_group(process)
            if not remove_marker:
                raise RuntimeError(
                    "Owned subprocess cleanup could not be confirmed; recovery marker retained."
                ) from exc
            output, errors = process.communicate()
            exc.stdout = output
            exc.stderr = errors
            raise
        except BaseException:
            # Cancellation must not outlive the ownership marker.  This includes
            # KeyboardInterrupt and SystemExit, which deliberately bypass Exception.
            remove_marker = terminate_process_group(process)
            raise
        remove_marker = terminate_process_group(process)
        if not remove_marker:
            raise RuntimeError(
                "Owned subprocess descendants could not be cleared after leader exit; "
                "recovery marker retained."
            )
        result = subprocess.CompletedProcess(tuple(argv), process.returncode, output, errors)
        if check and result.returncode:
            raise subprocess.CalledProcessError(
                result.returncode, result.args, output=result.stdout, stderr=result.stderr
            )
        return result
    finally:
        if remove_marker:
            (marker_store or ProcessMarkerStore()).remove(marker)
