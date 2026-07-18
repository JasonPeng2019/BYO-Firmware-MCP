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
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO, Sequence

DEFAULT_MARKER_ROOT = Path(os.environ.get("PYOCD_MCP_RUNS_ROOT", "runs")) / "owned-processes"


def validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, str | bytes) or not argv:
        raise ValueError("argv must be a non-empty sequence of explicit arguments")
    values = tuple(argv)
    if any(not isinstance(value, str) or not value or "\x00" in value for value in values):
        raise ValueError("every argv item must be a non-empty NUL-free string")
    return values


def process_group_options(platform: str | None = None) -> dict[str, object]:
    selected = platform or os.name
    if selected == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _start_token(pid: int) -> str | None:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            open_process = ctypes.windll.kernel32.OpenProcess
            open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            open_process.restype = wintypes.HANDLE
            handle = open_process(0x0400, False, pid)
            if not handle:
                return None
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            try:
                if not ctypes.windll.kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
                return f"win:{creation.dwHighDateTime:08x}{creation.dwLowDateTime:08x}"
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except (AttributeError, OSError):
            return None
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        fields = stat_path.read_text(encoding="utf-8").split()
        return f"proc:{fields[21]}"
    except (OSError, IndexError):
        return None


@dataclass(frozen=True, slots=True)
class ProcessMarker:
    schema_version: int
    marker_id: str
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
        token = _start_token(process.pid)
        if token is None:
            return None
        marker_id = uuid.uuid4().hex
        marker = ProcessMarker(
            1,
            marker_id,
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


def terminate_process_group(
    process: subprocess.Popen[Any], *, grace_seconds: float = 0.5
) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=grace_seconds)
    except (OSError, subprocess.TimeoutExpired):
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass


def terminate_marked_group(pid: int, start_token: str) -> bool:
    if not identity_matches(pid, start_token):
        return False
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            open_process = ctypes.windll.kernel32.OpenProcess
            open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            open_process.restype = wintypes.HANDLE
            handle = open_process(0x0001 | 0x00100000, False, pid)
            if not handle:
                return False
            try:
                if not identity_matches(pid, start_token):
                    return False
                if not ctypes.windll.kernel32.TerminateProcess(handle, 1):
                    return False
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        else:
            os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if not identity_matches(pid, start_token):
                return True
            time.sleep(0.02)
        if not identity_matches(pid, start_token):
            return True
        if os.name != "nt":
            os.killpg(pid, signal.SIGKILL)
        return True
    except (AttributeError, OSError, ProcessLookupError):
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
    marker = (marker_store or ProcessMarkerStore()).create(process, validated)
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
    stdout: int | IO[Any] | None = None,
    stderr: int | IO[Any] | None = None,
) -> subprocess.CompletedProcess[Any]:
    if timeout <= 0 or not math.isfinite(timeout):
        raise ValueError("subprocess timeout must be positive and finite")
    if capture_output and (stdout is not None or stderr is not None):
        raise ValueError("capture_output cannot be combined with explicit stdout or stderr")
    process_stdout = subprocess.PIPE if capture_output else stdout
    process_stderr = subprocess.PIPE if capture_output else stderr
    process, marker = popen_owned(
        argv,
        marker_store=marker_store,
        cwd=cwd,
        env=env,
        stdin=stdin,
        stdout=process_stdout,
        stderr=process_stderr,
        text=text,
    )
    try:
        try:
            output, errors = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            terminate_process_group(process)
            output, errors = process.communicate()
            exc.stdout = output
            exc.stderr = errors
            raise
        result = subprocess.CompletedProcess(tuple(argv), process.returncode, output, errors)
        if check and result.returncode:
            raise subprocess.CalledProcessError(
                result.returncode, result.args, output=result.stdout, stderr=result.stderr
            )
        return result
    finally:
        (marker_store or ProcessMarkerStore()).remove(marker)
