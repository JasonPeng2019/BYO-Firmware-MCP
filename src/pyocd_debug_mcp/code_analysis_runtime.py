"""Explicit launch context and native-program resolution boundary.

The private launch context supplies runtime authority. Managed failures never
substitute PATH; development fallback retains the runtime failure evidence.
"""

from __future__ import annotations

import copy
import shutil
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol


from pyocd_debug_mcp.code_analysis_config import AnalysisProblem
from pyocd_debug_mcp.kernel._analysis_posix import analysis_cleanup_scope_confirmed
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
    runtime_root: Path | None = None
    runtime_manifest_sha256: str | None = None
    analysis_mapping_sha256: str | None = None
    executable_sha256: str | None = None
    expected_identity: dict[str, Any] = field(default_factory=dict)
    observed_identity: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    runtime_problems: tuple[AnalysisProblem, ...] = ()
    receipt: Any = field(default=None, repr=False, compare=False)
    cleanup_receipt: dict[str, Any] | None = field(default=None, repr=False, compare=False)


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


def analysis_child_environment(program: ResolvedAnalysisProgram) -> dict[str, str] | None:
    """Managed loaders receive no inherited injection/search overrides.

    Explicit project context and query-driver arguments keep their existing
    meaning. Development PATH programs retain their inherited environment.
    """
    if program.origin != "managed_runtime":
        return None
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("LD_", "DYLD_"))
    }


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
                cleaned = terminate_process_group(process)
                if not cleaned or (
                    os.name != "nt"
                    and not analysis_cleanup_scope_confirmed(
                        getattr(process, "cleanup_receipt", None)
                    )
                ):
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
            result = popen_owned(argv, marker_store=marker_store, analysis_ownership=True, **kwargs)
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
    operation = current_operation()
    cancellation = operation.cancellation_requested if operation is not None else None
    remaining(deadline, cancellation)
    from pyocd_debug_mcp.code_analysis_manifest import payload_receipt, hash_file

    runtime_failure = None
    if context.runtime_root is not None:
        candidate = ResolvedAnalysisProgram(
            name, origin="managed_runtime", runtime_root=context.runtime_root
        )
        try:
            receipt = payload_receipt(context.runtime_root, name, deadline, cancellation)
            candidate = _receipt_program(receipt)
            result = _probe_program(
                candidate, deadline, cancellation, receipt.metadata.programs[name]["version"]
            )
            # Receipt metadata must be checked before success. A probe failure
            # established while the caller lived retains its classification
            # even when the separately bounded cleanup grace passes the deadline.
            if not result.problems:
                validate_analysis_program(result, deadline, cancellation)
            if not result.problems or context.managed:
                return result
            runtime_failure = result
        except TimeoutError:
            raise
        except (OSError, ValueError) as exc:
            _invalidate_probes(context.runtime_root, deadline, cancellation)
            failed_receipt = getattr(exc, "receipt", None)
            metadata = getattr(exc, "metadata", None)
            if failed_receipt is not None:
                candidate = _receipt_program(failed_receipt)
            elif metadata is not None:
                relative = metadata.programs[name]["executable"]
                candidate = replace(
                    candidate,
                    executable=context.runtime_root / relative,
                    runtime_manifest_sha256=metadata.hashes["release-manifest.json"],
                    analysis_mapping_sha256=metadata.hashes["analysis/runtime.json"],
                    executable_sha256=metadata.inventory.get(relative, {}).get("sha256"),
                    data_paths=tuple(
                        context.runtime_root / metadata.programs[name][k]
                        for k in (
                            ("resource_dir",) if name == "clangd" else ("cfg_dir", "platforms_dir")
                        )
                    ),
                    expected_identity={"metadata_sha256": dict(metadata.hashes)},
                )
            runtime_failure = _runtime_failure(candidate, exc)
        if context.managed:
            assert runtime_failure is not None
            return runtime_failure
    raw = _which_path_only(context, name)
    if raw is None:
        return runtime_failure or ResolvedAnalysisProgram(
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
        result = ResolvedAnalysisProgram(
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
        return _development_fallback(result, runtime_failure)
    try:
        digest, _, observed = hash_file(executable, deadline, cancellation)
        candidate = ResolvedAnalysisProgram(
            name,
            executable,
            origin="path",
            identity=(str(executable), *observed, digest),
            executable_sha256=digest,
        )
        result = _probe_program(candidate, deadline, cancellation)
    except TimeoutError:
        raise
    except (OSError, ValueError) as exc:
        result = _runtime_failure(ResolvedAnalysisProgram(name, executable, origin="path"), exc)
    if not result.problems:
        remaining(deadline, cancellation)
    return _development_fallback(result, runtime_failure)


def _development_fallback(
    result: ResolvedAnalysisProgram, failure: ResolvedAnalysisProgram | None
) -> ResolvedAnalysisProgram:
    if failure is None:
        return result
    return replace(
        result,
        runtime_root=failure.runtime_root,
        runtime_manifest_sha256=failure.runtime_manifest_sha256,
        analysis_mapping_sha256=failure.analysis_mapping_sha256,
        runtime_problems=failure.problems,
        expected_identity=failure.expected_identity,
        observed_identity=failure.observed_identity,
        warnings=(
            "Supplied runtime is unusable; source/development resolution selected PATH fallback.",
        ),
    )


def _receipt_program(receipt: Any) -> ResolvedAnalysisProgram:
    metadata = receipt.metadata
    relative = metadata.programs[receipt.name]["executable"]
    return ResolvedAnalysisProgram(
        receipt.name,
        receipt.executable,
        origin="managed_runtime",
        identity=receipt.identity,
        data_paths=receipt.data_paths,
        runtime_root=metadata.root,
        runtime_manifest_sha256=metadata.hashes["release-manifest.json"],
        analysis_mapping_sha256=metadata.hashes["analysis/runtime.json"],
        executable_sha256=metadata.inventory[relative]["sha256"],
        expected_identity={
            "metadata_sha256": dict(metadata.hashes),
            "executable": metadata.inventory[relative],
        },
        observed_identity={"files": {str(p): i for p, i in receipt.identities.items()}},
        receipt=receipt,
    )


def _runtime_failure(program: ResolvedAnalysisProgram, exc: Exception) -> ResolvedAnalysisProgram:
    return replace(
        program,
        identity=(),
        receipt=None,
        expected_identity={**program.expected_identity, **getattr(exc, "expected", {})},
        observed_identity={**program.observed_identity, **getattr(exc, "observed", {})},
        problems=(
            AnalysisProblem(
                "analysis/executable-unavailable",
                f"{program.name} runtime unusable: {exc}",
                "Restore/reinstall the supported runtime and required data, then retry.",
                getattr(exc, "path", None),
                program.name,
            ),
        ),
    )


def _invalidate_probes(root: Path, deadline: float, cancellation: Cancellation | None) -> None:
    while not _probe_lock.acquire(timeout=min(0.02, remaining(deadline, cancellation))):
        pass
    try:
        for identity in list(_probe_cache):
            if identity and identity[0] == str(root):
                del _probe_cache[identity]
    finally:
        _probe_lock.release()


def validate_analysis_program(
    program: ResolvedAnalysisProgram,
    deadline: float,
    cancellation: Cancellation | None = None,
) -> None:
    """Check the receipt again before returning success, without rehash/retry."""
    remaining(deadline, cancellation)
    if program.receipt is not None:
        from pyocd_debug_mcp.code_analysis_manifest import validate_receipt

        try:
            validate_receipt(program.receipt, deadline, cancellation)
        except TimeoutError:
            raise
        except (OSError, ValueError):
            assert program.runtime_root is not None
            _invalidate_probes(program.runtime_root, deadline, cancellation)
            raise


def _probe_program(
    candidate: ResolvedAnalysisProgram,
    deadline: float,
    cancellation: Cancellation | None,
    expected_version: str | None = None,
) -> ResolvedAnalysisProgram:
    assert candidate.executable is not None
    while not _probe_lock.acquire(timeout=min(0.02, remaining(deadline, cancellation))):
        pass
    try:
        cached = _probe_cache.get(candidate.identity)
        if cached is not None:
            remaining(deadline, cancellation)
            if (
                os.name != "nt"
                and not cached.problems
                and not analysis_cleanup_scope_confirmed(cached.cleanup_receipt)
            ):
                return replace(
                    cached,
                    problems=(
                        AnalysisProblem(
                            "analysis/executable-unavailable",
                            "Cached version probe has no confirmed POSIX cleanup scope.",
                            "Recover owned analysis processes and restart the server before retrying.",
                        ),
                    ),
                )
            return cached
        probe_deadline = min(deadline, time.monotonic() + 5)
        store = ProcessMarkerStore()
        output = b""
        cleanup_receipt: dict[str, Any] | None = None
        try:
            # Admission/late-spawn expiry has no completed executable probe
            # evidence. Preserve its transient TimeoutError and owned cleanup.
            process, marker = spawn_before_deadline(
                [str(candidate.executable), "--version"],
                probe_deadline,
                cancellation,
                store,
                env=analysis_child_environment(candidate),
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
                version = output.decode("utf-8", errors="replace")
                if expected_version is not None:
                    match = re.search(
                        r"\b" + candidate.name + r"(?: version)?\s+(\d+\.\d+(?:\.\d+)?)(?![\d.])",
                        version,
                        re.IGNORECASE,
                    )
                    observed_version = match.group(1) if match else None
                    # Cppcheck omits the trailing zero in its native banner.
                    numeric = (
                        observed_version + ".0"
                        if observed_version and observed_version.count(".") == 1
                        else observed_version
                    )
                    if numeric != expected_version:
                        raise RuntimeError(
                            f"Version mismatch: expected {expected_version}, observed {observed_version!r}."
                        )
            except TimeoutError:
                # Establish the cause before cleanup can consume the caller's
                # remaining time. Only a started probe's own cap is executable
                # failure evidence; caller expiry/cancellation stays transient.
                remaining(deadline, cancellation)
                raise RuntimeError("Version probe exceeded its 5-second spawn/probe budget.")
            finally:
                try:
                    cleaned = terminate_process_group(process)
                finally:
                    cleanup_receipt = copy.deepcopy(getattr(process, "cleanup_receipt", None))
                scope_cleaned = cleaned and (
                    os.name == "nt" or analysis_cleanup_scope_confirmed(cleanup_receipt)
                )
                if scope_cleaned:
                    store.remove(marker)
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
                if not scope_cleaned:
                    raise RuntimeError("Version probe cleanup scope unconfirmed; marker retained.")
            result = replace(candidate, version=version, cleanup_receipt=cleanup_receipt)
        except TimeoutError as exc:
            setattr(
                exc,
                "cleanup_receipt",
                cleanup_receipt or copy.deepcopy(getattr(exc, "cleanup_receipt", None)),
            )
            raise
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            result = replace(
                candidate,
                cleanup_receipt=cleanup_receipt
                or copy.deepcopy(getattr(exc, "cleanup_receipt", None)),
                version=output.decode("utf-8", errors="replace") or None,
                problems=(
                    AnalysisProblem(
                        "analysis/executable-unavailable",
                        f"{candidate.name} version probe unusable: {exc}",
                        "Restore the executable/data; if cleanup failed, recover owned processes and restart the server before retrying.",
                    ),
                ),
                expected_identity={**candidate.expected_identity, "version": expected_version},
                observed_identity={
                    **candidate.observed_identity,
                    "version_stdout": output.decode("utf-8", errors="replace"),
                },
            )
        _probe_cache[candidate.identity] = result
        return result
    finally:
        _probe_lock.release()
