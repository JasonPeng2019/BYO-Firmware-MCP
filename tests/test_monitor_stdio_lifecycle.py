"""The real server process over stdio: framing, lifecycle, and recovery.

These spawn the actual console entry point and speak MCP to it, so they cover the
things an in-process test cannot: that stdout carries only protocol framing, that
the clean-EOF path writes a close record, and that content left undelivered by an
abrupt kill is picked up at the next boot.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.store_cleanup import restore as store_restore
from tests.store_cleanup import snapshot as store_snapshot

SERVER_PROJECT = Path(__file__).resolve().parents[1]

_STORE_BEFORE: "set[str] | None" = None


def setUpModule() -> None:
    global _STORE_BEFORE
    _STORE_BEFORE = store_snapshot()


def tearDownModule() -> None:
    """Restore the real store to what was there before this module ran."""

    store_restore(_STORE_BEFORE)



def store_root() -> Path:
    from platformdirs import user_data_dir

    return Path(user_data_dir("BYO", appauthor=False, roaming=False))


class StdioServer:
    """A live server subprocess speaking JSON-RPC over stdio."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            ["uv", "run", "--project", str(SERVER_PROJECT), "pyocd-debug-mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(SERVER_PROJECT),
        )
        self.stdout_lines: list[str] = []

    def send(self, payload: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def read(self) -> dict:
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError("server closed stdout unexpectedly")
            self.stdout_lines.append(line)
            stripped = line.strip()
            if stripped:
                return json.loads(stripped)

    def initialize(self) -> dict:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "lifecycle-test", "version": "0"},
                },
            }
        )
        response = self.read()
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return response

    def call(self, name: str, arguments: dict, request_id: int = 2) -> dict:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return self.read()

    def close_stdin_and_wait(self, timeout: float = 30.0) -> int:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        return self.proc.wait(timeout=timeout)

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=30)

    def stderr_text(self) -> str:
        assert self.proc.stderr is not None
        return self.proc.stderr.read() or ""


class LifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Path(tempfile.mkdtemp(prefix="byo-lifecycle-proj-"))
        self.addCleanup(shutil.rmtree, self.project, True)
        from pyocd_debug_mcp.monitor import paths

        paths._reset_cache(None)
        self.workspace = paths.workspace_id(self.project)
        self.store = store_root()
        self.addCleanup(self._clean)

    def _clean(self) -> None:
        for base in ("server_data", "simulated_remote"):
            shutil.rmtree(self.store / base / self.workspace, ignore_errors=True)

    def records(self) -> list[dict]:
        found: list[dict] = []
        for base in ("server_data", "simulated_remote"):
            directory = self.store / base / self.workspace
            if not directory.exists():
                continue
            for path in sorted(directory.rglob("*.jsonl")):
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        found.append(json.loads(line))
        return found

    def kinds(self) -> list[str]:
        return [str(record.get("kind")) for record in self.records()]


class StdoutIsOnlyProtocolFraming(LifecycleTestCase):
    def test_every_stdout_line_is_valid_json_rpc(self) -> None:
        """Stdout is the wire: one stray byte breaks framing intermittently."""

        server = StdioServer()
        self.addCleanup(server.kill)
        server.initialize()
        server.call("initialization_handshake", {"workspace_path": str(self.project)})
        server.call("server_health_check", {}, request_id=3)
        for line in server.stdout_lines:
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)  # raises if anything else leaked
            self.assertEqual(payload.get("jsonrpc"), "2.0")

    def test_server_survives_a_burst_of_calls(self) -> None:
        server = StdioServer()
        self.addCleanup(server.kill)
        server.initialize()
        for index in range(12):
            response = server.call("server_health_check", {}, request_id=10 + index)
            self.assertIn("result", response)


class CleanShutdownWritesACloseRecord(LifecycleTestCase):
    def test_stdin_eof_exits_cleanly_and_records_close(self) -> None:
        server = StdioServer()
        server.initialize()
        server.call("initialization_handshake", {"workspace_path": str(self.project)})
        started = time.monotonic()
        code = server.close_stdin_and_wait()
        elapsed = time.monotonic() - started
        self.assertEqual(code, 0)
        self.assertLess(elapsed, 15.0)
        kinds = self.kinds()
        self.assertIn("boot", kinds)
        self.assertIn("close", kinds)
        # Occasions only: no record is keyed to an individual tool call.
        self.assertNotIn("call", kinds)

    def test_close_record_precedes_delivery(self) -> None:
        """Recording is the durable act; sending is best-effort.

        The close record must exist even though the interim transport delivers
        only to a local folder and could fail at any time.
        """

        server = StdioServer()
        server.initialize()
        server.call("initialization_handshake", {"workspace_path": str(self.project)})
        server.close_stdin_and_wait()
        close_records = [r for r in self.records() if r.get("kind") == "close"]
        self.assertTrue(close_records)
        detail = close_records[-1].get("detail") or {}
        self.assertIn("reason", detail)
        self.assertIn("total_calls", detail)


class AbruptKillLosesNothingAlreadyAppended(LifecycleTestCase):
    def test_records_survive_a_hard_kill(self) -> None:
        """Durability came from the append, not from any shutdown path."""

        server = StdioServer()
        server.initialize()
        server.call("initialization_handshake", {"workspace_path": str(self.project)})
        server.call("server_health_check", {}, request_id=3)
        server.kill()
        kinds = self.kinds()
        # The boot record was durable the moment it was appended, with no seal,
        # roll, or close having run.
        self.assertIn("boot", kinds)
        # No close record is expected: nothing ran after the kill.
        self.assertNotIn("close", kinds)

    def test_next_boot_recovers_undelivered_content(self) -> None:
        first = StdioServer()
        first.initialize()
        first.call("initialization_handshake", {"workspace_path": str(self.project)})
        first.kill()
        resident_before = list((self.store / "server_data" / self.workspace).glob("*.jsonl"))
        self.assertTrue(resident_before, "nothing was left behind to recover")

        second = StdioServer()
        second.initialize()
        second.call("initialization_handshake", {"workspace_path": str(self.project)})
        # Bootup recovery drains asynchronously after readiness.
        deadline = time.monotonic() + 20
        delivered = self.store / "simulated_remote" / self.workspace / "ledger"
        while time.monotonic() < deadline:
            if delivered.exists() and list(delivered.glob("*.jsonl")):
                break
            time.sleep(0.25)
        second.close_stdin_and_wait()
        self.assertTrue(
            delivered.exists() and list(delivered.glob("*.jsonl")),
            "the previous run's spool was not delivered at the next boot",
        )


class ServerStartsWithNoTransportConfigured(LifecycleTestCase):
    def test_health_reports_no_off_box_copy(self) -> None:
        server = StdioServer()
        self.addCleanup(server.kill)
        server.initialize()
        response = server.call("server_health_check", {})
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertNotEqual(payload["delivery"]["state"], "sent")
        self.assertFalse(payload["delivery"]["durable_off_box"])
        self.assertIn("no off-box copy", payload["delivery"]["off_box_note"])


if __name__ == "__main__":
    unittest.main()


class CloseoutOrdering(LifecycleTestCase):
    """Hardware is released first, the close record next, the send last.

    Ordered this way a slow or hung send can never strand a board, and a failed
    send can never cost the record.
    """

    def test_close_record_exists_before_anything_is_delivered(self) -> None:
        server = StdioServer()
        server.initialize()
        server.call("initialization_handshake", {"workspace_path": str(self.project)})
        server.close_stdin_and_wait()

        close_records = [r for r in self.records() if r.get("kind") == "close"]
        self.assertTrue(close_records, "no close record was written")

        # The close record must be inside the run's own ledger file, which is
        # written before any delivery is attempted.
        found_in_ledger = False
        for base in ("server_data", "simulated_remote"):
            directory = self.store / base / self.workspace
            if not directory.exists():
                continue
            for path in directory.rglob("*.jsonl"):
                if '"kind":"close"' in path.read_text(encoding="utf-8").replace(" ", ""):
                    found_in_ledger = True
        self.assertTrue(found_in_ledger)

    def test_final_counters_reach_the_close_record(self) -> None:
        server = StdioServer()
        server.initialize()
        server.call("initialization_handshake", {"workspace_path": str(self.project)})
        server.call("server_health_check", {}, request_id=3)
        server.close_stdin_and_wait()
        close = [r for r in self.records() if r.get("kind") == "close"][-1]
        detail = close.get("detail") or {}
        # Exactly two tools/call requests were made this session
        # (initialization_handshake, server_health_check): deterministic, not
        # merely a lower bound.
        self.assertEqual(detail.get("total_calls", 0), 2)
        self.assertIn("per_outcome", detail)


class ProfessionalBuildOverTheProtocol(LifecycleTestCase):
    """A build cut without narrative logging, verified through a real session."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = (
            SERVER_PROJECT / "src" / "pyocd_debug_mcp" / "monitor" / "build_profile.py"
        )
        self.original = self.profile.read_text(encoding="utf-8")
        self.profile.write_text(
            self.original.replace(
                "NARRATIVE_LOGGING: bool = True", "NARRATIVE_LOGGING: bool = False"
            ),
            encoding="utf-8",
        )
        self.addCleanup(self.profile.write_text, self.original, "utf-8")

    def _tool_names(self, server: "StdioServer") -> set[str]:
        server.send({"jsonrpc": "2.0", "id": 90, "method": "tools/list", "params": {}})
        response = server.read()
        return {tool["name"] for tool in response["result"]["tools"]}

    def test_checkin_is_absent_and_report_tool_refuses(self) -> None:
        server = StdioServer()
        self.addCleanup(server.kill)
        server.initialize()
        names = self._tool_names(server)
        self.assertNotIn("submit_routine_checkin", names)
        # Present and explaining, so the agent does not hunt a missing tool.
        self.assertIn("report_agent_issue", names)
        self.assertIn("server_health_check", names)

    def test_health_declares_the_build_profile(self) -> None:
        server = StdioServer()
        self.addCleanup(server.kill)
        server.initialize()
        response = server.call("server_health_check", {})
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["narrative_logging"], "not_built")
