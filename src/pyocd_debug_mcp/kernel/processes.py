"""Owned subprocess groups with strict argv and recoverable marker records."""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess
import threading
import time
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO, Sequence

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
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | suspended}
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

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    information = EXTENDED_LIMIT_INFORMATION()
    information.BasicLimitInformation.LimitFlags = 0x00002000
    process_handle = wintypes.HANDLE(int(getattr(process, "_handle")))
    if not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(information), ctypes.sizeof(information)
    ) or not kernel32.AssignProcessToJobObject(job, process_handle):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "Unable to assign owned subprocess to a kill-on-close job")
    return int(job)


def _resume_windows_process(process: subprocess.Popen[Any]) -> None:
    import ctypes
    from ctypes import wintypes

    resume = ctypes.windll.ntdll.NtResumeProcess
    resume.argtypes = (wintypes.HANDLE,)
    resume.restype = ctypes.c_long
    status = resume(wintypes.HANDLE(int(getattr(process, "_handle"))))
    if status != 0:
        raise OSError(status, "NtResumeProcess failed for owned subprocess")


def _close_windows_job(pid: int, handle: int, *, terminate: bool, timeout: float) -> bool:
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

    kernel32 = ctypes.windll.kernel32
    if terminate and not kernel32.TerminateJobObject(wintypes.HANDLE(handle), 1):
        return False
    deadline = time.monotonic() + timeout
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

        selected = ctypes.windll.kernel32 if kernel32 is None else kernel32
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


def terminate_process_group(process: subprocess.Popen[Any], *, grace_seconds: float = 0.5) -> bool:
    if os.name == "nt":
        with _WINDOWS_JOB_GUARD:
            handle = _WINDOWS_JOB_HANDLES.get(process.pid)
        if handle is None:
            return process.poll() is not None
        return _close_windows_job(
            process.pid, handle, terminate=process.poll() is None, timeout=grace_seconds
        )

    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return True
    try:
        process.wait(timeout=grace_seconds)
    except (OSError, subprocess.TimeoutExpired):
        pass

    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False

    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return True
    try:
        process.wait(timeout=grace_seconds)
    except (OSError, subprocess.TimeoutExpired):
        return False

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.02)
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def terminate_marked_group(pid: int, start_token: str) -> bool:
    try:
        current_token = _start_token(pid)
    except ProcessIdentityUnavailable:
        return False
    if current_token is not None and current_token != start_token:
        return False
    if current_token is None:
        # Windows Job Objects kill descendants when the owning helper closes.
        # A leaderless POSIX PGID has no remaining identity authority and may
        # have been reused, so startup must retain the marker rather than signal.
        return os.name == "nt"
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
    except (AttributeError, OSError):
        return False


def popen_owned(
    argv: Sequence[str],
    *,
    marker_store: ProcessMarkerStore | None = None,
    **kwargs: Any,
) -> tuple[subprocess.Popen[Any], Path | None]:
    validated = validate_argv(argv)
    if kwargs.get("shell"):
        raise ValueError("shell execution is forbidden; pass explicit argv")
    kwargs.update(process_group_options())
    process = subprocess.Popen(validated, **kwargs)
    store = marker_store or ProcessMarkerStore()
    job_handle: int | None = None
    marker: Path | None = None
    try:
        if os.name == "nt":
            job_handle = _create_windows_kill_job(process)
            with _WINDOWS_JOB_GUARD:
                _WINDOWS_JOB_HANDLES[process.pid] = job_handle
        marker = store.create(process, validated)
        if os.name == "nt":
            _resume_windows_process(process)
    except BaseException as exc:
        if job_handle is not None:
            cleaned = _close_windows_job(process.pid, job_handle, terminate=True, timeout=0.5)
        else:
            cleaned = terminate_process_group(process)
        if cleaned:
            store.remove(marker)
        else:
            raise RuntimeError(
                "Owned subprocess initialization failed and cleanup could not be confirmed."
            ) from exc
        raise
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
