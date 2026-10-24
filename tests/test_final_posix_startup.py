"""Final native M3 startup/EOF and stale birth-identity controls."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.kernel import hygiene, processes


@unittest.skipUnless(os.name == "posix", "native POSIX process/session identity required")
class FinalPosixStartup(unittest.TestCase):
    def child(self):
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            start_new_session=True,
        )

        def finish():
            if child.stdin and not child.stdin.closed:
                child.stdin.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Popen still anchors our exact child, never an enumerated PID.
                child.kill()
                child.wait(timeout=5)

        self.addCleanup(finish)
        token = processes._start_token(child.pid)
        self.assertIsNotNone(token)
        self.assertEqual(os.getpgid(child.pid), child.pid)
        return child, token

    def test_real_worker_eof_reap_and_empty_group_allow_actual_startup_cleanup(self):
        child, token = self.child()
        child.stdin.close()
        self.assertEqual(child.wait(timeout=5), 0)
        with self.assertRaises(ProcessLookupError):
            os.killpg(child.pid, 0)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "final-stale.json"
            marker = processes.ProcessMarker(
                2,
                "final-stale",
                child.pid,
                token,
                child.pid,
                token,
                "a" * 64,
                sys.executable,
                "2026-10-24T00:00:00Z",
            )
            path.write_text(json.dumps(asdict(marker)), encoding="utf-8")
            result = hygiene.require_clean_startup(path.parent)
            self.assertEqual(result.stale_removed, 1)
            self.assertEqual(result.unresolved, 0)
            self.assertFalse(path.exists())

    def test_live_nonempty_group_with_stale_birth_receives_no_signal(self):
        child, token = self.child()
        signals = []
        actual = os.killpg

        def observed(pgid, sig):
            signals.append((pgid, sig))
            return actual(pgid, sig)

        with patch.object(processes.os, "killpg", side_effect=observed):
            self.assertFalse(processes.terminate_marked_group(child.pid, token + ":stale"))
        self.assertFalse(any(sig in (signal.SIGTERM, signal.SIGKILL) for _, sig in signals))
        self.assertIsNone(child.poll())
        child.stdin.close()
        self.assertEqual(child.wait(timeout=5), 0)

    def test_actual_source_startup_removes_empty_marker_and_finishes_protocol_eof(self):
        child, token = self.child()
        child.stdin.close()
        self.assertEqual(child.wait(timeout=5), 0)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "project").mkdir()
            runs = root / "runs"
            marker = runs / "owned-processes/final-stale.json"
            marker.parent.mkdir(parents=True)
            marker.write_text(
                json.dumps(
                    asdict(
                        processes.ProcessMarker(
                            2,
                            "final-startup",
                            child.pid,
                            token,
                            child.pid,
                            token,
                            "b" * 64,
                            sys.executable,
                            "2026-10-24T00:00:00Z",
                        )
                    )
                ),
                encoding="utf-8",
            )
            command = [sys.executable, "-B", str(Path(__file__).with_name("final_source_stdio.py"))]
            environment = dict(
                os.environ,
                PYOCD_MCP_RUNS_ROOT=str(runs),
                BYO_MCP_MONITOR_ROOT=str(root / "monitor"),
                BYO_MCP_ARTIFACT_ROOT=str(root / "project"),
            )
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "final-native-posix-startup", "version": "1"},
                },
            }
            notification = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
            diagnostics = root / "server-stderr.log"
            with diagnostics.open("w", encoding="utf-8") as errors:
                process = subprocess.Popen(
                    command,
                    env=environment,
                    text=True,
                    encoding="utf-8",
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                )
                try:
                    process.stdin.write(json.dumps(request) + "\n")
                    process.stdin.flush()
                    self.assertTrue(
                        select.select([process.stdout], [], [], 25)[0], "initialization timeout"
                    )
                    first = process.stdout.readline()
                    self.assertEqual(json.loads(first).get("id"), 1)
                    output, _ = process.communicate(json.dumps(notification) + "\n", timeout=25)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    for stream in (process.stdin, process.stdout):
                        if stream is not None and not stream.closed:
                            stream.close()
            completed = subprocess.CompletedProcess(
                command, process.returncode, first + output, diagnostics.read_text(encoding="utf-8")
            )
            evidence = os.environ.get("BYO_TEST_STDIO_IDENTITIES")
            if evidence:
                output = Path(evidence)
                output.mkdir(parents=True, exist_ok=True)
                (output / "native-startup-wire.json").write_text(
                    json.dumps(
                        {
                            "argv": command,
                            "exit": completed.returncode,
                            "stdout": completed.stdout,
                            "stderr": completed.stderr,
                            "runs_root": str(runs),
                            "marker_removed": not marker.exists(),
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(marker.exists())
            messages = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
            self.assertTrue(all(item.get("jsonrpc") == "2.0" for item in messages))
            self.assertTrue(any(item.get("id") == 1 and "result" in item for item in messages))


if __name__ == "__main__":
    unittest.main()
