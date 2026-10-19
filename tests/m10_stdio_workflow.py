"""Record M10's actual stdio workflow in one fresh, owned server process.

Run with ``uv run --locked python -m tests.m10_stdio_workflow --evidence PATH``.
The child uses the production module entry point with a passive Python profiler.
No handler, cancellation shim, clock, transport, or reservation is substituted.
All wire lines, stderr, lifecycle observations and temporary project files remain
under PATH. This is a host/logical-board workflow, not physical hardware proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import runpy
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone

import psutil


def stamp():
    return {"utc": datetime.now(timezone.utc).isoformat(), "monotonic": time.perf_counter()}


def serve_observed(trace_path: Path) -> None:
    """Observe call/return and lock state without modifying any product object."""
    selected = {
        "__main__": {"main"},
        "pyocd_debug_mcp.tools.misc": {"wait", "_cooperative_sleep"},
        "pyocd_debug_mcp.kernel.registry": {"_call_tool_inner"},
        "pyocd_debug_mcp.kernel.operations": {
            "create", "mark_running", "mark_handler_started", "request_cancel",
            "cleanup", "finish", "run_synchronous",
        },
    }
    lock = threading.Lock()
    with trace_path.open("w", encoding="utf-8", buffering=1) as output:
        def observe(frame, event, arg):
            module = frame.f_globals.get("__name__", "")
            name = frame.f_code.co_name
            if event not in {"call", "return"} or name not in selected.get(module, ()):
                return
            values = frame.f_locals
            op = values.get("managed") or values.get("operation") or values.get("self")
            if name == "create" and event == "return":
                op = arg
            if not hasattr(op, "operation_id"):
                operations = sys.modules.get("pyocd_debug_mcp.kernel.operations")
                op = operations.current_operation() if operations else None
            row = {**stamp(), "pid": os.getpid(), "thread": threading.current_thread().name,
                   "module": module, "function": frame.f_code.co_qualname, "event": event,
                   "file": frame.f_code.co_filename}
            if op is not None:
                operations = sys.modules["pyocd_debug_mcp.kernel.operations"]
                manager = operations.operation_manager
                worker = manager._board_workers.get(op.board_id)
                reservation = manager._batch_reservations.get(op.board_id)
                row.update(operation_id=op.operation_id, request_id=op.request_id,
                           board=op.board_id, tool=op.tool_name, state=op.state.value,
                           done=op.done.is_set(), cancelled=op.cancellation_requested.is_set(),
                           resources_cleaned=op.resources._cleaned,
                           worker_locked=worker.locked() if worker else False,
                           reservation_locked=reservation.locked() if reservation else False)
            with lock:
                output.write(json.dumps(row) + "\n")

        threading.setprofile(observe)
        sys.setprofile(observe)
        try:
            runpy.run_module("pyocd_debug_mcp.server", run_name="__main__")
        finally:
            sys.setprofile(None)
            threading.setprofile(None)


class Wire:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()
        self.events = []
        self.received = queue.Queue()
        self.terminals = {}
        self.project = Path(tempfile.mkdtemp(prefix="project-", dir=root)).resolve()
        env = os.environ.copy()
        env.update(BYO_MCP_ARTIFACT_ROOT=str(self.project),
                   BYO_MCP_MONITOR_ROOT=str(root / "monitor"),
                   BYO_MANAGED_TEST_EVIDENCE=str(root / "managed-analysis"))
        env.pop("BYO_MCP_TEST_SEAMS", None)
        env.pop("BYO_MCP_TEST_FAULTS", None)
        command = [sys.executable, "-m", "tests.m10_stdio_workflow", "--serve-observed",
                   str(root / "handler-trace.jsonl")]
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                     cwd=Path(__file__).resolve().parents[1], env=env)
        self.identities = {}
        self.capture_processes()
        self.record("spawn", argv=command, pid=self.proc.pid, project=str(self.project),
                    environment={k: v for k, v in env.items() if k.startswith("BYO_")})
        self.readers = [threading.Thread(target=self.read, args=(stream, label), daemon=True)
                        for stream, label in ((self.proc.stdout, "stdout"),
                                              (self.proc.stderr, "stderr"))]
        for reader in self.readers:
            reader.start()

    def capture_processes(self):
        process = psutil.Process(self.proc.pid)
        for owned in [process, *process.children(recursive=True)]:
            self.identities[owned.pid] = owned.create_time()

    def record(self, kind, **extra):
        with self.lock:
            row = {"sequence": len(self.events), **stamp(), "kind": kind, **extra}
            self.events.append(row)
            with (self.root / "wire.jsonl").open("a", encoding="utf-8") as output:
                output.write(json.dumps(row) + "\n")
        return row

    def read(self, stream, kind):
        for raw in stream:
            row = self.record(kind, raw=raw)
            if kind == "stdout":
                self.received.put(row)
        self.record(kind + "-eof")

    def send(self, payload):
        self.record("send-begin", payload=payload)
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        return self.record("sent", payload=payload)

    def call(self, request, name, **arguments):
        return self.send({"jsonrpc": "2.0", "id": request, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}})

    def terminal(self, request, bound=60):
        end = time.monotonic() + bound
        while request not in self.terminals:
            row = self.received.get(timeout=max(0.001, end - time.monotonic()))
            response = json.loads(row["raw"])
            assert response["jsonrpc"] == "2.0", response
            if "id" in response and ("result" in response or "error" in response):
                assert response["id"] not in self.terminals, response
                self.terminals[response["id"]] = (row, response)
        self.capture_processes()
        return self.terminals[request]

    def close(self):
        self.record("close-stdin")
        self.proc.stdin.close()
        forced = False
        try:
            code = self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            forced = True
            self.record("cleanup-bound-exceeded", bound_seconds=60)
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], check=False)
            else:
                self.proc.kill()
            code = self.proc.wait(timeout=30)
        for reader in self.readers:
            reader.join(timeout=15)
            assert not reader.is_alive(), "owned pipe reader remained running"
        for stream in (self.proc.stdout, self.proc.stderr):
            stream.close()
        self.record("process-exit", exit_code=code, forced=forced)
        assert code == 0 and not forced, (code, forced)
        for pid, created in self.identities.items():
            try:
                assert psutil.Process(pid).create_time() != created, ("owned process remains", pid)
            except psutil.NoSuchProcess:
                pass
        self.record("owned-processes-absent", identities=self.identities)


def content(response):
    assert "result" in response and not response["result"].get("isError"), response
    return "\n".join(block.get("text", "") for block in response["result"]["content"])


def workflow(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=False)
    wire = Wire(root)
    result = {"outcome": "FAIL", "started": stamp(), "pid": wire.proc.pid}
    try:
        wire.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "m10-independent-validator", "version": "1"}}})
        assert "result" in wire.terminal(1)[1]
        wire.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        wire.call(2, "initialization_handshake", workspace_path=str(wire.project))
        handshake = content(wire.terminal(2)[1])
        assert "run_id:" in handshake and "supplied workspace_path was not" not in handshake
        wire.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
        tools = wire.terminal(3)[1]["result"]["tools"]
        wait_schema = next(tool for tool in tools if tool["name"] == "wait")
        assert wait_schema["inputSchema"]["properties"]["ms"]["maximum"] == 30_000
        waiting = wire.call(4, "wait", board_id="wait_cancel_case", ms=10_000)
        # Reproduce the historical ~200 ms client action, not a polling shim.
        time.sleep(max(0, waiting["monotonic"] + 0.2 - time.perf_counter()))
        cancelled = wire.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                               "params": {"requestId": 4, "reason": "M10 host wait cancellation"}})
        successor = wire.call(5, "wait", board_id="wait_cancel_case", ms=100)
        peer = wire.call(6, "wait", board_id="wait_peer_case", ms=25)
        # No response is awaited between cancellation and successor submission.
        first_row, first = wire.terminal(4)
        second_row, second = wire.terminal(5)
        peer_row, peer_response = wire.terminal(6)
        assert successor["sequence"] < first_row["sequence"], "successor was not sent before terminal"
        assert first.get("error", {}).get("message") == "Request cancelled", first
        assert "Waited 100 ms" in content(second)
        assert "Waited 25 ms" in content(peer_response)
        wire.call(7, "disconnect", board_id="wait_cancel_case")
        assert "Not connected." in content(wire.terminal(7)[1])
        result.update(outcome="WIRE_PASS_AWAITING_OWNERSHIP", cancel_after_seconds=cancelled["monotonic"]-waiting["monotonic"],
                      successor_send_after_cancel_seconds=successor["monotonic"]-cancelled["monotonic"],
                      cancelled_terminal_after_wait_seconds=first_row["monotonic"]-waiting["monotonic"],
                      successor_terminal_after_send_seconds=second_row["monotonic"]-successor["monotonic"],
                      peer_terminal_after_send_seconds=peer_row["monotonic"]-peer["monotonic"],
                      terminals={str(k): v[1] for k, v in wire.terminals.items()})
    except BaseException:
        result["exception"] = traceback.format_exc()
        raise
    finally:
        try:
            wire.close()
        except BaseException:
            result["outcome"] = "FAIL"
            result["cleanup_exception"] = traceback.format_exc()
            raise
        finally:
            result["finished"] = stamp()
            (root / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    trace = [json.loads(line) for line in (root / "handler-trace.jsonl").read_text().splitlines()]
    old = [row for row in trace if str(row.get("request_id")) == "4"]
    next_wait = [row for row in trace if str(row.get("request_id")) == "5"]
    exited = next(row for row in old if row["function"].endswith(".wait") and row["event"] == "return")
    cleared = next(row for row in old if row["function"] == "OperationManager.finish"
                   and row["event"] == "return")
    begun = next(row for row in next_wait if row["function"].endswith(".wait") and row["event"] == "call")
    assert exited["cancelled"] and cleared["done"] and cleared["resources_cleaned"]
    # The worker releases locks just before setting done/removing its operation;
    # a successor may legitimately enter before manager.finish returns. The real
    # handler and cleanup returns must precede successor handler entry.
    cleaned = next(row for row in old if row["function"] == "ManagedOperation.cleanup"
                   and row["event"] == "return")
    assert exited["monotonic"] <= cleaned["monotonic"] <= begun["monotonic"]
    assert all(row["monotonic"] <= begun["monotonic"] for row in old
               if row["module"] == "pyocd_debug_mcp.tools.misc")
    final = next(row for row in reversed(trace) if row["function"] == "OperationManager.finish"
                 and row["event"] == "return")
    assert not final["worker_locked"] and not final["reservation_locked"]
    records = [json.loads(line) for path in (root / "monitor").rglob("*.jsonl")
               for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    closes = [row for row in records if row.get("kind") == "close"]
    assert closes and closes[-1]["workspace"] != "unbound", "workspace did not bind/close"
    assert closes[-1]["detail"]["reason"] == "eof", closes[-1]
    result["monitor_close"] = closes[-1]
    result["outcome"] = "PASS"
    result["clock"] = "time.perf_counter (monotonic high resolution)"
    result["owned_processes_absent"] = wire.identities
    result["ownership"] = {"old_handler_exit": exited, "old_cleanup_return": cleaned,
                           "old_manager_finish": cleared, "successor_handler_enter": begun}
    result["source_files"] = {row["file"]: hashlib.sha256(Path(row["file"]).read_bytes()).hexdigest()
                              for row in trace}
    (root / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--serve-observed", type=Path)
    args = parser.parse_args()
    if args.serve_observed:
        serve_observed(args.serve_observed)
    else:
        if args.evidence is None:
            parser.error("--evidence is required")
        try:
            result = workflow(args.evidence.resolve())
        except BaseException:
            summary = args.evidence.resolve() / "summary.json"
            if summary.exists():
                failed = json.loads(summary.read_text(encoding="utf-8"))
                failed.update(outcome="FAIL", validation_exception=traceback.format_exc())
                summary.write_text(json.dumps(failed, indent=2), encoding="utf-8")
            raise
        print(json.dumps({k: v for k, v in result.items() if k not in {"terminals", "ownership"}}, indent=2))


if __name__ == "__main__":
    main()
