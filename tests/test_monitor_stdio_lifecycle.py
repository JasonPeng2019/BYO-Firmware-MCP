"""The real server process over stdio: framing, lifecycle, and recovery.

These spawn the actual console entry point and speak MCP to it, so they cover the
things an in-process test cannot: that stdout carries only protocol framing, that
the clean-EOF path writes a close record, and that content left undelivered by an
abrupt kill is picked up at the next boot.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Mapping
from pathlib import Path


SERVER_PROJECT = Path(__file__).resolve().parents[1]
MONITOR_ROOT_ENV = "BYO_MCP_MONITOR_ROOT"

_MONITOR_TEMPORARY: tempfile.TemporaryDirectory[str] | None = None
_MONITOR_ROOT: Path | None = None
_SHARED_STORE_FINGERPRINT: tuple[tuple[str, str], ...] | None = None


def setUpModule() -> None:
    global _MONITOR_TEMPORARY, _MONITOR_ROOT, _SHARED_STORE_FINGERPRINT
    _MONITOR_TEMPORARY = tempfile.TemporaryDirectory(prefix="byo-stdio-monitor-")
    _MONITOR_ROOT = Path(_MONITOR_TEMPORARY.name).resolve()
    _SHARED_STORE_FINGERPRINT = _shared_store_fingerprint()


def tearDownModule() -> None:
    """Assert the lifecycle module never altered the shared monitor store."""

    try:
        if _shared_store_fingerprint() != _SHARED_STORE_FINGERPRINT:
            raise AssertionError("stdio lifecycle tests changed the shared monitor store")
    finally:
        from pyocd_debug_mcp.monitor import paths

        paths._reset_cache(None)
        if _MONITOR_TEMPORARY is not None:
            _MONITOR_TEMPORARY.cleanup()


def _shared_store_root() -> Path:
    from platformdirs import user_data_dir

    return Path(user_data_dir("BYO", appauthor=False, roaming=False))


def _shared_store_fingerprint() -> tuple[tuple[str, str], ...] | None:
    """Return a content fingerprint without retaining shared-store contents."""

    root = _shared_store_root()
    if not root.exists():
        return None
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        try:
            if path.is_dir():
                entries.append((f"directory:{relative}", ""))
            elif path.is_file():
                entries.append((f"file:{relative}", hashlib.sha256(path.read_bytes()).hexdigest()))
        except OSError:
            entries.append((f"unreadable:{relative}", ""))
    return tuple(entries)


def monitor_root() -> Path:
    assert _MONITOR_ROOT is not None, "setUpModule did not initialize the monitor root"
    return _MONITOR_ROOT


class StdioServer:
    """A live server subprocess speaking JSON-RPC over stdio."""

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        extra_environment: Mapping[str, str] | None = None,
    ) -> None:
        environment = os.environ.copy()
        environment[MONITOR_ROOT_ENV] = str(monitor_root())
        if extra_environment is not None:
            environment.update(extra_environment)
        self.proc = subprocess.Popen(
            command
            or [sys.executable, "-B", str(Path(__file__).with_name("final_source_stdio.py"))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(SERVER_PROJECT),
            env=environment,
        )
        self.stdout_lines: list[str] = []
        self.stderr_lines: list[str] = []
        self._responses = queue.Queue()

        def read_stdout():
            try:
                for line in self.proc.stdout:
                    self._responses.put(line)
            finally:
                self._responses.put(None)

        def read_stderr():
            for line in self.proc.stderr:
                self.stderr_lines.append(line)

        # Drain both pipes from spawn; stderr must never block the protocol.
        self._readers = [
            threading.Thread(target=read_stdout, daemon=True),
            threading.Thread(target=read_stderr, daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def _diagnostics(self) -> str:
        return (
            f"command={self.proc.args!r}; exit={self.proc.poll()}; "
            f"stdout={self.stdout_lines!r}; stderr={''.join(self.stderr_lines)[-16384:]}"
        )

    def send(self, payload: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def read(self, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self._responses.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                self.kill()
                raise AssertionError("server response timed out; " + self._diagnostics()) from None
            if line is None:
                self.proc.wait(timeout=5)
                for reader in self._readers:
                    reader.join(timeout=1)
                raise AssertionError("server closed stdout unexpectedly; " + self._diagnostics())
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
        try:
            self.proc.stdin.close()
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            self.kill()
            error.add_note(self._diagnostics())
            raise
        finally:
            self._close_pipes()

    def kill(self) -> None:
        try:
            if self.proc.poll() is None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    self.proc.kill()
            self.proc.wait(timeout=30)
        finally:
            self._close_pipes()

    def _close_pipes(self) -> None:
        for reader in self._readers:
            reader.join(timeout=1)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is None or stream.closed:
                continue
            stream.close()

    def stderr_text(self) -> str:
        return "".join(self.stderr_lines)


class LifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Path(tempfile.mkdtemp(prefix="byo-lifecycle-proj-"))
        self.addCleanup(shutil.rmtree, self.project, True)
        from pyocd_debug_mcp.monitor import paths

        paths._reset_cache(monitor_root())
        self.workspace = paths.workspace_id(self.project)
        self.store = monitor_root()
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


class CleanShutdownWritesACloseRecord(LifecycleTestCase):
    def test_stdin_eof_exits_cleanly_and_records_close(self) -> None:
        server = StdioServer()
        self.addCleanup(server.kill)
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


class AbruptKillLosesNothingAlreadyAppended(LifecycleTestCase):
    def test_records_survive_a_hard_kill(self) -> None:
        """Durability came from the append, not from any shutdown path."""

        server = StdioServer()
        self.addCleanup(server.kill)
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
        self.addCleanup(first.kill)
        first.initialize()
        first.call("initialization_handshake", {"workspace_path": str(self.project)})
        first.kill()
        resident_before = list((self.store / "server_data" / self.workspace).glob("*.jsonl"))
        self.assertTrue(resident_before, "nothing was left behind to recover")

        second = StdioServer()
        self.addCleanup(second.kill)
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


if __name__ == "__main__":
    unittest.main()


class ProfessionalBuildOverTheProtocol(LifecycleTestCase):
    """A build cut without narrative logging, verified through a real session."""

    def setUp(self) -> None:
        super().setUp()
        prepared = os.environ.get("BYO_TEST_PROFESSIONAL_SRC")
        if prepared:
            source = Path(prepared).resolve()
        else:
            # Ordinary standalone suite use also leaves the reviewed source
            # untouched. The final Actions pass prepares and manifests this
            # exact one-line variant before its first command.
            temporary = self.enterContext(tempfile.TemporaryDirectory(prefix="byo-pro-profile-"))
            source = Path(temporary) / "src"
            shutil.copytree(
                SERVER_PROJECT / "src", source, ignore=shutil.ignore_patterns("__pycache__")
            )
            profile = source / "pyocd_debug_mcp/monitor/build_profile.py"
            original = profile.read_text(encoding="utf-8")
            self.assertEqual(original.count("NARRATIVE_LOGGING: bool = True"), 1)
            profile.write_text(
                original.replace(
                    "NARRATIVE_LOGGING: bool = True", "NARRATIVE_LOGGING: bool = False"
                ),
                encoding="utf-8",
            )
        self.assertIn(
            "NARRATIVE_LOGGING: bool = False",
            (source / "pyocd_debug_mcp/monitor/build_profile.py").read_text(encoding="utf-8"),
        )
        self.professional_environment = {"PYTHONPATH": str(source)}

    def test_professional_profile_reaches_graceful_protocol_eof(self) -> None:
        server = StdioServer(extra_environment=self.professional_environment)
        self.addCleanup(server.kill)
        server.initialize()
        response = server.call("server_health_check", {})
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["narrative_logging"], "not_built")
        self.assertEqual(server.close_stdin_and_wait(), 0)
