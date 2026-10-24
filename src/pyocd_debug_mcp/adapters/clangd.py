"""Owned persistent clangd transport and LSP position conversion.

Reader and writer threads keep protocol bytes away from MCP stdout. Each
request owns only a waiter, never the shared subprocess.
"""

from __future__ import annotations

import json
import copy
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from pyocd_debug_mcp.code_analysis_config import AnalysisConfigSnapshot
from pyocd_debug_mcp.code_analysis_runtime import (
    Cancellation,
    ResolvedAnalysisProgram,
    analysis_child_environment,
    analysis_cleanup_scope_confirmed,
    remaining,
    spawn_before_deadline,
)
from pyocd_debug_mcp.kernel.processes import (
    DEFAULT_PROCESS_GROUP_CLEANUP_GRACE_SECONDS,
    MAX_OWNED_PROCESS_CLEANUP_SECONDS,
    ProcessMarkerStore,
    terminate_process_group,
)
from pyocd_debug_mcp.kernel.operations import ARGUMENT_TIMEOUT_GRACE_SECONDS


class ClangdFailure(RuntimeError):
    def __init__(self, code: str, message: str, *, status: str = "error") -> None:
        super().__init__(message)
        self.code = code if code.startswith("analysis/") else "analysis/" + code
        self.status = status


def text_lines(text: str) -> list[str]:
    # LSP line breaks are CRLF/LF/CR, not Unicode separators consumed by splitlines.
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def position_to_lsp(text: str, line: int, column: int, encoding: str) -> dict[str, int]:
    lines = text_lines(text)
    if (
        type(line) is not int
        or type(column) is not int
        or not 1 <= line <= len(lines)
        or not 1 <= column <= len(lines[line - 1]) + 1
    ):
        raise ValueError(
            "Position must be a valid 1-based line/Unicode code-point column (line-end is allowed)."
        )
    prefix = lines[line - 1][: column - 1]
    units = {
        "utf-8": lambda: len(prefix.encode("utf-8")),
        "utf-16": lambda: len(prefix.encode("utf-16-le")) // 2,
        "utf-32": lambda: len(prefix),
    }
    if encoding not in units:
        raise ValueError("Unsupported position encoding.")
    return {"line": line - 1, "character": units[encoding]()}


def position_from_lsp(text: str, position: dict[str, Any], encoding: str) -> dict[str, int]:
    line, character = position.get("line"), position.get("character")
    lines = text_lines(text)
    if (
        type(line) is not int
        or type(character) is not int
        or not 0 <= line < len(lines)
        or character < 0
    ):
        raise ValueError("Invalid LSP position.")
    units = 0
    for index, char in enumerate(lines[line]):
        if units == character:
            return {"line": line + 1, "column": index + 1}
        units += (
            len(char.encode("utf-8"))
            if encoding == "utf-8"
            else len(char.encode("utf-16-le")) // 2
            if encoding == "utf-16"
            else 1
        )
        if units > character:
            raise ValueError("LSP position splits a Unicode character.")
    if units == character:
        return {"line": line + 1, "column": len(lines[line]) + 1}
    raise ValueError("LSP position exceeds line length.")


def range_from_lsp(text: str, value: dict[str, Any], encoding: str) -> dict[str, Any]:
    return {
        "start": position_from_lsp(text, value["start"], encoding),
        "end": position_from_lsp(text, value["end"], encoding),
    }


def local_uri_path(uri: str) -> Path | None:
    parts = urlsplit(uri)
    if (
        parts.scheme != "file"
        or parts.netloc not in {"", "localhost"}
        or parts.query
        or parts.fragment
    ):
        return None
    try:
        decoded = unquote(parts.path, encoding="utf-8", errors="strict")
    except UnicodeError:
        return None
    if not decoded or "\0" in decoded or decoded.startswith(("//", "\\\\")):
        return None
    if os.name == "nt":
        # Decode the colon before recognizing a drive. nturl2path in Python
        # 3.10-3.12 otherwise treats /c%3A/... as a root-relative path.
        if (
            len(decoded) < 4
            or decoded[0] != "/"
            or not "A" <= decoded[1].upper() <= "Z"
            or decoded[2] != ":"
            or decoded[3] not in "/\\"
        ):
            return None
        decoded = decoded[1:]
    path = Path(decoded)
    if not path.is_absolute():
        return None
    return path.resolve()


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    failure: ClangdFailure | None = None


class ClangdAdapter:
    def __init__(
        self,
        program: ResolvedAnalysisProgram,
        config_snapshot: AnalysisConfigSnapshot,
        marker_store: ProcessMarkerStore | None = None,
    ) -> None:
        self.program = program
        self.config = config_snapshot
        self.marker_store = marker_store or ProcessMarkerStore()
        self.capabilities: dict[str, Any] = {}
        self.position_encoding = "utf-16"
        self.process: subprocess.Popen[Any] | None = None
        self.marker: Path | None = None
        self._last_cleanup_receipt: dict[str, Any] | None = None
        self._guard = threading.Condition(threading.RLock())
        self._shutdown_guard = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0
        self._failure: ClangdFailure | None = None
        self._closing = False
        self._outgoing: queue.Queue[bytes | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stderr = bytearray()
        self._diagnostics: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self._unversioned: set[str] = set()
        self._workspace = [
            {"uri": self.config.project_root.as_uri(), "name": self.config.project_root.name}
        ]

    @property
    def stderr_tail(self) -> str:
        with self._guard:
            return bytes(self._stderr).decode("utf-8", errors="replace")

    @property
    def failed(self) -> bool:
        with self._guard:
            return self._failure is not None

    def _fail(self, failure: ClangdFailure) -> None:
        with self._guard:
            if self._failure is None:
                self._failure = failure
            for pending in self._pending.values():
                pending.failure = self._failure
                pending.event.set()
            self._guard.notify_all()

    def _argv(self) -> list[str]:
        """Private subclass seam for a scriptable peer; no tool-call override."""
        if self.program.executable is None or self.config.database_path is None:
            raise ClangdFailure("executable-unavailable", "No resolved clangd/database.")
        argv = [
            str(self.program.executable),
            "--enable-config",
            "--background-index",
            "--limit-references=0",
            "--limit-results=0",
            "--compile-commands-dir=" + str(self.config.database_path.parent),
            "--query-driver="
            + ",".join(str(p).replace("\\", "/") for p in self.config.query_driver_paths),
        ]
        if self.program.origin == "managed_runtime" and self.program.data_paths:
            argv.append("--resource-dir=" + str(self.program.data_paths[0]))
        return argv

    def start(self, deadline: float, cancellation: Cancellation | None = None) -> None:
        argv = self._argv()
        assert self.config.database_path is not None
        try:
            self.process, self.marker = spawn_before_deadline(
                argv,
                deadline,
                cancellation,
                self.marker_store,
                env=analysis_child_environment(self.program),
                cwd=self.config.project_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            for name, target in (
                ("reader", self._read),
                ("writer", self._write),
                ("stderr", self._drain_stderr),
            ):
                thread = threading.Thread(target=target, name="clangd-" + name, daemon=True)
                self._threads.append(thread)
                thread.start()
            response = self.request(
                "initialize",
                {
                    "processId": None,
                    "rootUri": self.config.project_root.as_uri(),
                    "workspaceFolders": self._workspace,
                    "capabilities": {
                        "general": {"positionEncodings": ["utf-8", "utf-16", "utf-32"]},
                        "workspace": {"workspaceFolders": True, "configuration": True},
                        "textDocument": {
                            "synchronization": {"dynamicRegistration": False},
                            "publishDiagnostics": {
                                "versionSupport": True,
                                "relatedInformation": True,
                            },
                            "definition": {"linkSupport": True},
                            "hover": {"contentFormat": ["markdown", "plaintext"]},
                        },
                    },
                    "initializationOptions": {
                        "compilationDatabasePath": str(self.config.database_path.parent)
                    },
                },
                deadline,
                cancellation,
            )
            if not isinstance(response, dict) or not isinstance(response.get("capabilities"), dict):
                raise ClangdFailure("protocol-error", "Invalid initialize capabilities.")
            self.capabilities = response["capabilities"]
            self.position_encoding = self.capabilities.get("positionEncoding", "utf-16")
            if self.position_encoding not in {"utf-8", "utf-16", "utf-32"}:
                raise ClangdFailure(
                    "capability-unsupported",
                    "Unsupported LSP position encoding.",
                    status="incomplete",
                )
            sync = self.capabilities.get("textDocumentSync")
            if isinstance(sync, dict):
                sync_ok = (
                    sync.get("openClose") is True
                    and type(sync.get("change")) is int
                    and sync.get("change") in {1, 2}
                )
            else:
                sync_ok = type(sync) is int and sync in {1, 2}
            if not sync_ok:
                raise ClangdFailure(
                    "capability-unsupported",
                    "Backend lacks saved-document synchronization.",
                    status="incomplete",
                )
            self.notify("initialized", {})
        except BaseException as exc:
            if self.process is None:
                self._last_cleanup_receipt = copy.deepcopy(getattr(exc, "cleanup_receipt", None))
            if not self._closing:
                self.shutdown(time.monotonic() + 5, graceful=False)
            if isinstance(exc, (ClangdFailure, TimeoutError, KeyboardInterrupt, SystemExit)):
                raise
            raise ClangdFailure(
                "protocol-error", f"Cannot start owned clangd transport: {exc}"
            ) from exc

    def _send(self, value: dict[str, Any]) -> None:
        with self._guard:
            cleanup = value.get("method") in {"$/cancelRequest", "shutdown", "exit"}
            response = "method" not in value and "id" in value
            if (self._failure is not None or self._closing) and not (cleanup or response):
                raise self._failure or ClangdFailure("child-exited", "Analysis session is closing.")
            data = json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            self._outgoing.put(
                b"Content-Length: " + str(len(data)).encode("ascii") + b"\r\n\r\n" + data
            )

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def cancel(self, request_id: int) -> None:
        self.notify("$/cancelRequest", {"id": request_id})

    def request(
        self, method: str, params: Any, deadline: float, cancellation: Cancellation | None = None
    ) -> Any:
        remaining(deadline, cancellation)
        with self._guard:
            self._next_id += 1
            request_id = self._next_id
            pending = _Pending()
            self._pending[request_id] = pending
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            while not pending.event.wait(min(0.02, remaining(deadline, cancellation))):
                pass
            remaining(deadline, cancellation)
            if pending.failure is not None:
                raise pending.failure
            return pending.result
        except TimeoutError:
            if not self._closing:
                self.shutdown(
                    time.monotonic() + ARGUMENT_TIMEOUT_GRACE_SECONDS,
                    failure=ClangdFailure(
                        "timeout",
                        "Shared clangd generation stopped after request deadline/cancellation.",
                        status="incomplete",
                    ),
                )
            raise
        finally:
            with self._guard:
                self._pending.pop(request_id, None)

    def clear_diagnostics(self, uri: str) -> None:
        with self._guard:
            self._diagnostics = {
                key: value for key, value in self._diagnostics.items() if key[0] != uri
            }
            self._unversioned.discard(uri)

    def diagnostics_for(self, uri: str, version: int) -> list[dict[str, Any]] | None:
        """Peek at explicit revision evidence without treating silence as clean."""
        with self._guard:
            return self._diagnostics.get((uri, version))

    def wait_diagnostics(
        self, uri: str, version: int, deadline: float, cancellation: Cancellation | None = None
    ) -> list[dict[str, Any]]:
        try:
            with self._guard:
                while (uri, version) not in self._diagnostics:
                    if self._failure is not None:
                        raise self._failure
                    self._guard.wait(min(0.02, remaining(deadline, cancellation)))
                remaining(deadline, cancellation)
                return self._diagnostics[uri, version]
        except TimeoutError as exc:
            with self._guard:
                unversioned = uri in self._unversioned
            raise ClangdFailure(
                "diagnostics-unversioned" if unversioned else "diagnostics-missing",
                "No explicit diagnostics notification matched the requested saved document version.",
                status="incomplete",
            ) from exc

    def _write(self) -> None:
        stream = None
        try:
            assert self.process is not None and self.process.stdin is not None
            stream = self.process.stdin
            while True:
                data = self._outgoing.get()
                if data is None:
                    return
                view = memoryview(data)
                while view:
                    written = stream.write(view)
                    if not written:
                        raise OSError("clangd stdin closed")
                    view = view[written:]
        except (OSError, ValueError) as exc:
            self._fail(ClangdFailure("protocol-error", f"LSP write failed: {exc}"))
        finally:
            if stream is not None:
                stream.close()

    def _read(self) -> None:
        try:
            assert self.process is not None and self.process.stdout is not None
            stream = self.process.stdout
            while True:
                headers: dict[str, str] = {}
                while True:
                    line = stream.readline()
                    if not line:
                        raise ClangdFailure("child-exited", "clangd stdout reached EOF.")
                    if line == b"\r\n":
                        break
                    key, separator, value = line.decode("ascii").strip().partition(":")
                    if not separator or key.lower() in headers:
                        raise ValueError("malformed/duplicate LSP header")
                    headers[key.lower()] = value.strip()
                length = int(headers["content-length"])
                if length < 0:
                    raise ValueError("negative Content-Length")
                data = bytearray()
                while len(data) < length:
                    part = stream.read(length - len(data))
                    if not part:
                        raise ClangdFailure("child-exited", "clangd exited in a partial frame.")
                    data.extend(part)
                message = json.loads(data.decode("utf-8"))
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise ValueError("invalid JSON-RPC message")
                self._route(message)
        except ClangdFailure as exc:
            self._fail(exc)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._fail(ClangdFailure("protocol-error", f"Invalid LSP transport: {exc}"))
        finally:
            try:
                self._shutdown(time.monotonic() + 5, graceful=False, wait_for_owner=False)
            except ClangdFailure as exc:
                self._fail(exc)

    def _route(self, message: dict[str, Any]) -> None:
        if "method" in message:
            method = message["method"]
            params = message.get("params", {})
            if "id" in message:
                reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
                if method == "workspace/workspaceFolders":
                    reply["result"] = self._workspace
                elif method == "workspace/configuration":
                    reply["result"] = [None for _ in params.get("items", [])]
                elif method == "workspace/applyEdit":
                    reply["result"] = {
                        "applied": False,
                        "failureReason": "Saved-file read-only analysis.",
                    }
                elif method in {"window/showMessageRequest", "window/workDoneProgress/create"}:
                    reply["result"] = None
                else:
                    reply["error"] = {
                        "code": -32601,
                        "message": "Unsupported server request; no commands or dynamic registrations are executed.",
                    }
                self._send(reply)
            elif method == "textDocument/publishDiagnostics":
                uri, version, diagnostics = (
                    params.get("uri"),
                    params.get("version"),
                    params.get("diagnostics"),
                )
                if not isinstance(uri, str) or not isinstance(diagnostics, list):
                    raise ValueError("invalid diagnostics notification")
                with self._guard:
                    if type(version) is int:
                        previous = [
                            key for key in self._diagnostics if key[0] == uri and key[1] < version
                        ]
                        for key in previous:
                            del self._diagnostics[key]
                        self._diagnostics[uri, version] = diagnostics
                    else:
                        self._unversioned.add(uri)
                    self._guard.notify_all()
            return
        request_id = message.get("id")
        if type(request_id) is not int:
            raise ValueError("reply id must match a numeric client request")
        with self._guard:
            pending = self._pending.get(request_id)
            if pending is None:
                return  # cancelled/late response
            if "error" in message:
                error = message["error"]
                code = error.get("code") if isinstance(error, dict) else None
                pending.failure = ClangdFailure(
                    "protocol-error",
                    f"LSP {code}: {error}",
                    status="incomplete" if code in {-32800, -32801, -32802} else "error",
                )
            elif "result" in message:
                pending.result = message["result"]
            else:
                pending.failure = ClangdFailure(
                    "protocol-error", "Reply has neither result nor error."
                )
            pending.event.set()

    def _drain_stderr(self) -> None:
        try:
            assert self.process is not None and self.process.stderr is not None
            while True:
                part = self.process.stderr.read(4096)
                if not part:
                    return
                with self._guard:
                    self._stderr.extend(part)
                    del self._stderr[:-16384]  # bounded execution-error evidence
        except (OSError, ValueError):
            return

    def shutdown(
        self, deadline: float, *, graceful: bool = True, failure: ClangdFailure | None = None
    ) -> None:
        self._shutdown(deadline, graceful=graceful, failure=failure, wait_for_owner=True)

    def _shutdown(
        self,
        deadline: float,
        *,
        wait_for_owner: bool,
        graceful: bool = True,
        failure: ClangdFailure | None = None,
    ) -> None:
        # Reader cleanup must yield to an owner that may be joining the reader.
        # Other callers wait for that owner's confirmed cleanup or recovery.
        if not self._shutdown_guard.acquire(blocking=wait_for_owner):
            return
        try:
            if not wait_for_owner and self._closing:
                return  # A previous owner retains any failed cleanup for recovery.
            process = self.process
            if process is None:
                return
            self._closing = True
            transport_usable = not self.failed
            with self._guard:
                pending_ids = list(self._pending)
            for request_id in pending_ids:
                try:
                    self.cancel(request_id)
                except ClangdFailure:
                    break
            # Hold shutdown ownership while waking sibling callers, so their
            # cleanup cannot kill the child before the cancel/close exchange.
            if failure is not None:
                self._fail(failure)
            # MCP stdio clients may escalate after two seconds of EOF. clangd
            # can keep indexing the standard library after acknowledging exit;
            # bound that wait by the existing native group grace, then own/kill it.
            graceful_deadline = min(
                deadline - MAX_OWNED_PROCESS_CLEANUP_SECONDS,
                time.monotonic() + DEFAULT_PROCESS_GROUP_CLEANUP_GRACE_SECONDS,
            )
            if graceful and transport_usable and process.poll() is None:
                try:
                    # FIFO writes plus this bounded roundtrip let a writable
                    # peer receive the preceding cancellations before termination.
                    # A blocked pipe/server expires this existing grace and is killed.
                    self.request("shutdown", None, graceful_deadline)
                    self.notify("exit", {})
                    process.wait(timeout=max(0.001, graceful_deadline - time.monotonic()))
                except (ClangdFailure, TimeoutError, subprocess.TimeoutExpired):
                    pass
            self._fail(ClangdFailure("child-exited", "Analysis session closed."))
            self._outgoing.put(None)
            # Only the writer closes stdin: closing it from here can block on
            # a synchronous WriteFile to a wedged peer. Let a writable pipe
            # deliver EOF before native termination; a blocked writer is freed
            # by termination within the existing cleanup budget.
            for thread in self._threads:
                if thread.name == "clangd-writer" and thread is not threading.current_thread():
                    thread.join(
                        timeout=max(
                            0,
                            min(
                                DEFAULT_PROCESS_GROUP_CLEANUP_GRACE_SECONDS,
                                deadline - MAX_OWNED_PROCESS_CLEANUP_SECONDS - time.monotonic(),
                            ),
                        )
                    )
            try:
                cleaned = terminate_process_group(process)
            finally:
                self._last_cleanup_receipt = copy.deepcopy(
                    getattr(process, "cleanup_receipt", None)
                )
            scope_cleaned = cleaned and (
                os.name == "nt" or analysis_cleanup_scope_confirmed(self._last_cleanup_receipt)
            )
            if scope_cleaned:
                for thread in self._threads:
                    if thread is not threading.current_thread():
                        thread.join(timeout=max(0, deadline - time.monotonic()))
                if any(
                    thread is not threading.current_thread() and thread.is_alive()
                    for thread in self._threads
                ):
                    raise ClangdFailure(
                        "cleanup-failed",
                        "clangd transport still blocked; recovery marker retained.",
                    )
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
                self.marker_store.remove(self.marker)
                self.marker = None
                self.process = None
            else:
                raise ClangdFailure(
                    "cleanup-failed", "clangd termination unconfirmed; recovery marker retained."
                )
        except OSError as exc:
            raise ClangdFailure(
                "cleanup-failed", f"clangd cleanup failed: {exc}; recovery marker retained."
            ) from exc
        finally:
            self._shutdown_guard.release()
