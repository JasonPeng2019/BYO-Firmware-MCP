"""Owned clangd lifecycle: lazy start, crashes, wedged shutdown and stdio EOF.

Contract "Adapter/service interface and lifecycle" items 1, 2, 8 and 10: one
lazily started owned child, every waiter completed after a crash with no retry
loop, a bounded idempotent shutdown (LSP shutdown/exit inside the 5 s grace plus
the 1.5 s owned-group reserve) reached from the real stdio server's EOF path,
and MCP stdout carrying only JSON-RPC.  The fake peer runs through the real
adapter's owned-process path; the stdio cases run the real server process.
"""

from __future__ import annotations

import json
import time
import unittest

from tests.code_analysis_support import (
    ARGUMENT_TIMEOUT_GRACE_SECONDS,
    OWNED_CLEANUP_SECONDS,
    SCHEDULING_SLACK_SECONDS,
    AnalysisStdioServer,
    Call,
    FakeClangd,
    FakeServiceCase,
    assert_envelope,
    assert_failure,
    file_uri,
    raw_range,
    simple_project,
    temporary_directory,
    wait_pids_gone,
)

SHUTDOWN_BOUND = ARGUMENT_TIMEOUT_GRACE_SECONDS + OWNED_CLEANUP_SECONDS + SCHEDULING_SLACK_SECONDS
DEFINITION = {"result": {"uri": "$request_uri", "range": raw_range((2, 4), (2, 10))}}
CRASH_MARKER = "FAKE-CLANGD-CRASH-MARKER"


class LazySingleChild(FakeServiceCase):
    def test_status_never_starts_and_queries_share_one_child(self) -> None:
        assert_envelope(self, self.service.status(), "status")
        self.assertEqual(self.fake.starts(), [], "status must not start clangd")
        for _ in range(2):
            assert_envelope(self, self.definition(), "definition", status="ok")
        line, column = self.position_of("helper")
        self.service.references(
            file_path=str(self.source()), line=line, column=column, timeout_seconds=10.0
        )
        self.service.hover(
            file_path=str(self.source()), line=line, column=column, timeout_seconds=10.0
        )
        self.assertEqual(len(self.fake.starts()), 1, "one lazily started child per configuration")
        ids = [
            message["id"]
            for message in self.fake.received()
            if "id" in message and "method" in message
        ]
        self.assertEqual(len(ids), len(set(ids)), f"request ids reused: {ids}")
        status = assert_envelope(self, self.service.status(), "status")
        self.assertEqual(status["data"]["session_state"], "ready")


class IdempotentShutdown(FakeServiceCase):
    def scenario(self) -> dict:
        return {"responses": {"textDocument/definition": DEFINITION}}

    def test_shutdown_twice_closes_session_and_releases_the_child(self) -> None:
        assert_envelope(self, self.definition(), "definition", status="ok")
        pids = self.fake.pids()
        started = time.monotonic()
        self.service.shutdown()
        self.service.shutdown()
        self.assertLess(time.monotonic() - started, SHUTDOWN_BOUND)
        self.assertTrue(self.fake.received("shutdown"), "LSP shutdown request expected")
        self.assertTrue(self.fake.received("exit"), "LSP exit notification expected")
        self.assertEqual(len(self.fake.received("shutdown")), 1, "shutdown must be idempotent")
        self.assertEqual(wait_pids_gone(pids, SHUTDOWN_BOUND), [])
        status = assert_envelope(self, self.service.status(), "status")
        self.assertEqual(status["data"]["session_state"], "closed")

    def test_queries_after_shutdown_do_not_start_a_child(self) -> None:
        self.service.shutdown()
        envelope = assert_envelope(self, self.definition(), "definition")
        self.assertNotIn(envelope["status"], {"ok", "not_found"}, envelope)
        self.assertEqual(self.fake.starts(), [])

    def test_wedged_peer_shutdown_is_bounded_and_terminates_the_group(self) -> None:
        self.fake.configure(ignore_shutdown=True, ignore_exit=True, spawn_grandchild=True)
        assert_envelope(self, self.definition(), "definition", status="ok")
        self.fake.wait_for(lambda: self.fake.events("grandchild"), message="grandchild spawn")
        pids = self.fake.pids()
        self.assertEqual(len(pids), 2, pids)
        started = time.monotonic()
        self.service.shutdown()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, SHUTDOWN_BOUND, f"wedged shutdown took {elapsed:.2f}s")
        self.assertEqual(wait_pids_gone(pids, OWNED_CLEANUP_SECONDS + SCHEDULING_SLACK_SECONDS), [])
        status = assert_envelope(self, self.service.status(), "status")
        self.assertEqual(status["data"]["session_state"], "closed")


class CrashHandling(FakeServiceCase):
    def test_crash_completes_every_pending_waiter_without_retry(self) -> None:
        self.fake.configure(
            responses={
                "textDocument/hover": {"never": True},
                "textDocument/references": {
                    "exit": 3,
                    "exit_delay": 0.3,
                    "stderr": CRASH_MARKER + "\n",
                },
            }
        )
        line, column = self.position_of("helper")
        arguments = {
            "file_path": str(self.source()),
            "line": line,
            "column": column,
            "timeout_seconds": 20.0,
        }
        hover = Call(self.service.hover, **arguments)
        self.fake.wait_received("textDocument/hover")
        references = Call(self.service.references, **arguments)
        crashed = references.join(10)
        waiter = hover.join(10)
        assert_failure(self, crashed, "references", "error", "analysis/child-exited")
        self.assertIn(
            CRASH_MARKER, json.dumps(crashed, ensure_ascii=False), "bounded stderr tail expected"
        )
        assert_envelope(self, waiter, "hover", status="error")
        self.assertLess(hover.elapsed, 10.0, "a pending waiter was left until its own deadline")
        self.assertEqual(
            len(self.fake.starts()), 1, "a crash must not be hidden by an automatic retry"
        )
        status = assert_envelope(self, self.service.status(), "status")
        self.assertEqual(status["data"]["session_state"], "failed")

        self.fake.configure(responses={"textDocument/definition": DEFINITION})
        assert_envelope(self, self.definition(), "definition", status="ok")
        self.assertEqual(len(self.fake.starts()), 2, "the next independent query starts fresh")

    def test_exit_during_initialize_is_child_exited(self) -> None:
        self.fake.configure(responses={"initialize": {"exit": 1, "stderr": CRASH_MARKER}})
        call = Call(self.definition)
        envelope = call.join(15)
        assert_failure(self, envelope, "definition", "error", "analysis/child-exited")
        self.assertLess(call.elapsed, 10.0)
        self.assertEqual(self.fake.received("initialized"), [])

    def test_exit_after_initialized_is_child_exited_not_a_hang(self) -> None:
        self.fake.configure(exit_after_initialized=0)
        call = Call(self.definition)
        envelope = call.join(15)
        assert_failure(
            self,
            envelope,
            "definition",
            "error",
            {"analysis/child-exited", "analysis/protocol-error"},
        )
        self.assertLess(call.elapsed, 10.0)

    def test_large_stderr_is_drained_and_the_reported_tail_is_bounded(self) -> None:
        flood = "x" * 1_048_576 + "\n"
        self.fake.configure(
            startup_stderr=flood,
            responses={
                "textDocument/definition": [
                    DEFINITION,
                    {"exit": 2, "stderr": "\n" + CRASH_MARKER + "-TAIL\n"},
                ]
            },
        )
        assert_envelope(self, self.definition(), "definition", status="ok")
        crashed = assert_failure(
            self, self.definition(), "definition", "error", "analysis/child-exited"
        )
        serialized = json.dumps(crashed, ensure_ascii=False)
        self.assertIn(CRASH_MARKER + "-TAIL", serialized, "the tail must keep the latest stderr")
        self.assertLess(len(serialized), 256 * 1024, "stderr tail must be bounded")


class StdioServerLifecycle(unittest.TestCase):
    """Real server subprocess; harness program installed through the gated seam."""

    def setUp(self) -> None:
        directory = temporary_directory("byo-ca-stdio-")
        self.base = directory.__enter__()
        self.addCleanup(directory.__exit__, None, None, None)
        self.project = simple_project(self.base / "project")
        self.fake = FakeClangd(
            self.base / "fake", {"responses": {"textDocument/definition": DEFINITION}}
        )

    def start(self) -> AnalysisStdioServer:
        server = AnalysisStdioServer(self.base / "server", self.project.root, fake=self.fake)
        self.addCleanup(server.kill)
        server.initialize()
        return server

    def definition_arguments(self, **overrides: object) -> dict:
        arguments = {
            "file_path": str(self.project.path("src/main.c")),
            "line": 5,
            "column": 25,
            "timeout_seconds": 10.0,
        }
        arguments.update(overrides)
        return arguments

    def assert_clean_exit(self, server: AnalysisStdioServer, pids: list[int]) -> None:
        code, elapsed = server.close_stdin_and_wait(timeout=60)
        diagnostics = server.stderr_text()[-4000:]
        self.assertEqual(code, 0, diagnostics)
        self.assertLess(
            elapsed, SHUTDOWN_BOUND, f"stdio EOF exit took {elapsed:.2f}s\n{diagnostics}"
        )
        self.assertEqual(
            wait_pids_gone(pids, SCHEDULING_SLACK_SECONDS), [], "owned clangd outlived the server"
        )
        self.assertEqual(
            server.markers(), [], "owned-process markers left after confirmed termination"
        )
        for line in server.stdout_lines:
            if line.strip():
                self.assertEqual(json.loads(line).get("jsonrpc"), "2.0", line)

    def test_status_over_stdio_starts_no_child(self) -> None:
        server = self.start()
        envelope = server.call_envelope("get_code_analysis_status", {})
        assert_envelope(self, envelope, "status", status="ok")
        self.assertEqual(envelope["data"]["session_state"], "not_started")
        self.assertEqual(self.fake.starts(), [])
        self.assert_clean_exit(server, [])

    def test_stdin_eof_shuts_down_the_owned_child_and_its_markers(self) -> None:
        server = self.start()
        envelope = server.call_envelope("find_code_definition", self.definition_arguments())
        assert_envelope(self, envelope, "definition", status="ok")
        pids = self.fake.pids()
        self.assertEqual(len(pids), 1)
        self.assertTrue(server.markers(), "the persistent child must be an owned, marked process")
        self.assert_clean_exit(server, pids)
        self.assertEqual(len(self.fake.received("shutdown")), 1)
        self.assertEqual(len(self.fake.received("exit")), 1)

    def test_stdin_eof_with_a_wedged_peer_is_bounded(self) -> None:
        self.fake.configure(ignore_shutdown=True, ignore_exit=True, spawn_grandchild=True)
        server = self.start()
        envelope = server.call_envelope("find_code_definition", self.definition_arguments())
        assert_envelope(self, envelope, "definition", status="ok")
        self.fake.wait_for(lambda: self.fake.events("grandchild"), message="grandchild spawn")
        self.assert_clean_exit(server, self.fake.pids())

    def test_peer_stderr_never_reaches_mcp_stdout(self) -> None:
        self.fake.configure(startup_stderr="Content-Length: 12\r\n\r\nnot-json-rpc\n" * 50)
        server = self.start()
        envelope = server.call_envelope("find_code_definition", self.definition_arguments())
        assert_envelope(self, envelope, "definition", status="ok")
        self.assert_clean_exit(server, self.fake.pids())
        self.assertFalse(
            any(b"not-json-rpc" in line for line in server.stdout_lines),
            "child stderr leaked to MCP stdout",
        )

    def test_mcp_cancellation_cancels_the_pending_lsp_request(self) -> None:
        self.fake.configure(responses={"textDocument/definition": {"never": True}})
        server = self.start()
        request_id = server.request(
            "tools/call",
            {
                "name": "find_code_definition",
                "arguments": self.definition_arguments(timeout_seconds=30.0),
            },
        )
        (pending,) = self.fake.wait_received("textDocument/definition")
        server.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": request_id, "reason": "test cancellation"},
            }
        )
        cancels = self.fake.wait_received("$/cancelRequest", timeout=10.0)
        self.assertIn(pending["id"], [message["params"]["id"] for message in cancels])
        status = server.call_envelope("get_code_analysis_status", {})
        assert_envelope(self, status, "status")
        self.assert_clean_exit(server, self.fake.pids())

    def test_uri_of_the_opened_document_is_the_canonical_saved_file(self) -> None:
        server = self.start()
        server.call_envelope("find_code_definition", self.definition_arguments())
        (opened,) = self.fake.received("textDocument/didOpen")
        self.assertEqual(
            opened["params"]["textDocument"]["uri"], file_uri(self.project.path("src/main.c"))
        )
        self.assert_clean_exit(server, self.fake.pids())


if __name__ == "__main__":
    unittest.main()
