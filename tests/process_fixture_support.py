"""Shared process fixture support fixtures; no collected cases."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
import psutil
from pyocd_debug_mcp.kernel import processes


class _NativePosixCase(unittest.TestCase):
    """Real owned processes and an exact unrelated peer; Actions-only fixtures."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="fr13-native-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = processes.ProcessMarkerStore(self.root / "markers")
        self.created: list[tuple[subprocess.Popen[Any], Path | None]] = []
        self.created_births: dict[int, float] = {}
        self.descendants: list[tuple[int, float]] = []
        self.real_spawn = processes.popen_owned
        self.peer = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.peer_birth = psutil.Process(self.peer.pid).create_time()
        self.addCleanup(self.cleanup_native)

    @staticmethod
    def native_state(pid: int, birth: float) -> str:
        try:
            process = psutil.Process(pid)
            if process.create_time() != birth:
                return "reused"
            status = process.status()
            return "zombie" if status in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD} else "live"
        except psutil.ZombieProcess:
            return "unknown"
        except psutil.NoSuchProcess:
            return "gone"

    def capture_spawn(self, *args: Any, **kwargs: Any) -> tuple[subprocess.Popen[Any], Path | None]:
        result = self.real_spawn(*args, **kwargs)
        self.created.append(result)
        self.created_births[result[0].pid] = psutil.Process(result[0].pid).create_time()
        return result

    def owned(self, code: str) -> subprocess.Popen[Any]:
        process, _marker = self.capture_spawn(
            [sys.executable, "-c", code],
            marker_store=self.store,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return process

    def descendant_program(
        self,
        *,
        exit_code: int = 7,
        linger: bool = False,
        stdout: str = "owned-output",
        ignore_term: bool = False,
    ) -> str:
        receipt = self.root / "descendant.json"
        receipt.unlink(missing_ok=True)
        child = (
            "import json, os, signal, sys, time, psutil\n"
            "from pathlib import Path\n"
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
            + "p=Path(sys.argv[1]); t=p.with_suffix('.tmp')\n"
            "t.write_text(json.dumps({'pid':os.getpid(),"
            " 'birth':psutil.Process().create_time()}))\n"
            "os.replace(t,p)\n"
            "time.sleep(60)\n"
        )
        return (
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            f"receipt=Path({str(receipt)!r})\n"
            f"child=subprocess.Popen([sys.executable, '-c', {child!r}, str(receipt)],"
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "deadline=time.monotonic()+10\n"
            "while not receipt.exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
            "if not receipt.exists(): raise SystemExit(90)\n"
            f"print({stdout!r}, flush=True)\n"
            "print('owned-error', file=sys.stderr, flush=True)\n"
            + ("time.sleep(60)\n" if linger else f"raise SystemExit({exit_code})\n")
        )

    def child_identity(self) -> tuple[int, float]:
        receipt = self.root / "descendant.json"
        deadline = time.monotonic() + 10
        while not receipt.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(receipt.is_file(), "real descendant did not publish its identity")
        record = json.loads(receipt.read_text(encoding="utf-8"))
        identity = (record["pid"], record["birth"])
        if identity not in self.descendants:
            self.descendants.append(identity)
        return identity

    def assert_live_child(self, process: subprocess.Popen[Any]) -> tuple[int, float]:
        pid, birth = self.child_identity()
        self.assertEqual(self.native_state(pid, birth), "live")
        self.assertEqual(os.getpgid(pid), process.pid)
        self.assertEqual(os.getsid(pid), process.pid)
        self.assert_peer()
        return pid, birth

    def assert_peer(self) -> None:
        self.assertIsNone(self.peer.poll())
        self.assertEqual(self.native_state(self.peer.pid, self.peer_birth), "live")
        self.assertEqual(os.getpgid(self.peer.pid), self.peer.pid)
        self.assertEqual(os.getsid(self.peer.pid), self.peer.pid)

    def assert_reaped(self, process: subprocess.Popen[Any], status: int | None = None) -> None:
        self.assertIsNotNone(process.returncode)
        if status is not None:
            self.assertEqual(process.returncode, status)
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)

    def assert_child_terminal(self, identity: tuple[int, float]) -> None:
        self.assertIn(self.native_state(*identity), {"gone", "zombie"})
        self.assert_peer()

    def cleanup_native(self) -> None:
        # Fixture rescue is separate from every assertion. Fault patches have
        # ended; only owned groups and freshly matched exact children are used.
        for pid, birth in self.descendants:
            try:
                if self.native_state(pid, birth) == "live":
                    child = psutil.Process(pid)
                    if child.create_time() == birth:
                        child.kill()
            except psutil.NoSuchProcess:
                pass
        rescues = []
        for process, marker in reversed(self.created):
            try:
                confirmed = processes.terminate_process_group(process)
                if confirmed:
                    self.store.remove(marker)
                else:
                    birth = self.created_births[process.pid]
                    before = self.native_state(process.pid, birth)
                    rescue: dict[str, Any] = {
                        "pid": process.pid,
                        "birth": birth,
                        "before": before,
                        "product_cleanup_confirmed": False,
                        "marker_retained": marker is not None and marker.exists(),
                        "action": "none",
                        "scope": "fixture recovery after behavioral assertions",
                    }
                    if before == "live":
                        child = psutil.Process(process.pid)
                        if child.create_time() == birth:
                            child.kill()
                            rescue["action"] = "emergency exact PID/birth kill"
                    deadline = time.monotonic() + 5
                    while (
                        self.native_state(process.pid, birth) == "live"
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    if self.native_state(process.pid, birth) == "zombie":
                        pid, status = os.waitpid(process.pid, 0)
                        rescue["native_reap"] = {"pid": pid, "status": status}
                        process.returncode = os.waitstatus_to_exitcode(status)
                    rescue["after"] = self.native_state(process.pid, birth)
                    rescues.append(rescue)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        evidence = os.environ.get("BYO_FINAL_CLEANUP_EVIDENCE")
        if evidence:
            destination = Path(evidence)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / (self._testMethodName + ".fixture-recovery.json")).write_text(
                json.dumps({"test": self.id(), "recoveries": rescues}, indent=2) + "\n",
                encoding="utf-8",
            )
        if self.native_state(self.peer.pid, self.peer_birth) == "live":
            self.peer.terminate()
        self.peer.wait(timeout=5)


CLOCK = time.monotonic  # Never consume a test-owned mocked clock sequence.


def facts(pid: int, birth: float | None = None) -> dict:
    record = {"pid": pid, "expected_birth": birth, "at": CLOCK()}
    try:
        process = psutil.Process(pid)
        actual = process.create_time()
        record["birth"] = actual
        if birth is not None and actual != birth:
            return {**record, "identity": "reused", "state": "unknown"}
        record["identity"] = "observed"
        for key in ("ppid", "exe", "cmdline", "status"):
            try:
                record[key] = getattr(process, key)()
            except (psutil.Error, OSError, SystemError) as exc:
                record[key] = {"unknown": repr(exc)}
        record["state"] = (
            "terminal"
            if record.get("status") in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
            else "live"
            if isinstance(record.get("status"), str)
            else "unknown"
        )
        if os.name == "posix":
            for key, query in (("pgid", os.getpgid), ("sid", os.getsid)):
                try:
                    record[key] = query(pid)
                except OSError as exc:
                    record[key] = {"unavailable": repr(exc)}
        if psutil.Process(pid).create_time() != actual:
            record.update(identity="changed", state="unknown")
    except psutil.ZombieProcess as exc:
        record.update(identity="unknown", state="unknown", error=repr(exc))
    except psutil.NoSuchProcess:
        record.update(identity="gone", state="gone")
    except (psutil.Error, OSError, SystemError) as exc:
        record.update(identity="unknown", state="unknown", error=repr(exc))
    return record
