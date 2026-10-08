"""Explicit launch context and native-program resolution boundary.

Managed manifest mapping is intentionally unresolved until packaging accepts
versions, paths, digests and data. Never substitute PATH for a managed launch.
"""

from __future__ import annotations

import shutil
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


from pyocd_debug_mcp.code_analysis_config import AnalysisProblem, sha256_bytes
from pyocd_debug_mcp.kernel.processes import (
    ProcessMarkerStore,
    popen_owned,
    terminate_process_group,
)
from pyocd_debug_mcp.kernel.operations import current_operation


class Cancellation(Protocol):
    def is_set(self) -> bool: ...


@dataclass(frozen=True)
class AnalysisRuntimeContext:
    project_root: Path
    runtime_root: Path | None = None
    managed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_root", Path(self.project_root).resolve())
        if self.runtime_root is not None:
            object.__setattr__(self, "runtime_root", Path(self.runtime_root).resolve())
        if self.managed and self.runtime_root is None:
            raise ValueError("Managed analysis requires a validated runtime_root.")


@dataclass(frozen=True)
class ResolvedAnalysisProgram:
    name: str
    executable: Path | None = None
    version: str | None = None
    origin: str | None = None
    identity: tuple[Any, ...] = ()
    data_paths: tuple[Path, ...] = ()
    problems: tuple[AnalysisProblem, ...] = ()


_context: AnalysisRuntimeContext | None = None
_probe_cache: dict[tuple[Any, ...], ResolvedAnalysisProgram] = {}
_probe_lock = threading.Lock()
_spawn_admission = threading.Lock()
_spawn_cleanup_failure: str | None = None


def configure_analysis_runtime(context: AnalysisRuntimeContext) -> None:
    """Call before server import; handshake labels cannot redirect this context."""
    global _context
    _context = context


def analysis_runtime_context(project_root: Path) -> AnalysisRuntimeContext:
    return _context or AnalysisRuntimeContext(project_root)


def remaining(deadline: float, cancellation: Cancellation | None = None) -> float:
    if cancellation is not None and cancellation.is_set():
        raise TimeoutError("Analysis request cancelled.")
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("Analysis deadline expired.")
    return value


def _which_path_only(context: AnalysisRuntimeContext, name: str) -> str | None:
    # Windows shutil.which(basename) can prepend cwd even with an explicit PATH.
    # Search only deliberately listed directories, with complete candidate names
    # so Python 3.10 and newer agree about PATHEXT and never insert cwd.
    suffixes = [""]
    if os.name == "nt":
        suffixes += os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        directory = Path(entry.strip('"') if os.name == "nt" else entry)
        if not directory.is_absolute():
            directory = context.project_root / directory
        try:
            directory = directory.resolve()
        except (OSError, ValueError):
            continue
        for suffix in suffixes:
            candidate = shutil.which(str(directory / (name + suffix)), path="")
            if candidate is not None:
                return candidate
    return None


def spawn_before_deadline(
    argv: list[str],
    deadline: float,
    cancellation: Cancellation | None,
    marker_store: ProcessMarkerStore,
    **kwargs: Any,
) -> tuple[subprocess.Popen[Any], Path | None]:
    """Bound spawn too; a late child is still owned and cleaned by its spawn thread."""
    remaining(deadline, cancellation)
    while not _spawn_admission.acquire(timeout=min(0.02, remaining(deadline, cancellation))):
        pass
    if _spawn_cleanup_failure is not None:
        _spawn_admission.release()
        raise RuntimeError(_spawn_cleanup_failure)
    guard = threading.Lock()
    ready = threading.Event()
    outcome: list[Any] = []
    abandoned = False

    def clean_abandoned(result: Any) -> None:
        global _spawn_cleanup_failure
        try:
            if not isinstance(result, BaseException):
                process, marker = result
                if not terminate_process_group(process):
                    _spawn_cleanup_failure = (
                        "Late analysis child termination unconfirmed; recovery marker retained. "
                        "Restart the server after owned-process recovery before starting another child."
                    )
                    return
                marker_store.remove(marker)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
        except Exception as exc:
            _spawn_cleanup_failure = (
                f"Late analysis child cleanup failed; recovery marker retained: {exc}"
            )
        finally:
            _spawn_admission.release()

    def spawn() -> None:
        nonlocal abandoned
        try:
            result = popen_owned(argv, marker_store=marker_store, **kwargs)
        except BaseException as exc:
            result = exc
        with guard:
            late = abandoned
            if not late:
                outcome.append(result)
                ready.set()
        if late:
            clean_abandoned(result)

    try:
        threading.Thread(target=spawn, name="analysis-spawn", daemon=True).start()
    except BaseException:
        _spawn_admission.release()
        raise
    try:
        while not ready.wait(min(0.02, remaining(deadline, cancellation))):
            pass
        remaining(deadline, cancellation)
    except BaseException:
        with guard:
            abandoned = True
            completed = outcome[0] if outcome else None
        if completed is not None:
            clean_abandoned(completed)
        raise
    result = outcome[0]
    _spawn_admission.release()
    if isinstance(result, BaseException):
        raise result
    return result


def resolve_analysis_program(
    context: AnalysisRuntimeContext, name: str, deadline: float
) -> ResolvedAnalysisProgram:
    if name not in {"clangd", "cppcheck"}:
        raise ValueError("Unsupported analysis program.")
    if context.managed:
        return ResolvedAnalysisProgram(
            name,
            problems=(
                AnalysisProblem(
                    "analysis/executable-unavailable",
                    "Managed analysis manifest mapping is unresolved.",
                    "Restore a supported runtime with ROOT-accepted analysis executables/data manifest; packaging is still a prerequisite.",
                ),
            ),
        )
    raw = _which_path_only(context, name)
    if raw is None:
        return ResolvedAnalysisProgram(
            name,
            problems=(
                AnalysisProblem(
                    "analysis/executable-unavailable",
                    f"No {name} executable on development PATH.",
                    f"Install/configure {name} on PATH for source use, or restore the supported managed runtime.",
                ),
            ),
        )
    executable = Path(raw).resolve()
    if os.name == "nt" and executable.suffix.lower() in {".bat", ".cmd"}:
        return ResolvedAnalysisProgram(
            name,
            executable,
            origin="path",
            problems=(
                AnalysisProblem(
                    "analysis/executable-unavailable",
                    "Windows batch wrappers require shell interpretation and are unsupported.",
                    f"Put the native {name} executable on development PATH.",
                ),
            ),
        )
    identity: tuple[Any, ...] = ()
    operation = current_operation()
    cancellation = operation.cancellation_requested if operation is not None else None
    try:
        stat = executable.stat()
        identity = (
            str(executable),
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            sha256_bytes(executable.read_bytes()),
        )
        remaining(deadline, cancellation)
        while not _probe_lock.acquire(timeout=min(0.02, remaining(deadline, cancellation))):
            pass
        try:
            cached = _probe_cache.get(identity)
            if cached is not None:
                return cached
            probe_deadline = min(deadline, time.monotonic() + 5)
            store = ProcessMarkerStore()
            process, marker = spawn_before_deadline(
                [str(executable), "--version"],
                probe_deadline,
                cancellation,
                store,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                while True:
                    try:
                        output, errors = process.communicate(
                            timeout=min(0.02, remaining(probe_deadline, cancellation))
                        )
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if process.returncode != 0 or not output.strip():
                    raise RuntimeError(
                        f"Version probe failed ({process.returncode}): {errors.decode('utf-8', errors='replace')[-4096:]}"
                    )
                version = output.decode("utf-8", errors="replace").strip()
            except TimeoutError:
                # Establish the cause before cleanup can consume the caller's
                # remaining time. Only a started probe's own cap is executable
                # failure evidence; caller expiry/cancellation stays transient.
                remaining(deadline, cancellation)
                raise RuntimeError("Version probe exceeded its 5-second spawn/probe budget.")
            finally:
                cleaned = terminate_process_group(process)
                if cleaned:
                    store.remove(marker)
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
                if not cleaned:
                    raise RuntimeError("Version probe cleanup unconfirmed; marker retained.")
            result = ResolvedAnalysisProgram(name, executable, version, "path", identity)
            _probe_cache[identity] = result
            return result
        finally:
            _probe_lock.release()
    except TimeoutError:
        # Caller deadline/cancellation is not executable-identity evidence.
        # Let the service report the incomplete request; an independent call
        # gets its own finite probe without any hidden retry here.
        raise
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result = ResolvedAnalysisProgram(
            name,
            executable,
            origin="path",
            problems=(
                AnalysisProblem(
                    "analysis/executable-unavailable",
                    f"{name} version probe unusable: {exc}",
                    "Restore an executable and its required data, then retry.",
                ),
            ),
        )
        if identity:
            _probe_cache[identity] = result
        return result
