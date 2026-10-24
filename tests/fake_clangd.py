"""Scriptable fake clangd language server for code-analysis contract tests.

The peer speaks real Content-Length framed JSON-RPC over binary stdio, so the
adapter under test exercises its actual transport, reader and owned-process
code.  Behavior comes from a JSON scenario file named by ``--scenario``; every
received message, argv and lifecycle event is appended to the scenario's JSONL
log so tests can assert what the client really sent.  The fake never runs a
server-supplied command, never touches hardware and never opens the network.

Invocation (normally from a private ``ClangdAdapter._argv`` override in
``code_analysis_support``, which keeps the adapter's real owned pipes)::

    python fake_clangd.py --scenario SCENARIO.json [clangd arguments ...]

The scenario file is re-read for every message, so a test may change the
behavior of an already running peer (``FakeClangd.configure`` in the support
module writes it atomically).  Process-level keys such as
``position_encoding`` naturally only matter at initialization.

``--version`` prints a clangd-like banner and exits 0 (or follows the scenario's
``version`` behavior) without starting the protocol loop.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO

FAKE_VERSION_BANNER = "clangd version 23.1.0-fake (code-analysis contract peer)"
_GATE_LIMIT_SECONDS = 30.0


class FakeClangd:
    def __init__(
        self, scenario: dict[str, Any], argv: list[str], scenario_path: Path | None = None
    ) -> None:
        self.scenario = scenario
        self.scenario_path = scenario_path
        self.argv = argv
        self.log_path = Path(scenario["log"])
        self.stdin: BinaryIO = sys.stdin.buffer
        self.stdout: BinaryIO = sys.stdout.buffer
        self.write_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.documents: dict[str, dict[str, Any]] = {}
        self.sync_count = 0
        self.method_counts: dict[str, int] = {}
        self.held: dict[str, list[dict[str, Any]]] = {}
        self.next_server_id = 0
        self.shutdown_requested = False

    # ------------------------------------------------------------------ logging
    def log(self, event: str, **fields: Any) -> None:
        record = {"event": event, "pid": os.getpid(), "at": time.monotonic(), **fields}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self.log_lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()

    def reload(self) -> None:
        """Pick up scenario edits made by the test while this peer runs."""

        if self.scenario_path is None:
            return
        for _ in range(50):
            try:
                self.scenario = json.loads(self.scenario_path.read_text(encoding="utf-8"))
                return
            except (OSError, ValueError):
                time.sleep(0.01)

    # ---------------------------------------------------------------- transport
    def read_message(self) -> dict[str, Any] | None:
        headers: dict[str, str] = {}
        while True:
            line = self.stdin.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            name, _, value = line.decode("ascii").partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers["content-length"])
        body = b""
        while len(body) < length:
            chunk = self.stdin.read(length - len(body))
            if not chunk:
                return None
            body += chunk
        return json.loads(body.decode("utf-8"))

    @staticmethod
    def frame(message: dict[str, Any]) -> bytes:
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body

    def write_raw(self, data: bytes, *, split: bool = False) -> None:
        with self.write_lock:
            if split and len(data) > 4:
                # Deliberately split inside the header, at the header/body
                # boundary and inside a multibyte body to exercise reassembly.
                header_end = data.index(b"\r\n\r\n") + 4
                cuts = sorted(
                    {3, header_end - 2, header_end, header_end + (len(data) - header_end) // 2}
                )
                start = 0
                for cut in [*cuts, len(data)]:
                    if cut <= start or cut > len(data):
                        continue
                    self.stdout.write(data[start:cut])
                    self.stdout.flush()
                    time.sleep(0.02)
                    start = cut
            else:
                self.stdout.write(data)
                self.stdout.flush()

    def send(self, message: dict[str, Any], *, split: bool | None = None) -> None:
        if split is None:
            split = self.scenario.get("framing") == "split"
        self.log("sent", message=message)
        self.write_raw(self.frame(message), split=split)

    # ---------------------------------------------------------------- behaviors
    def behavior(self, method: str) -> dict[str, Any]:
        behaviors = self.scenario.get("responses", {})
        configured = behaviors.get(method)
        if isinstance(configured, list):
            index = self.method_counts.get(method, 1) - 1
            configured = configured[min(index, len(configured) - 1)]
        return dict(configured or {})

    def substitute(self, value: Any, request: dict[str, Any]) -> Any:
        params = request.get("params") or {}
        request_uri = (params.get("textDocument") or {}).get("uri")
        if isinstance(value, str):
            if value == "$request_uri":
                return request_uri
            if value == "$request_position":
                return params.get("position")
            return value
        if isinstance(value, list):
            return [self.substitute(item, request) for item in value]
        if isinstance(value, dict):
            return {key: self.substitute(item, request) for key, item in value.items()}
        return value

    def wait_gate(self, gate: str | None) -> None:
        if not gate:
            return
        deadline = time.monotonic() + _GATE_LIMIT_SECONDS
        while not Path(gate).exists() and time.monotonic() < deadline:
            time.sleep(0.01)

    def spawn_grandchild(self) -> None:
        child = subprocess.Popen(  # noqa: S603 - fixed argv owned by the fake
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.log("grandchild", child_pid=child.pid)

    # --------------------------------------------------------------- handlers
    def initialize_result(self) -> dict[str, Any]:
        capabilities: dict[str, Any] = {
            "textDocumentSync": {"openClose": True, "change": 1, "save": {"includeText": False}},
            "definitionProvider": True,
            "referencesProvider": True,
            "hoverProvider": True,
        }
        capabilities.update(self.scenario.get("capabilities", {}))
        encoding = self.scenario.get("position_encoding", "utf-16")
        if encoding is not None:
            capabilities["positionEncoding"] = encoding
        return {
            "capabilities": capabilities,
            "serverInfo": {"name": "clangd", "version": "23.1.0-fake"},
        }

    def respond(self, request: dict[str, Any], behavior: dict[str, Any], result: Any) -> None:
        request_id = request["id"]
        method = request["method"]
        self.wait_gate(behavior.get("gate"))
        delay = float(behavior.get("delay", 0))
        if delay:
            time.sleep(delay)
        if behavior.get("malformed"):
            self.log("malformed", method=method)
            self.write_raw(b"Content-Length: not-a-number\r\n\r\n{garbage")
            return
        if "error" in behavior:
            message = {"jsonrpc": "2.0", "id": request_id, "error": behavior["error"]}
        else:
            message = {"jsonrpc": "2.0", "id": request_id, "result": result}
        if behavior.get("with_notification"):
            # Concatenate a notification and the reply in one write.
            note = self.substitute(behavior["with_notification"], request)
            self.log("sent", message=note)
            self.log("sent", message=message)
            self.write_raw(self.frame(note) + self.frame(message))
            return
        self.send(message, split=behavior.get("split"))

    def handle_request(self, request: dict[str, Any]) -> None:
        method = request["method"]
        with self.state_lock:
            self.method_counts[method] = self.method_counts.get(method, 0) + 1
        behavior = self.behavior(method)
        if behavior.get("stderr"):
            sys.stderr.write(str(behavior["stderr"]))
            sys.stderr.flush()
        if "exit" in behavior:
            delay = float(behavior.get("exit_delay", 0))
            if delay:
                time.sleep(delay)
            self.log("crash", method=method, code=behavior["exit"])
            sys.stderr.flush()
            os._exit(int(behavior["exit"]))
        if behavior.get("never"):
            self.log("ignored", method=method, id=request["id"])
            return
        if method == "initialize":
            result: Any = self.initialize_result()
        elif method == "shutdown":
            self.shutdown_requested = True
            if self.scenario.get("ignore_shutdown"):
                self.log("ignored", method=method, id=request["id"])
                return
            result = None
        else:
            result = self.substitute(behavior.get("result"), request)
        batch = behavior.get("batch")
        if batch:
            # Hold replies until ``batch`` requests arrived, then answer in
            # reverse order to prove out-of-order routing by id.
            with self.state_lock:
                held = self.held.setdefault(method, [])
                held.append({"request": request, "result": result})
                if len(held) < int(batch):
                    return
                release = list(reversed(held))
                held.clear()
            for item in release:
                self.respond(item["request"], {}, item["result"])
            return
        threading.Thread(target=self.respond, args=(request, behavior, result), daemon=True).start()

    def publish(self, uri: str, version: int | None, diagnostics: list[Any]) -> None:
        params: dict[str, Any] = {"uri": uri, "diagnostics": diagnostics}
        if version is not None:
            params["version"] = version
        self.send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": params})

    def diagnostics_for_sync(self, index: int) -> list[Any]:
        sequence = self.scenario.get("diagnostics_sequence")
        if sequence:
            return list(sequence[min(index, len(sequence) - 1)])
        return list(self.scenario.get("diagnostics", []))

    def after_sync(self, uri: str, version: int) -> None:
        with self.state_lock:
            index = self.sync_count
            self.sync_count += 1
        self.wait_gate(self.scenario.get("diagnostics_gate"))
        delay = float(self.scenario.get("diagnostics_delay", 0))
        if delay:
            time.sleep(delay)
        self.reload()
        mode = self.scenario.get("diagnostics_mode", "versioned")
        diagnostics = self.diagnostics_for_sync(index)
        if mode == "versioned":
            self.publish(uri, version, diagnostics)
        elif mode == "unversioned":
            self.publish(uri, None, diagnostics)
        elif mode == "stale":
            self.publish(uri, version - 1 if version > 1 else 0, diagnostics)
        elif mode == "wrong_uri":
            self.publish(uri + ".other.c", version, diagnostics)
        elif mode == "future":
            self.publish(uri, version + 1000, diagnostics)
        elif mode == "silent":
            return
        else:
            raise ValueError(f"unknown diagnostics_mode {mode!r}")

    def handle_notification(self, message: dict[str, Any]) -> None:
        method = message["method"]
        params = message.get("params") or {}
        if method == "initialized":
            for request in self.scenario.get("server_requests", []):
                self.send({"jsonrpc": "2.0", **request})
            if self.scenario.get("spawn_grandchild"):
                self.spawn_grandchild()
            if self.scenario.get("exit_after_initialized") is not None:
                self.log("crash", method=method, code=self.scenario["exit_after_initialized"])
                os._exit(int(self.scenario["exit_after_initialized"]))
        elif method == "textDocument/didOpen":
            document = params["textDocument"]
            self.documents[document["uri"]] = {
                "version": document["version"],
                "text": document["text"],
            }
            threading.Thread(
                target=self.after_sync, args=(document["uri"], document["version"]), daemon=True
            ).start()
        elif method == "textDocument/didChange":
            document = params["textDocument"]
            changes = params.get("contentChanges") or []
            text = changes[-1].get("text") if changes else None
            self.documents[document["uri"]] = {"version": document["version"], "text": text}
            threading.Thread(
                target=self.after_sync, args=(document["uri"], document["version"]), daemon=True
            ).start()
        elif method == "textDocument/didClose":
            self.documents.pop(params["textDocument"]["uri"], None)
        elif method == "exit":
            self.log("exit", shutdown_requested=self.shutdown_requested)
            if not self.scenario.get("ignore_exit"):
                os._exit(0 if self.shutdown_requested else 1)

    def run(self) -> int:
        self.log("start", argv=self.argv, cwd=os.getcwd())
        if self.scenario.get("startup_stderr"):
            sys.stderr.write(str(self.scenario["startup_stderr"]))
            sys.stderr.flush()
        while True:
            message = self.read_message()
            if message is None:
                self.log("eof")
                return 0
            self.log("received", message=message)
            self.reload()
            if "method" in message and "id" in message:
                self.handle_request(message)
            elif "method" in message:
                self.handle_notification(message)
            else:
                self.log("client_response", message=message)


def _parse_argv(argv: list[str]) -> tuple[Path | None, list[str]]:
    scenario: Path | None = None
    rest: list[str] = []
    index = 0
    while index < len(argv):
        if argv[index] == "--scenario" and index + 1 < len(argv):
            scenario = Path(argv[index + 1])
            index += 2
            continue
        rest.append(argv[index])
        index += 1
    return scenario, rest


def main(argv: list[str]) -> int:
    scenario_path, clangd_argv = _parse_argv(argv)
    if scenario_path is None:
        sys.stderr.write("fake_clangd requires --scenario PATH\n")
        return 2
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    if "--version" in clangd_argv:
        version = scenario.get("version", {})
        log_path = Path(scenario["log"])
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"event": "version_probe", "pid": os.getpid(), "argv": clangd_argv})
                + "\n"
            )
        if version.get("sleep"):
            time.sleep(float(version["sleep"]))
        sys.stdout.write(version.get("banner", FAKE_VERSION_BANNER) + "\n")
        sys.stdout.flush()
        return int(version.get("exit", 0))
    return FakeClangd(scenario, clangd_argv, scenario_path).run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
