"""Independent caller and released-identity controls derived from FR13 contracts.

Native positive controls use real processes and the unchanged public caller.
The permission and observation controls replace only their stated fault input.
No production timing, return decoder or membership classification is replaced.
"""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from pyocd_debug_mcp.kernel import processes
from tests.test_process_callers import facts

CLOCK = time.monotonic
FIXTURE = Path(__file__).with_name("process_release_fixture.py")
ARTIFACT = Path(os.environ.get("BYO_TEST_FAILURES", tempfile.gettempdir())) / "process-callers"


class ValidatorCallerContracts(unittest.TestCase):
    def setUp(self) -> None:
        ARTIFACT.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="independent-02-", dir=ARTIFACT))
        self.store = processes.ProcessMarkerStore(self.root / "markers")
        self.owned: list[tuple[subprocess.Popen, Path]] = []
        self.events = []
        self.real_spawn = processes.popen_owned
        self.peer = subprocess.Popen(
            [sys.executable, str(FIXTURE), "peer", str(self.root)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
        self.addCleanup(self.emergency)
        self.peer_receipt = self.receipt("peer")

    def note(self, boundary: str, **values) -> None:
        self.events.append({"boundary": boundary, "at": CLOCK(), **values})

    def receipt(self, role: str) -> dict:
        path = self.root / (role + ".json")
        deadline = CLOCK() + 10
        while CLOCK() < deadline:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.002)
        self.fail("fixture identity/readiness gap: " + role)

    def current(self, role: str) -> dict:
        identity = self.receipt(role)
        return {"role": role, **facts(identity["pid"], identity["birth"])}

    def peer_preserved(self) -> None:
        current = self.current("peer")
        self.note("unrelated exact peer preservation", facts=current)
        self.assertEqual(current["identity"], "observed", current)
        self.assertEqual(current["birth"], self.peer_receipt["birth"])
        self.assertEqual(current["state"], "live", current)
        self.assertIsNone(self.peer.poll())
        if sys.platform != "win32":
            self.assertEqual(current["pgid"], self.peer.pid)
            self.assertEqual(current["sid"], self.peer.pid)

    def argv(self, mode="live") -> list[str]:
        return [sys.executable, str(FIXTURE), "leader", str(self.root), mode]

    def fixture_identity(self, record: dict, role: str, mode="live") -> dict:
        self.assertIsInstance(record["birth"], (int, float), "unknown birth grants no admission")
        current = facts(record["pid"], record["birth"])
        self.assertEqual(current["identity"], "observed", current)
        self.assertEqual(current["birth"], record["birth"])
        self.assertEqual(current["state"], "live", current)
        expected = [str(FIXTURE), role, str(self.root)] + ([mode] if role == "leader" else [])
        self.assertEqual(current["cmdline"][1:], expected)
        base_executable = getattr(sys, "_base_executable", None)
        if not isinstance(base_executable, str):
            self.fail("actual CPython base executable is unavailable")
        allowed = {
            Path(path).resolve()
            for path in (sys.executable, base_executable, psutil.Process().exe())
        }
        self.assertIn(Path(current["exe"]).resolve(), allowed)
        self.assertIn(Path(current["cmdline"][0]).resolve(), allowed)
        current["executable_sha256"] = hashlib.sha256(Path(current["exe"]).read_bytes()).hexdigest()
        return current

    def release(self, process, mode="live") -> None:
        leader = self.receipt("leader")
        premise = {"leader": self.fixture_identity(leader, "leader", mode)}
        self.assertEqual(premise["leader"]["identity"], "observed")
        self.assertEqual(premise["leader"]["state"], "live")
        if sys.platform == "win32":
            # A venv Python redirector can be the managed root; executing
            # Python is then its exact direct child, inside the same Job.
            root = psutil.Process(process.pid)
            premise["managed_root"] = self.fixture_identity(
                {"pid": root.pid, "birth": root.create_time()}, "leader", mode
            )
            if leader["pid"] != root.pid:
                executing = psutil.Process(leader["pid"])
                self.assertEqual(executing.create_time(), leader["birth"])
                self.assertEqual(executing.ppid(), root.pid)
                self.assertEqual(leader["ppid"], root.pid)
            self.assertEqual(leader["argv"][-3:], ["leader", str(self.root), mode])
            for pid in (root.pid, leader["pid"]):
                self.assert_job_membership(pid, root.pid, True)
            self.assert_job_membership(self.peer_receipt["pid"], root.pid, False)
        else:
            self.assertEqual(leader["pid"], process.pid)
        if mode != "terminal":
            child = self.fixture_identity(self.receipt("leaf"), "leaf")
            premise["child"] = child
            self.assertEqual(child["state"], "live", child)
            spawned = self.receipt("leaf-root")
            child_root = self.fixture_identity(spawned, "leaf")
            self.assertEqual(spawned["creator_pid"], leader["pid"])
            self.assertEqual(spawned["creator_birth"], leader["birth"])
            self.assertEqual(child_root["ppid"], leader["pid"])
            self.assertEqual(spawned["ppid"], leader["pid"])
            premise["actual_spawned_leaf_root"] = child_root
            if child["pid"] != child_root["pid"]:
                self.assertEqual(sys.platform, "win32", "unexpected executing-child topology")
                self.assertEqual(child["ppid"], child_root["pid"])
            else:
                self.assertEqual(child["birth"], child_root["birth"])
                self.assertEqual(child["ppid"], leader["pid"])
            if sys.platform == "win32":
                self.assert_job_membership(child_root["pid"], process.pid, True)
                self.assert_job_membership(child["pid"], process.pid, True)
            if sys.platform != "win32":
                self.assertEqual(child["pgid"], process.pid)
                self.assertEqual(child["sid"], process.pid)
        self.note("native spawn and live descendant premise", **premise)
        (self.root / "release").touch()

    def refused_spawn_identity(self, fault: str) -> None:
        if sys.platform != "win32":
            self.fail("native Windows refusal control")
        original = self.receipt

        def receipt(role):
            value = original(role)
            if role == "leaf-root":
                if fault == "foreign":
                    return {**value, **self.peer_receipt}
                if fault == "reused":
                    return {**value, "birth": value["birth"] - 1}
                return {**value, "birth": None}
            return value

        with (
            patch.object(self, "receipt", receipt),
            patch.object(
                processes, "terminate_process_group", wraps=processes.terminate_process_group
            ) as cleanup,
        ):
            with self.assertRaises(AssertionError):
                self.capture(
                    self.argv(),
                    marker_store=self.store,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        cleanup.assert_not_called()
        self.assertFalse((self.root / "release").exists())
        self.assertEqual(self.current("leaf")["state"], "live")
        self.assertTrue(list(self.store.root.glob("*.json")))
        self.peer_preserved()
        self.note("native refusal before release", fault=fault)

    @unittest.skipUnless(sys.platform == "win32", "native Windows Job refusal")
    def test_windows_foreign_spawn_root_is_refused_before_release(self) -> None:
        self.refused_spawn_identity("foreign")

    @unittest.skipUnless(sys.platform == "win32", "native Windows birth refusal")
    def test_windows_reused_spawn_root_birth_is_refused_before_release(self) -> None:
        self.refused_spawn_identity("reused")

    @unittest.skipUnless(sys.platform == "win32", "native Windows unknown birth refusal")
    def test_windows_unknown_spawn_root_birth_is_refused_before_release(self) -> None:
        self.refused_spawn_identity("unknown")

    def assert_job_membership(self, pid: int, root_pid: int, expected: bool) -> None:
        if sys.platform != "win32":
            self.fail("Windows Job premise requested on a different host")
        import ctypes
        from ctypes import wintypes

        job = processes._WINDOWS_JOB_HANDLES[root_pid]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        kernel.IsProcessInJob.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        self.assertTrue(handle, "exact fixture process handle unavailable")
        try:
            observed = wintypes.BOOL()
            self.assertTrue(
                kernel.IsProcessInJob(handle, wintypes.HANDLE(job), ctypes.byref(observed))
            )
            self.assertEqual(bool(observed.value), expected)
            self.note(
                "actual Job membership",
                root=root_pid,
                pid=pid,
                job=job,
                member=bool(observed.value),
            )
        finally:
            kernel.CloseHandle(handle)

    def capture(self, *args, **kwargs) -> tuple[subprocess.Popen, Path]:
        process, marker = self.real_spawn(*args, **kwargs)
        assert marker is not None
        self.owned.append((process, marker))
        self.note("owned admission", pid=process.pid, marker=str(marker))
        self.release(process, args[0][-1])
        return process, marker

    def assert_actual_reap(self, process) -> None:
        if sys.platform != "win32":
            with self.assertRaises(ChildProcessError):
                os.waitpid(process.pid, os.WNOHANG)
        else:
            import ctypes
            from ctypes import wintypes

            handle = wintypes.HANDLE(int(process._handle))
            code = wintypes.DWORD()
            kernel = getattr(ctypes, "windll").kernel32
            self.assertEqual(kernel.WaitForSingleObject(handle, 0), 0)
            self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
            self.assertEqual(code.value, 0)
            self.assertNotIn(process.pid, processes._WINDOWS_JOB_HANDLES)
        self.assertEqual(process.returncode, 0)
        self.note("actual native leader reap or completed Windows handle", pid=process.pid)

    def test_normal_zero_exit_caller_clears_live_child_and_preserves_exact_peer(self) -> None:
        with patch.object(processes, "popen_owned", self.capture):
            result = processes.run_owned(
                self.argv(),
                timeout=15,
                capture_output=True,
                text=True,
                marker_store=self.store,
            )
        child = self.current("leaf")
        self.note("natural caller return before any rescue", result=repr(result), child=child)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "validator-normal-output\n")
        self.assertEqual(result.stderr, "validator-normal-error\n")
        self.assertIn(child["state"], {"terminal", "gone"}, child)
        self.assert_actual_reap(self.owned[0][0])
        self.assertFalse(list(self.store.root.glob("*.json")))
        self.peer_preserved()

    @unittest.skipUnless(os.name == "posix", "POSIX retained terminal-group control")
    def test_actual_terminal_only_leader_needs_no_group_signal(self) -> None:
        if sys.platform == "win32":
            self.fail("POSIX-only control")
        with (
            patch.object(processes, "popen_owned", self.capture),
            patch.object(
                processes.os, "killpg", side_effect=PermissionError("signal forbidden")
            ) as signals,
        ):
            result = processes.run_owned(
                self.argv("terminal"),
                timeout=15,
                capture_output=True,
                text=True,
                marker_store=self.store,
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "validator-normal-output\n")
        signals.assert_not_called()
        self.assert_actual_reap(self.owned[0][0])
        self.assertFalse(list(self.store.root.glob("*.json")))
        self.peer_preserved()

    @unittest.skipUnless(os.name == "posix", "scoped POSIX permission fault with real members")
    def test_denied_live_signals_keep_caller_error_marker_and_peer(self) -> None:
        if sys.platform == "win32":
            self.fail("POSIX-only control")
        with (
            patch.object(processes, "popen_owned", self.capture),
            patch.object(
                processes.os, "killpg", side_effect=PermissionError("scoped denial")
            ) as signals,
        ):
            with self.assertRaisesRegex(RuntimeError, "descendants could not be cleared"):
                processes.run_owned(
                    self.argv(),
                    timeout=15,
                    capture_output=True,
                    text=True,
                    marker_store=self.store,
                )
        self.assertTrue(signals.call_args_list)
        leader = self.owned[0][0]
        self.assertTrue(all(call.args[0] == leader.pid for call in signals.call_args_list))
        self.assertEqual(leader.returncode, 0)
        self.assertEqual(self.current("leaf")["state"], "live")
        self.assertEqual(len(list(self.store.root.glob("*.json"))), 1)
        self.note(
            "denied-signal natural result",
            leader=facts(leader.pid),
            child=self.current("leaf"),
        )
        self.peer_preserved()

    @unittest.skipUnless(os.name == "posix", "scoped POSIX observation fault with real members")
    def test_unknown_membership_grants_no_signal_and_keeps_marker(self) -> None:
        if sys.platform == "win32":
            self.fail("POSIX-only control")
        with (
            patch.object(processes, "popen_owned", self.capture),
            patch.object(
                processes, "_current_group_members", side_effect=OSError("scoped observation fault")
            ),
            patch.object(processes.os, "killpg", wraps=os.killpg) as signals,
        ):
            with self.assertRaisesRegex(RuntimeError, "descendants could not be cleared"):
                processes.run_owned(
                    self.argv(),
                    timeout=15,
                    capture_output=True,
                    text=True,
                    marker_store=self.store,
                )
        signals.assert_not_called()
        self.assertEqual(self.current("leaf")["state"], "live")
        self.assertEqual(len(list(self.store.root.glob("*.json"))), 1)
        self.peer_preserved()

    @unittest.skipUnless(os.name == "posix", "actual external leader reap; no simulated PID reuse")
    def test_actual_external_reap_releases_signal_authority_despite_cached_status(self) -> None:
        if sys.platform == "win32":
            self.fail("POSIX-only control")
        process, marker = self.capture(
            self.argv(),
            marker_store=self.store,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(process.wait(timeout=15), 0)
        pid, status = os.waitpid(process.pid, 0)
        self.assertEqual((pid, os.waitstatus_to_exitcode(status)), (process.pid, 0))
        self.assertEqual(self.current("leaf")["state"], "live")
        self.note(
            "external kernel reap before cleanup",
            pid=pid,
            cached_status=process.returncode,
        )
        with patch.object(processes.os, "killpg", wraps=os.killpg) as signals:
            confirmed = processes.terminate_process_group(process)
        self.assertFalse(confirmed)
        signals.assert_not_called()
        self.assertTrue(marker.is_file())
        self.assertEqual(self.current("leaf")["state"], "live")
        self.peer_preserved()

    @unittest.skipUnless(
        os.name == "posix", "unadmitted raw group has no retained caller authority"
    )
    def test_reaped_plain_popen_live_group_receives_only_absence_probes(self) -> None:
        if sys.platform == "win32":
            self.fail("POSIX-only control")
        process = subprocess.Popen(
            self.argv(),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.release(process)
        self.assertEqual(process.wait(timeout=15), 0)
        self.assertEqual(self.current("leaf")["state"], "live")
        with patch.object(processes.os, "killpg", wraps=os.killpg) as signals:
            confirmed = processes.terminate_process_group(process)
        self.assertFalse(confirmed)
        self.assertTrue(signals.call_args_list)
        self.assertTrue(all(call.args == (process.pid, 0) for call in signals.call_args_list))
        self.assertEqual(self.current("leaf")["state"], "live")
        self.peer_preserved()

    def emergency(self) -> None:
        self.note("behavioral assertion boundary before separate emergency cleanup")
        for role in ("leaf", "leader", "peer"):
            path = self.root / (role + ".json")
            if not path.is_file():
                self.note("emergency identity gap", role=role)
                continue
            try:
                identity = json.loads(path.read_text(encoding="utf-8"))
                before = facts(identity["pid"], identity["birth"])
                self.note("before emergency", role=role, facts=before)
                if before.get("identity") == "observed" and before["state"] == "live":
                    process = psutil.Process(identity["pid"])
                    if process.create_time() == identity["birth"]:
                        process.kill()
                        self.note("emergency exact identity kill", role=role, facts=before)
                if sys.platform != "win32":
                    try:
                        os.waitpid(identity["pid"], os.WNOHANG)
                    except ChildProcessError:
                        pass
            except (OSError, psutil.Error, ValueError) as exc:
                self.note("emergency cleanup error", role=role, error=repr(exc))
        for process, _marker in self.owned:
            if sys.platform == "win32":
                confirmed = processes.terminate_process_group(process)
                self.note(
                    "separate fixture Job containment and actual root reap",
                    pid=process.pid,
                    confirmed=confirmed,
                    marker_retained=bool(_marker.exists()),
                )
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        try:
            self.peer.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.note("emergency peer wait incomplete")
        (self.root / "validator-events.json").write_text(
            json.dumps({"test": self.id(), "events": self.events}, indent=2) + "\n",
            encoding="utf-8",
        )
