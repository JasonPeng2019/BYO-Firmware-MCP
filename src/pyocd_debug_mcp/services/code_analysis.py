"""Run-scoped saved-file analysis orchestration, independent of board authority."""

from __future__ import annotations

import math
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pyocd_debug_mcp.adapters.clangd import (
    ClangdAdapter,
    ClangdFailure,
    local_uri_path,
    position_to_lsp,
    range_from_lsp,
)
from pyocd_debug_mcp.code_analysis_config import (
    AnalysisConfigSnapshot,
    AnalysisProblem,
    HEADER_SUFFIXES,
    SOURCE_SUFFIXES,
    load_analysis_config,
    sha256_bytes,
    validate_clangd_configuration,
)
from pyocd_debug_mcp.code_analysis_runtime import (
    AnalysisRuntimeContext,
    ResolvedAnalysisProgram,
    remaining,
    resolve_analysis_program,
    validate_analysis_program,
)
from pyocd_debug_mcp.kernel.operations import DEFAULT_OPERATION_TIMEOUT_SECONDS, current_operation

INDEX_LIMITATIONS = [
    "One selected build configuration; no claim about other boards, targets or macro combinations.",
    "Global background-index completeness is unverified; empty references do not prove dead code.",
    "Saved files only. Closed headers and external dependencies may be stale in the background index.",
]
COVERAGE_DIAGNOSTIC_CODES = {
    "pp_file_not_found",
    "drv_unknown_argument",
    "drv_unsupported_opt",
    "drv_unsupported_option_argument",
    "invalid_ast",
    "fe_expected_compiler_job",
}


@dataclass
class _Document:
    version: int
    sha256: str
    text: str
    dependencies: tuple[Any, ...]


class _Cancellation:
    def __init__(self, operation: threading.Event | None, generation: threading.Event) -> None:
        self.operation = operation
        self.generation = generation

    def is_set(self) -> bool:
        return self.generation.is_set() or (self.operation is not None and self.operation.is_set())


def _acquire(lock: Any, deadline: float, cancellation: Any = None) -> None:
    while not lock.acquire(timeout=min(0.02, remaining(deadline, cancellation))):
        pass


class CodeAnalysisService:
    @staticmethod
    def _clangd_problems(snapshot: AnalysisConfigSnapshot) -> list[AnalysisProblem]:
        problems = list(snapshot.clangd_problems)
        # The native reader can lose relative-directory commands and use host
        # flags. Shared parsing still resolves these relative to the project.
        for index, entry in enumerate(snapshot.entries):
            if (
                not Path(entry.raw["directory"]).is_absolute()
                and not Path(entry.raw["file"]).is_absolute()
            ):
                problems.append(
                    AnalysisProblem(
                        "analysis/database-invalid",
                        "clangd cannot establish the selected build when both compilation directory and source paths are relative.",
                        "Re-export compile_commands.json with absolute directory/source fields; preserve the original arguments and target flags.",
                        f"compilation_database[{index}].directory",
                        "clangd",
                    )
                )
        # Valid shared JSON: the unsupported representation belongs solely to
        # clangd selection. Cppcheck has separate native-reader evidence.
        escapes = list(re.finditer(rb"\\(?:u[0-9a-fA-F]{4}|.)", snapshot.database_bytes or b""))
        if any(
            first.end() == second.start()
            and re.fullmatch(rb"\\u[dD][89aAbB][0-9a-fA-F]{2}", first.group())
            and re.fullmatch(rb"\\u[dD][c-fC-F][0-9a-fA-F]{2}", second.group())
            for first, second in zip(escapes, escapes[1:])
        ):
            problems.append(
                AnalysisProblem(
                    "analysis/database-invalid",
                    "clangd cannot safely interpret escaped non-BMP characters in this compilation database.",
                    "Re-export compile_commands.json as literal UTF-8 JSON (for Python, ensure_ascii=False); keep all original commands/flags.",
                    "compilation_database",
                    "clangd",
                )
            )
        return problems

    def __init__(
        self,
        context: AnalysisRuntimeContext,
        adapter_factory: Callable[..., ClangdAdapter] = ClangdAdapter,
        *,
        program_resolver: Callable[..., ResolvedAnalysisProgram] = resolve_analysis_program,
    ) -> None:
        self.context = context
        self.adapter_factory = adapter_factory
        self.program_resolver = program_resolver
        self._session_guard = threading.RLock()
        self._document_guard = threading.Lock()
        self._document_locks: dict[Path, threading.Lock] = {}
        self._documents: dict[Path, _Document] = {}
        self._versions: dict[Path, int] = {}
        self._adapter: ClangdAdapter | None = None
        self._identity: tuple[Any, ...] | None = None
        self._generation = threading.Event()
        self._state = "not_started"
        self._closed = False

    def _snapshot(self, source: Path | None = None) -> AnalysisConfigSnapshot:
        snapshot = load_analysis_config(self.context.project_root)
        with self._document_guard:
            known = list(self._document_locks)
        for path in {*known, *([source] if source is not None else [])}:
            validate_clangd_configuration(snapshot, path)
        return snapshot

    def _envelope(
        self, operation: str, snapshot: AnalysisConfigSnapshot | None = None
    ) -> dict[str, Any]:
        return {
            "status": "ok",
            "backend": "clangd",
            "operation": operation,
            "project_root": str(self.context.project_root),
            "compilation_database": str(snapshot.database_path)
            if snapshot and snapshot.database_path
            else None,
            "compilation_database_sha256": snapshot.database_sha256 if snapshot else None,
            "index_completeness": "unverified",
            "data": {},
            "warnings": list(snapshot.warnings) if snapshot else [],
        }

    @staticmethod
    def _program_report(program: ResolvedAnalysisProgram) -> dict[str, Any]:
        return {
            "executable": str(program.executable) if program.executable else None,
            "version": program.version,
            "executable_origin": program.origin,
            "runtime_root": str(program.runtime_root) if program.runtime_root else None,
            "runtime_manifest_sha256": program.runtime_manifest_sha256,
            "analysis_mapping_sha256": program.analysis_mapping_sha256,
            "executable_sha256": program.executable_sha256,
            "data_paths": list(map(str, program.data_paths)),
            "expected_identity": dict(program.expected_identity),
            "observed_identity": dict(program.observed_identity),
        }

    def _observe_runtime(
        self, program: ResolvedAnalysisProgram, deadline: float, cancellation: Any
    ) -> None:
        if (
            self.context.runtime_root is not None
            and self._identity is not None
            and program.identity != self._identity[1]
        ):
            self._invalidate_session(deadline, cancellation)

    def _invalidate_session(self, deadline: float, cancellation: Any) -> None:
        # Close admission before waiting for initialization/synchronization.
        self._generation.set()
        self._state = "restarting"
        _acquire(self._session_guard, deadline, cancellation)
        try:
            if self._adapter is not None:
                for path in self._documents:
                    self._adapter.clear_diagnostics(path.as_uri())
                self._adapter.shutdown(time.monotonic() + 5, graceful=False)
                self._adapter = None
            self._documents.clear()
        finally:
            self._session_guard.release()

    @staticmethod
    def _failure(
        result: dict[str, Any],
        status: str,
        code: str,
        message: str,
        remedy: str = "Fix the stated cause and retry; use get_code_analysis_status for setup details.",
    ) -> dict[str, Any]:
        result.update(
            status=status,
            code=code if code.startswith("analysis/") else "analysis/" + code,
            message=message,
            remedy=remedy,
        )
        return result

    def status(self) -> dict[str, Any]:
        managed = current_operation()
        # The resolver owns the five-second probe cap; this is the actual
        # request budget, including any time already spent in the dispatcher.
        deadline = (
            managed.started_at + managed.timeout_seconds
            if managed is not None
            else time.monotonic() + DEFAULT_OPERATION_TIMEOUT_SECONDS
        )
        cancellation = managed.cancellation_requested if managed is not None else None
        snapshot = self._snapshot()
        result = self._envelope("status", snapshot)
        if snapshot.database_path is not None:
            result["warnings"].append(
                "clangd may write disposable native index shards and .gitignore under "
                + str(snapshot.database_path.parent / ".cache" / "clangd" / "index")
                + "; service-owned reports/metadata use .firm/code-analysis/."
            )
        program = ResolvedAnalysisProgram("clangd", runtime_root=self.context.runtime_root)
        clangd_problems = self._clangd_problems(snapshot)
        integrity_failure: Exception | None = None
        integrity_problems: list[AnalysisProblem] = []
        try:
            remaining(deadline, cancellation)
            program = self.program_resolver(self.context, "clangd", deadline)
            self._observe_runtime(program, deadline, cancellation)
            if program.problems:
                problem = program.problems[0]
                self._failure(result, "unavailable", problem.code, problem.message, problem.remedy)
            elif program.executable is None:
                self._failure(
                    result, "unavailable", "executable-unavailable", "No usable executable."
                )
            elif clangd_problems:
                problem = clangd_problems[0]
                self._failure(
                    result, "not_configured", problem.code, problem.message, problem.remedy
                )
            else:
                try:
                    validate_analysis_program(program, deadline, cancellation)
                except TimeoutError:
                    raise
                except (OSError, ValueError) as exc:
                    # Bind the cause before draining: cleanup/lock failures must
                    # preserve this evidence inside the same typed boundary.
                    integrity_failure = exc
                    problem = AnalysisProblem(
                        "analysis/executable-unavailable",
                        str(exc),
                        "Restore/reinstall the supported runtime, then retry.",
                    )
                    integrity_problems.append(problem)
                    self._invalidate_session(deadline, cancellation)
                    self._failure(
                        result, "unavailable", problem.code, problem.message, problem.remedy
                    )
        except TimeoutError as exc:
            self._failure(
                result,
                "incomplete",
                "timeout",
                str(exc),
            )
        except ClangdFailure as exc:
            self._state = "failed"
            self._failure(result, exc.status, exc.code, str(exc))
            if self._adapter is not None:
                result["stderr_tail"] = self._adapter.stderr_tail
        problems = [
            *program.problems,
            *program.runtime_problems,
            *clangd_problems,
            *[p for p in snapshot.problems if p.backend == "cppcheck"],
            *integrity_problems,
        ]
        adapter = self._adapter
        if adapter is not None:
            if adapter.failed:
                self._state = "failed"
            if (snapshot.identity, program.identity) != self._identity:
                self._generation.set()
                if self._state != "failed":
                    self._state = "restarting"
        state = self._state
        result["data"] = {
            **self._program_report(program),
            "configuration_problems": [p.to_payload() for p in problems],
            "session_state": state,
            "index_limitations": INDEX_LIMITATIONS,
            "required_data_locations": list(map(str, program.data_paths)),
        }
        result["data"]["expected_identity"].update(getattr(integrity_failure, "expected", {}))
        result["data"]["observed_identity"].update(getattr(integrity_failure, "observed", {}))
        result["warnings"].extend(program.warnings)
        return result

    def _session(
        self,
        snapshot: AnalysisConfigSnapshot,
        program: ResolvedAnalysisProgram,
        deadline: float,
        cancellation: Any,
    ) -> tuple[ClangdAdapter, threading.Event]:
        identity = snapshot.identity, program.identity
        _acquire(self._session_guard, deadline, cancellation)
        try:
            if self._closed:
                raise ClangdFailure("child-exited", "Analysis service is closed.")
            try:
                validate_analysis_program(program, deadline, cancellation)
            except TimeoutError:
                raise
            except (OSError, ValueError) as exc:
                self._invalidate_session(deadline, cancellation)
                raise ClangdFailure("configuration-changed", str(exc), status="incomplete") from exc
            if self._adapter is not None and (
                identity != self._identity or self._adapter.failed or self._generation.is_set()
            ):
                self._state = "restarting"
                self._generation.set()
                self._adapter.shutdown(time.monotonic() + 5, graceful=False)
                self._adapter = None
                self._documents.clear()
            if self._adapter is None:
                self._generation = threading.Event()
                self._identity = identity
                self._state = "initializing"
                adapter = self.adapter_factory(program, snapshot, None)
                self._adapter = adapter
                try:
                    adapter.start(deadline, _Cancellation(cancellation, self._generation))
                except BaseException as exc:
                    self._state = "failed"
                    if self._generation.is_set():
                        raise ClangdFailure(
                            "configuration-changed",
                            "Runtime/configuration changed during session initialization.",
                            status="incomplete",
                        ) from exc
                    raise
                self._state = "ready"
            return self._adapter, self._generation
        finally:
            self._session_guard.release()

    def _dependency_identity(self, deadline: float, cancellation: Any) -> tuple[Any, ...]:
        """Observe project headers to invalidate diagnostics; external/global index remains unverified."""
        records: dict[str, str] = {}
        for directory, subdirs, files in os.walk(self.context.project_root):
            subdirs[:] = [
                d
                for d in subdirs
                if d not in {".git", ".venv", ".firm", ".harness-runtime", "node_modules"}
            ]
            remaining(deadline, cancellation)
            for name in files:
                path = Path(directory) / name
                if path.suffix.lower() in HEADER_SUFFIXES:
                    try:
                        records[str(path)] = sha256_bytes(path.read_bytes())
                    except OSError:
                        records[str(path)] = "unreadable"
        with self._document_guard:
            known_headers = [p for p in self._document_locks if p.suffix.lower() in HEADER_SUFFIXES]
        # Explicitly known headers can live in excluded generated/runtime trees.
        for path in known_headers:
            remaining(deadline, cancellation)
            if str(path) in records:
                continue
            try:
                records[str(path)] = sha256_bytes(path.read_bytes())
            except OSError:
                records[str(path)] = "unreadable"
        return tuple(sorted(records.items()))

    def _sync(
        self,
        adapter: ClangdAdapter,
        source: Path,
        text: str,
        digest: str,
        dependencies: tuple[Any, ...],
        deadline: float,
        cancellation: Any,
        *,
        require_diagnostics: bool = False,
    ) -> int:
        _acquire(self._session_guard, deadline, cancellation)
        try:
            if adapter is not self._adapter or self._generation.is_set():
                raise ClangdFailure(
                    "configuration-changed", "Analysis generation invalidated.", status="incomplete"
                )
            old = self._documents.get(source)
            unchanged = (
                old is not None and old.sha256 == digest and old.dependencies == dependencies
            )
            if unchanged and old is not None:
                if (
                    not require_diagnostics
                    or adapter.diagnostics_for(source.as_uri(), old.version) is not None
                ):
                    return old.version
            version = self._versions.get(source, 0) + 1
            self._versions[source] = version
            adapter.clear_diagnostics(source.as_uri())
            document = {"uri": source.as_uri(), "version": version}
            if old is not None and old.sha256 == digest and not unchanged:
                # Known header changes need a fresh parse even with identical
                # source bytes: clangd may deduplicate an unchanged didChange.
                adapter.notify("textDocument/didClose", {"textDocument": {"uri": source.as_uri()}})
            if old is None or (old.sha256 == digest and not unchanged):
                adapter.notify(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            **document,
                            "languageId": "c" if source.suffix == ".c" else "cpp",
                            "text": text,
                        }
                    },
                )
            else:
                adapter.notify(
                    "textDocument/didChange",
                    {
                        "textDocument": document,
                        "contentChanges": [{"text": text}],
                        "wantDiagnostics": True,
                    },
                )
            self._documents[source] = _Document(version, digest, text, dependencies)
            return version
        finally:
            self._session_guard.release()

    def _location(
        self, raw: dict[str, Any], encoding: str, warnings: list[str], source: Path, text: str
    ) -> dict[str, Any]:
        uri = raw.get("targetUri", raw.get("uri"))
        raw_range = raw.get("targetRange", raw.get("range"))
        if not isinstance(uri, str) or not isinstance(raw_range, dict):
            raise ClangdFailure("protocol-error", "Invalid backend location.")
        path = local_uri_path(uri)
        result: dict[str, Any] = {
            "uri": uri,
            "path": str(path) if path else None,
            "position_mapping": "unavailable",
            "range": None,
            "raw_lsp_range": raw_range,
            "lsp_position_encoding": encoding,
        }
        target_text: str | None = None
        try:
            if path is not None:
                target_text = text if path == source else path.read_bytes().decode("utf-8")
                result["range"] = range_from_lsp(target_text, raw_range, encoding)
                result["position_mapping"] = "available"
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            pass
        if result["position_mapping"] == "unavailable":
            warnings.append(
                f"Position mapping unavailable for {uri}; raw LSP coordinates retained."
            )
        for raw_name, public_name, range_text in (
            ("targetSelectionRange", "target_selection_range", target_text),
            ("originSelectionRange", "origin_selection_range", text),
        ):
            if raw_name in raw:
                result["raw_lsp_" + public_name] = raw[raw_name]
                try:
                    result[public_name] = (
                        range_from_lsp(range_text, raw[raw_name], encoding)
                        if range_text is not None
                        else None
                    )
                except (ValueError, KeyError, TypeError):
                    result[public_name] = None
        return result

    @staticmethod
    def _counts(items: list[Any], limit: int) -> dict[str, Any]:
        return {
            "returned_count": min(len(items), limit),
            "observed_count": len(items),
            "truncated": len(items) > limit,
        }

    def _normalize(
        self,
        operation: str,
        raw: Any,
        adapter: ClangdAdapter,
        source: Path,
        text: str,
        result: dict[str, Any],
        limit: int,
    ) -> None:
        encoding = adapter.position_encoding
        warnings = result["warnings"]
        if operation in {"definition", "references"}:
            items = [] if raw is None else raw if isinstance(raw, list) else [raw]
            if not all(isinstance(item, dict) for item in items):
                raise ClangdFailure("protocol-error", "Invalid location response.")
            locations = [
                self._location(item, encoding, warnings, source, text) for item in items[:limit]
            ]
            result["data"] = {"locations": locations, **self._counts(items, limit)}
            if not items:
                result["status"] = "not_found"
        elif operation == "hover":
            if raw is None:
                result.update(status="not_found", data={"contents": None})
                return
            if not isinstance(raw, dict):
                raise ClangdFailure("protocol-error", "Invalid hover response.")
            contents = raw.get("contents")
            if (
                isinstance(contents, dict)
                and contents.get("kind") in {"markdown", "plaintext"}
                and isinstance(contents.get("value"), str)
            ):
                normalized = {"kind": contents["kind"], "value": contents["value"]}
            else:
                marked = contents if isinstance(contents, list) else [contents]
                parts: list[str] = []
                for part in marked:
                    if isinstance(part, str):
                        parts.append(part)
                    elif (
                        isinstance(part, dict)
                        and isinstance(part.get("language"), str)
                        and isinstance(part.get("value"), str)
                    ):
                        parts.append(f"```{part['language']}\n{part['value']}\n```")
                    else:
                        raise ClangdFailure("protocol-error", "Invalid hover contents.")
                normalized = {"kind": "markdown", "value": "\n\n".join(parts)}
            result["data"] = {
                "contents": normalized,
                "documentation_trust": "untrusted_source_text",
            }
            if "range" in raw:
                result["data"]["range"] = range_from_lsp(text, raw["range"], encoding)
            if not normalized["value"]:
                result["status"] = "not_found"
        else:
            if not isinstance(raw, list):
                raise ClangdFailure("protocol-error", "Invalid diagnostics response.")
            diagnostics: list[dict[str, Any]] = []
            for diagnostic in raw[:limit]:
                if not isinstance(diagnostic, dict) or not isinstance(
                    diagnostic.get("message"), str
                ):
                    raise ClangdFailure("protocol-error", "Invalid diagnostic.")
                diagnostics.append(
                    {
                        "severity": diagnostic.get("severity")
                        if type(diagnostic.get("severity")) is int
                        and diagnostic.get("severity") in {1, 2, 3, 4}
                        else None,
                        "message": diagnostic["message"],
                        "code": diagnostic.get("code")
                        if type(diagnostic.get("code")) in {str, int}
                        else None,
                        "source": diagnostic.get("source")
                        if isinstance(diagnostic.get("source"), str)
                        else None,
                        "range": range_from_lsp(text, diagnostic["range"], encoding),
                        "related_locations": [
                            {
                                "message": related.get("message"),
                                **self._location(
                                    related["location"], encoding, warnings, source, text
                                ),
                            }
                            for related in diagnostic.get("relatedInformation", [])
                        ],
                    }
                )
            result["data"] = {"diagnostics": diagnostics, **self._counts(raw, limit)}
            self._coverage(result, raw)

    def _coverage(self, result: dict[str, Any], diagnostics: list[dict[str, Any]]) -> None:
        if not all(isinstance(item, dict) for item in diagnostics):
            raise ClangdFailure("protocol-error", "Invalid diagnostics notification.")
        blockers = [
            item for item in diagnostics if str(item.get("code")) in COVERAGE_DIAGNOSTIC_CODES
        ]
        if blockers:
            self._failure(
                result,
                "incomplete",
                "compile-context-missing",
                "clangd reports missing include/unsupported compiler settings; firmware parsing coverage is incomplete.",
                "Build generated headers and correct target/includes/flags in the selected native database.",
            )
            result["warnings"].extend(str(item.get("message")) for item in blockers)

    def _query(
        self,
        operation: str,
        file_path: str,
        line: int | None,
        column: int | None,
        timeout_seconds: float,
        max_results: int = 100,
        include_declaration: bool = False,
    ) -> dict[str, Any]:
        result = self._envelope(operation)
        adapter: ClangdAdapter | None = None
        generation: threading.Event | None = None
        lock: threading.Lock | None = None
        locked = False
        try:
            try:
                valid_timeout = (
                    type(timeout_seconds) in {int, float}
                    and math.isfinite(timeout_seconds)
                    and timeout_seconds > 0
                )
            except OverflowError:
                valid_timeout = False
            if (
                not valid_timeout
                or type(max_results) is not int
                or not 1 <= max_results <= 1000
                or type(include_declaration) is not bool
            ):
                raise ValueError(
                    "timeout_seconds must be positive finite; max_results integer 1..1000; include_declaration boolean."
                )
            managed = current_operation()
            deadline = (managed.started_at if managed else time.monotonic()) + timeout_seconds
            operation_cancel = managed.cancellation_requested if managed else None
            remaining(deadline, operation_cancel)
            if (
                not isinstance(file_path, str)
                or not file_path.strip()
                or "://" in file_path
                or file_path.startswith(("//", "\\\\"))
                or "\0" in file_path
            ):
                raise ValueError(
                    "file_path must be an existing local C/C++ path, not URI/network input."
                )
            candidate = Path(file_path)
            source = (
                candidate if candidate.is_absolute() else self.context.project_root / candidate
            ).resolve()
            if (
                not source.is_file()
                or source.suffix.lower() not in SOURCE_SUFFIXES | HEADER_SUFFIXES
            ):
                raise ValueError("file_path must name an existing C/C++ source/header file.")
            with self._document_guard:
                lock = self._document_locks.setdefault(source, threading.Lock())
            _acquire(lock, deadline, operation_cancel)
            locked = True
            saved = source.read_bytes()
            text = saved.decode("utf-8")
            digest = sha256_bytes(saved)
            result["source"] = {"path": str(source), "sha256": digest, "document_version": None}
            if operation != "diagnostics":
                if line is None or column is None:
                    raise ValueError("line and column are required.")
                position_to_lsp(
                    text, line, column, "utf-32"
                )  # validate public coordinates before native work
            snapshot = self._snapshot(source)
            result.update(self._envelope(operation, snapshot))
            remaining(deadline, operation_cancel)
            program = self.program_resolver(self.context, "clangd", deadline)
            result["data"].update(self._program_report(program))
            result["data"]["configuration_problems"] = [
                p.to_payload() for p in (*program.problems, *program.runtime_problems)
            ]
            result["warnings"].extend(program.warnings)
            self._observe_runtime(program, deadline, operation_cancel)
            if (
                self._adapter is not None
                and (snapshot.identity, program.identity) != self._identity
            ):
                self._generation.set()
                self._state = "restarting"
            if program.problems or program.executable is None:
                problem = program.problems[0] if program.problems else None
                return self._failure(
                    result,
                    "unavailable",
                    problem.code if problem else "executable-unavailable",
                    problem.message if problem else "No usable clangd.",
                    problem.remedy if problem else "Restore clangd and required data.",
                )
            clangd_problems = self._clangd_problems(snapshot)
            if clangd_problems:
                problem = clangd_problems[0]
                return self._failure(
                    result, "not_configured", problem.code, problem.message, problem.remedy
                )
            entry = next((e for e in snapshot.entries if e.source == source), None)
            if entry is None and source.suffix.lower() not in HEADER_SUFFIXES:
                return self._failure(
                    result,
                    "not_configured",
                    "compile-context-missing",
                    "Queried source has no selected-build command.",
                    "Export this source in the selected build compilation database.",
                )
            origin = None
            if entry is None:
                # Prime a genuine selected unit before opening a header: clangd can
                # borrow its command. Disclose inference; never fabricate a unit match.
                origin = min(
                    snapshot.entries,
                    key=lambda e: (e.source.parent != source.parent, e.source.stem != source.stem),
                )
                result["warnings"].append(
                    f"Inferred header context from selected build; candidate originating unit: {origin.source}. clangd may borrow another selected unit."
                )
            dependency_identity = self._dependency_identity(deadline, operation_cancel)
            adapter, generation = self._session(snapshot, program, deadline, operation_cancel)
            cancellation = _Cancellation(operation_cancel, generation)
            if origin is not None:
                with self._document_guard:
                    origin_lock = self._document_locks.setdefault(origin.source, threading.Lock())
                # Avoid lock inversion between two header calls: unit priming uses
                # its own short lock and holds no other unit lock.
                _acquire(origin_lock, deadline, cancellation)
                try:
                    origin_saved = origin.source.read_bytes()
                    self._sync(
                        adapter,
                        origin.source,
                        origin_saved.decode("utf-8"),
                        sha256_bytes(origin_saved),
                        dependency_identity,
                        deadline,
                        cancellation,
                    )
                finally:
                    origin_lock.release()
            capability = {
                "definition": "definitionProvider",
                "references": "referencesProvider",
                "hover": "hoverProvider",
            }.get(operation)
            if capability and not (
                adapter.capabilities.get(capability) is True
                or isinstance(adapter.capabilities.get(capability), dict)
            ):
                raise ClangdFailure(
                    "capability-unsupported",
                    f"Backend does not provide {operation}.",
                    status="incomplete",
                )
            version = self._sync(
                adapter,
                source,
                text,
                digest,
                dependency_identity,
                deadline,
                cancellation,
                require_diagnostics=operation == "diagnostics",
            )
            result["source"]["document_version"] = version
            if operation == "diagnostics":
                raw = adapter.wait_diagnostics(source.as_uri(), version, deadline, cancellation)
            else:
                assert line is not None and column is not None
                params: dict[str, Any] = {
                    "textDocument": {"uri": source.as_uri()},
                    "position": position_to_lsp(text, line, column, adapter.position_encoding),
                }
                if operation == "references":
                    params["context"] = {"includeDeclaration": include_declaration}
                raw = adapter.request("textDocument/" + operation, params, deadline, cancellation)
            try:
                self._normalize(
                    operation,
                    raw,
                    adapter,
                    source,
                    text,
                    result,
                    max_results
                    if operation != "definition"
                    else len(raw)
                    if isinstance(raw, list)
                    else 1,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ClangdFailure("protocol-error", f"Invalid backend result: {exc}") from exc
            result["data"].update(self._program_report(program))
            result["data"]["configuration_problems"] = [
                p.to_payload() for p in program.runtime_problems
            ]
            if operation != "diagnostics":
                diagnostic_evidence = adapter.diagnostics_for(source.as_uri(), version)
                if diagnostic_evidence is None:
                    result["warnings"].append(
                        "No matching diagnostics evidence was observed alongside this semantic query; target parsing coverage is unverified."
                    )
                else:
                    self._coverage(result, diagnostic_evidence)
            if generation.is_set() or self._snapshot(source).identity != snapshot.identity:
                return self._failure(
                    result,
                    "incomplete",
                    "configuration-changed",
                    "Configuration/database changed during analysis.",
                )
            if sha256_bytes(source.read_bytes()) != digest:
                return self._failure(
                    result, "incomplete", "source-changed", "Saved source changed during analysis."
                )
            dependencies_changed = (
                self._dependency_identity(deadline, cancellation) != dependency_identity
            )
            try:
                validate_analysis_program(program, deadline, cancellation)
            except TimeoutError:
                raise
            except (OSError, ValueError) as exc:
                self._invalidate_session(deadline, operation_cancel)
                result["data"]["expected_identity"].update(getattr(exc, "expected", {}))
                result["data"]["observed_identity"].update(getattr(exc, "observed", {}))
                return self._failure(result, "incomplete", "configuration-changed", str(exc))
            if dependencies_changed:
                adapter.clear_diagnostics(source.as_uri())
                return self._failure(
                    result,
                    "incomplete",
                    "source-changed",
                    "A project header changed during analysis; diagnostics invalidated.",
                )
            remaining(deadline, cancellation)
            result["warnings"].extend(INDEX_LIMITATIONS)
            return result
        except ClangdFailure as exc:
            failure = exc
            if failure.code == "analysis/cleanup-failed":
                self._state = "failed"
                if adapter is not None:
                    result["stderr_tail"] = adapter.stderr_tail
                return self._failure(result, failure.status, failure.code, str(failure))
            if generation is not None and generation.is_set():
                return self._failure(
                    result,
                    "incomplete",
                    "configuration-changed",
                    "Previous analysis generation was invalidated.",
                )
            if adapter is not None and adapter.failed:
                self._state = "failed"
                try:
                    adapter.shutdown(time.monotonic() + 5, graceful=False)
                except ClangdFailure as cleanup:
                    failure = cleanup
            self._failure(result, failure.status, failure.code, str(failure))
            if adapter is not None:
                result["stderr_tail"] = adapter.stderr_tail
            return result
        except TimeoutError as exc:
            return self._failure(
                result,
                "incomplete",
                "configuration-changed"
                if generation is not None and generation.is_set()
                else "timeout",
                str(exc),
            )
        except (ValueError, UnicodeError) as exc:
            return self._failure(
                result,
                "error",
                "invalid-argument",
                str(exc),
                "Use saved UTF-8 C/C++ files and strict documented argument types/positions.",
            )
        except (OSError, KeyError, TypeError) as exc:
            return self._failure(result, "error", "protocol-error", str(exc))
        finally:
            if locked and lock is not None:
                lock.release()

    def definition(
        self, file_path: str, line: int, column: int, timeout_seconds: float = 30
    ) -> dict[str, Any]:
        return self._query("definition", file_path, line, column, timeout_seconds)

    def references(
        self,
        file_path: str,
        line: int,
        column: int,
        timeout_seconds: float = 30,
        include_declaration: bool = False,
        max_results: int = 100,
    ) -> dict[str, Any]:
        return self._query(
            "references", file_path, line, column, timeout_seconds, max_results, include_declaration
        )

    def hover(
        self, file_path: str, line: int, column: int, timeout_seconds: float = 30
    ) -> dict[str, Any]:
        return self._query("hover", file_path, line, column, timeout_seconds)

    def diagnostics(
        self, file_path: str, max_results: int = 100, timeout_seconds: float = 30
    ) -> dict[str, Any]:
        return self._query("diagnostics", file_path, None, None, timeout_seconds, max_results)

    def shutdown(self) -> None:
        with self._session_guard:
            self._closed = True
            self._generation.set()
            if self._adapter is not None:
                self._adapter.shutdown(time.monotonic() + 5)
                self._adapter = None
            self._documents.clear()
            self._state = "closed"
