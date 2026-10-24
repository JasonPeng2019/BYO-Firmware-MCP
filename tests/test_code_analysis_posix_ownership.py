"""Actual native Python lifecycle contracts for ROOT's matching-host Actions.

No analyzer/header fixture is executed. Linux /proc and macOS libproc identify
each observed PID/start independently of production psutil ownership tokens.
Windows Job/hardware contracts remain in their original modules.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

import psutil

from tests.code_analysis_managed_support import posix_process_identity, production


def native_retained_exit(pid: int) -> tuple[int, int, int] | None:
    """Independent WNOWAIT oracle: exact direct child, event kind, exit status.

    Never call the production exit decoder or Popen.poll here. Repeating this
    native observation proves that the child has not been reaped in between.
    """
    if sys.platform == "darwin":
        # Public Darwin siginfo_t/waitid ABI; Python need not expose os.waitid.
        class Siginfo(ctypes.Structure):
            _fields_ = [
                ("signo", ctypes.c_int),
                ("error", ctypes.c_int),
                ("code", ctypes.c_int),
                ("pid", ctypes.c_int),
                ("uid", ctypes.c_uint),
                ("status", ctypes.c_int),
                ("address", ctypes.c_void_p),
                ("value", ctypes.c_void_p),
                ("band", ctypes.c_long),
                ("padding", ctypes.c_ulong * 7),
            ]

        waitid = ctypes.CDLL(None, use_errno=True).waitid
        waitid.argtypes = (ctypes.c_int, ctypes.c_uint, ctypes.POINTER(Siginfo), ctypes.c_int)
        waitid.restype = ctypes.c_int
        result = Siginfo()
        # Darwin P_PID=1, WEXITED=4, WNOHANG=1, WNOWAIT=32.
        if waitid(1, pid, ctypes.byref(result), 4 | 1 | 32) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return (result.pid, result.code, result.status) if result.pid else None
    if sys.platform.startswith("linux"):
        result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        return (result.si_pid, result.si_code, result.si_status) if result else None
    raise AssertionError("Retained-exit oracle requires a matching Linux/macOS Actions host")


def native_group_members(group: int) -> set[int]:
    """Original-group oracle independent of production psutil enumeration."""
    if sys.platform.startswith("linux"):
        members = set()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                data = (entry / "stat").read_text()
            except (FileNotFoundError, ProcessLookupError):
                continue
            fields = data[data.rfind(")") + 2 :].split()
            if int(fields[2]) == group:
                members.add(int(entry.name))
        return members
    if sys.platform == "darwin":
        listpids = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_listpids
        listpids.argtypes = (ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_int)
        listpids.restype = ctypes.c_int
        capacity = 64
        while capacity <= 65536:
            buffer = (ctypes.c_int * capacity)()
            size = ctypes.sizeof(buffer)
            ctypes.set_errno(0)
            used = listpids(2, group, buffer, size)  # PROC_PGRP_ONLY, includes zombies.
            error = ctypes.get_errno()
            if (
                used < 0
                or used > size
                or used % ctypes.sizeof(ctypes.c_int)
                or (used == 0 and error)
            ):
                raise OSError(error, "Independent native group observation unavailable")
            if used < size:
                members = set(buffer[: used // ctypes.sizeof(ctypes.c_int)])
                if any(pid <= 0 for pid in members):
                    raise AssertionError("Invalid native group membership")
                return members
            capacity *= 2
        raise AssertionError("Independent native group observation was truncated")
    raise AssertionError("Native group oracle requires a matching Linux/macOS Actions host")


# Gates keep the native leader available until its exact identity is observed.
# Detached descendants remain in the owned tree long enough for observation.
TREE = r"""
import json, os, pathlib, signal, sys, time
base, detach = pathlib.Path(sys.argv[1]), sys.argv[2] == "detach"
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (base / "child-ready").write_text(str(os.getpid()))
    if detach:
        while not (base / "detach").exists(): time.sleep(.01)
        os.setsid()
        (base / "detached").write_text(str(os.getpgrp()))
    grandchild = None
    while True:
        if grandchild is None and (base / "fork-grandchild").exists():
            grandchild = os.fork()
            if grandchild == 0:
                os.setsid()
                (base / "grandchild-ready.tmp").write_text(str(os.getpid()))
                os.replace(base / "grandchild-ready.tmp", base / "grandchild-ready")
                while True: time.sleep(.05)
        time.sleep(.05)
(base / "ready.tmp").write_text(json.dumps({"leader": os.getpid(), "child": child, "group": os.getpgrp()}))
os.replace(base / "ready.tmp", base / "ready")
while not (base / "exit").exists(): time.sleep(.01)
sys.stdout.write("native-fixture-complete\n")
sys.stdout.flush()
"""

ESCAPE = r"""
import os, pathlib, sys, time
base = pathlib.Path(sys.argv[1])
(base / 'ready').touch()
while not (base / 'release').exists(): time.sleep(.005)
middle = os.fork()
if middle == 0:
    os.setsid()
    escaped = os.fork()
    if escaped: os._exit(0)
    os.close(1)
    os.close(2)
    (base / 'escaped.tmp').write_text(str(os.getpid()))
    os.replace(base / 'escaped.tmp', base / 'escaped')
    while True: time.sleep(.05)
os.waitpid(middle, 0)
(base / 'reparented').write_text(str(middle))
while not (base / 'exit').exists(): time.sleep(.005)
"""


# Like clangd's --query-driver probes: short-lived owned children whose own
# children outlive them, so ancestry proposals retire or reparent mid-walk.
CHURN = r"""
import os, pathlib, sys, time
base, generations = pathlib.Path(sys.argv[1]), 0
while not (base / "exit").exists():
    middle = os.fork()
    if middle == 0:
        if os.fork() == 0:
            time.sleep(.002)
            os._exit(0)
        os._exit(0)
    os.waitpid(middle, 0)
    generations += 1
    if generations == 100:
        (base / "churning").touch()
"""

# A live owned parent with one exact child for direct ancestry contracts.
PARENT = r"""
import os, pathlib, sys, time
base = pathlib.Path(sys.argv[1])
child = os.fork()
if child == 0:
    while not (base / "exit").exists(): time.sleep(.01)
    os._exit(0)
(base / "child-ready.tmp").write_text(str(child))
os.replace(base / "child-ready.tmp", base / "child-ready")
os.waitpid(child, 0)
(base / "reaped").touch()
while not (base / "exit-parent").exists(): time.sleep(.01)
"""


@unittest.skipUnless(os.name == "posix", "Actual POSIX process ownership; Windows Jobs separate")
class NativePosixOwnership(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="analysis ownership ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.runtime = production("code_analysis_runtime")
        self.processes = production("kernel.processes")
        self.store = self.processes.ProcessMarkerStore(self.base / "markers")
        self.owned = []
        self.observed: dict[int, str] = {}
        self.enterContext(mock.patch.object(self.runtime, "_spawn_cleanup_failure", None))
        self.enterContext(mock.patch.object(self.runtime, "_spawn_admission", threading.Lock()))
        self.enterContext(mock.patch.object(self.runtime, "_probe_cache", {}))
        self.addCleanup(self.emergency_cleanup)

    def until(self, predicate, budget: float = 5):
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        self.fail("Native ownership fixture did not reach its barrier")

    def observe(self, pid: int) -> str:
        identity = self.until(lambda: posix_process_identity(pid))
        self.assertNotEqual(identity[1], "Z", identity)
        self.observed[pid] = identity[0]
        return identity[0]

    def is_owned_live(self, pid: int, token: str) -> bool:
        current = posix_process_identity(pid)
        return current is not None and current[0] == token and current[1] != "Z"

    def emergency_cleanup(self) -> None:
        # Never signal a recycled PID or an identity we did not actually capture.
        for pid, token in list(self.observed.items()):
            if self.is_owned_live(pid, token):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for process, _ in self.owned:
            try:
                process.wait(timeout=2)
            except (subprocess.TimeoutExpired, RuntimeError):
                pass
            # Negative contracts intentionally leave a native anchor retained.
            # Reap only this fixture's exact direct child after the independent
            # oracle observes its exit; never upgrade its production receipt.
            try:
                if native_retained_exit(process.pid) is not None:
                    os.waitpid(process.pid, os.WNOHANG)
            except ChildProcessError:
                pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def spawn(self, argv, **kwargs):
        process, marker = self.runtime.spawn_before_deadline(
            argv,
            time.monotonic() + 5,
            None,
            self.store,
            cwd=self.base,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )
        self.owned.append((process, marker))
        self.observe(process.pid)
        self.assertIsNotNone(marker)
        saved = json.loads(marker.read_bytes())
        self.assertEqual(saved["pid"], process.pid)
        self.assertTrue(self.processes.identity_matches(process.pid, saved["start_token"]))
        self.assertEqual(os.getsid(process.pid), process.pid)
        self.assertEqual(os.getpgid(process.pid), process.pid)
        return process, marker

    def tree(self, *, detach: bool = False):
        directory = self.base / ("detached-tree" if detach else "group-tree")
        directory.mkdir()
        process, marker = self.spawn(
            [sys.executable, "-c", TREE, str(directory), "detach" if detach else "group"]
        )
        self.until(lambda: (directory / "ready").exists() and (directory / "child-ready").exists())
        ready = json.loads((directory / "ready").read_text())
        self.assertEqual(ready["leader"], process.pid)
        self.assertEqual(ready["group"], process.pid)
        self.observe(ready["child"])
        if detach:
            # Observe while still in the tree, then exercise later group escape.
            observation_deadline = time.monotonic() + 0.5
            while time.monotonic() < observation_deadline:
                self.assertIsNone(process.poll())
                time.sleep(0.01)
            (directory / "detach").touch()
            self.until(lambda: (directory / "detached").exists())
            self.assertEqual(os.getpgid(ready["child"]), ready["child"])
        return process, marker, directory, ready

    def peer(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.owned.append((process, None))
        token = self.observe(process.pid)
        return process, token

    @contextmanager
    def unavailable_leader_image(self, pid: int, *, metadata: bool, session: bool):
        """Fault only live-image queries for this real, positively exited child."""
        actual_birth, actual_status = psutil.Process.create_time, psutil.Process.status
        actual_group, actual_session = os.getpgid, os.getsid

        def birth(candidate):
            if candidate.pid == pid:
                raise psutil.AccessDenied(pid)
            return actual_birth(candidate)

        def status(candidate):
            if candidate.pid == pid:
                raise psutil.AccessDenied(pid)
            return actual_status(candidate)

        def group(candidate):
            if candidate == pid:
                raise ProcessLookupError(errno.ESRCH, "exited leader has no live group image")
            return actual_group(candidate)

        def sid(candidate):
            if candidate == pid:
                raise ProcessLookupError(errno.ESRCH, "exited leader has no live session image")
            return actual_session(candidate)

        with ExitStack() as stack:
            if metadata:
                stack.enter_context(mock.patch.object(psutil.Process, "create_time", birth))
                stack.enter_context(mock.patch.object(psutil.Process, "status", status))
            if session:
                stack.enter_context(mock.patch.object(os, "getpgid", side_effect=group))
                stack.enter_context(mock.patch.object(os, "getsid", side_effect=sid))
            yield

    def gated_exit(self, exit_code: int, name: str, *, release: bool = True):
        gate = self.base / name
        process, marker = self.spawn(
            [
                sys.executable,
                "-c",
                "import pathlib,sys,time; gate=pathlib.Path(sys.argv[1]);\n"
                "while not gate.exists(): time.sleep(.01)\n"
                "print('retained exit'); sys.exit(int(sys.argv[2]))",
                str(gate),
                str(exit_code),
            ]
        )
        self.assertIsNone(native_retained_exit(process.pid), "Positive live startup control")
        if not release:
            return process, marker, gate
        gate.touch()
        self.assertEqual(
            self.until(lambda: native_retained_exit(process.pid)), (process.pid, 1, exit_code)
        )
        return process, marker

    def test_exact_retained_exit_survives_unavailable_leader_live_image(self) -> None:
        peer, peer_token = self.peer()
        for exit_code in (0, 23):
            for metadata, session in ((True, False), (False, True), (True, True)):
                with self.subTest(exit_code=exit_code, metadata=metadata, session=session):
                    process, marker = self.gated_exit(
                        exit_code, f"exit-{exit_code}-{metadata}-{session}"
                    )
                    saved_marker = marker.read_bytes()
                    with self.unavailable_leader_image(
                        process.pid, metadata=metadata, session=session
                    ):
                        output, errors = process.communicate(timeout=5)
                        self.assertEqual(output, b"retained exit\n")
                        self.assertEqual(errors, b"")
                        self.assertEqual(process.returncode, exit_code)
                        self.assertEqual(
                            native_retained_exit(process.pid), (process.pid, 1, exit_code)
                        )
                        self.assertEqual(marker.read_bytes(), saved_marker)
                        self.confirm_cleanup(process, marker, [process.pid])
                    receipt = process.cleanup_receipt
                    self.assertEqual(receipt["leader_exit"], exit_code)
                    leader = [entry for entry in receipt["captured"] if entry["pid"] == process.pid]
                    self.assertEqual(len(leader), 1, receipt)
                    self.assertEqual(leader[0]["start_token"], receipt["start_token"])
                    self.assertEqual(leader[0]["state"], "zombie")
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(process.pid, os.WNOHANG)
                    self.assertTrue(self.is_owned_live(peer.pid, peer_token))

    def test_live_image_failure_after_actual_leader_exit_keeps_native_exit_authority(self) -> None:
        process, marker, gate = self.gated_exit(23, "exit-during-live-image", release=False)
        process._stop_observer.set()
        process._observer.join(timeout=1)
        self.assertFalse(process._observer.is_alive())
        self.assertIsNone(native_retained_exit(process.pid))
        actual_birth = psutil.Process.create_time
        reached = []

        def exit_then_deny(parent):
            if parent.pid == process.pid:
                gate.touch()
                self.assertEqual(
                    self.until(lambda: native_retained_exit(process.pid)), (process.pid, 1, 23)
                )
                reached.append(parent.pid)
                raise psutil.AccessDenied(parent.pid)
            return actual_birth(parent)

        with mock.patch.object(psutil.Process, "create_time", exit_then_deny):
            self.assertTrue(process._capture(time.monotonic() + 2))
        self.assertEqual(reached, [process.pid])
        self.assertFalse(process._uncertainties)
        self.assertEqual(native_retained_exit(process.pid), (process.pid, 1, 23))
        self.confirm_cleanup(process, marker, [process.pid])
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)

    def test_unavailable_exact_retained_exit_never_confirms_or_removes_marker(self) -> None:
        peer, peer_token = self.peer()
        posix = production("kernel._analysis_posix")
        actual_exit = posix._exit_status
        for exit_code, native_error in (
            (0, errno.EPERM),
            (23, errno.EPERM),
            (0, errno.ECHILD),
            (23, errno.ECHILD),
        ):
            with self.subTest(exit_code=exit_code, native_error=native_error):
                process, marker, gate = self.gated_exit(
                    exit_code, f"unknown-exit-{exit_code}-{native_error}", release=False
                )
                # Drain any live-state observation before releasing this actual
                # child; a check-then-wait injection can race to a truthful exit.
                process._stop_observer.set()
                observer = process._observer
                self.assertIsNotNone(observer)
                observer.join(timeout=0.5)
                self.assertFalse(observer.is_alive())
                self.assertIsNone(
                    process.returncode, "Gated live child has no cached terminal status"
                )
                saved_marker = marker.read_bytes()
                denied = []

                def unavailable_exit(pid):
                    if pid == process.pid:
                        denied.append(pid)
                        raise OSError(native_error, "exact retained exit unavailable")
                    return actual_exit(pid)

                with (
                    mock.patch.object(posix, "_exit_status", side_effect=unavailable_exit),
                ):
                    gate.touch()
                    self.assertEqual(
                        self.until(lambda: native_retained_exit(process.pid)),
                        (process.pid, 1, exit_code),
                    )
                    with self.unavailable_leader_image(process.pid, metadata=True, session=True):
                        self.assertFalse(
                            self.processes.terminate_process_group(process, grace_seconds=0.2)
                        )
                self.assertTrue(denied, "Cleanup must actually reach the unavailable exit seam")
                receipt = process.cleanup_receipt
                self.assertIs(receipt["confirmed"], False)
                self.assertIs(receipt["scope_confirmed"], False)
                self.assertIs(receipt["leader_reaped"], False)
                self.assertIsNone(receipt["leader_exit"], "Never invent exit zero from errno")
                self.assertTrue(receipt["uncertainties"])
                self.assertEqual(marker.read_bytes(), saved_marker)
                self.assertEqual(native_retained_exit(process.pid), (process.pid, 1, exit_code))
                self.assertFalse(self.is_owned_live(process.pid, self.observed[process.pid]))
                self.assertTrue(self.is_owned_live(peer.pid, peer_token))

    def test_cached_correct_exit_cannot_replace_fresh_mismatched_retained_proof(self) -> None:
        posix = production("kernel._analysis_posix")
        actual_exit = posix._exit_status
        for exit_code in (0, 23):
            with self.subTest(exit_code=exit_code):
                process, marker = self.gated_exit(exit_code, f"mismatched-exit-{exit_code}")
                self.assertEqual(process.wait(timeout=5), exit_code)
                self.assertEqual(native_retained_exit(process.pid), (process.pid, 1, exit_code))
                calls = []

                def mismatched_status(pid):
                    if pid == process.pid:
                        calls.append(pid)
                        return exit_code + 1
                    return actual_exit(pid)

                with mock.patch.object(posix, "_exit_status", side_effect=mismatched_status):
                    self.assertFalse(
                        self.processes.terminate_process_group(process, grace_seconds=0.2)
                    )
                self.assertTrue(calls, "Fresh authoritative decoder must actually be checked")
                receipt = process.cleanup_receipt
                self.assertIs(receipt["scope_confirmed"], False)
                self.assertIs(receipt["leader_reaped"], False)
                self.assertEqual(receipt["leader_exit"], exit_code)
                self.assertTrue(receipt["uncertainties"])
                self.assertTrue(marker.exists())
                self.assertEqual(native_retained_exit(process.pid), (process.pid, 1, exit_code))

    def test_retained_leader_exit_cannot_hide_inaccessible_captured_detached_live_child(
        self,
    ) -> None:
        peer, peer_token = self.peer()
        process, marker, directory, ready = self.tree(detach=True)
        child, token = ready["child"], self.observed[ready["child"]]
        self.until(lambda: any(pid == child for pid, _ in process._captured))
        (directory / "exit").touch()
        self.assertEqual(self.until(lambda: native_retained_exit(process.pid)), (process.pid, 1, 0))
        actual_birth = psutil.Process.create_time
        denied = []

        def child_birth(candidate):
            if candidate.pid == child:
                denied.append(child)
                raise psutil.AccessDenied(child)
            return actual_birth(candidate)

        saved_marker = marker.read_bytes()
        with (
            mock.patch.object(psutil.Process, "create_time", child_birth),
            self.unavailable_leader_image(process.pid, metadata=True, session=True),
        ):
            self.assertFalse(self.processes.terminate_process_group(process, grace_seconds=0.2))
        self.assertTrue(denied, "Actual captured descendant observation must reach the fault")
        receipt = process.cleanup_receipt
        self.assertEqual(receipt["leader_exit"], 0)
        self.assertIs(receipt["leader_reaped"], False)
        self.assertIs(receipt["confirmed"], False)
        self.assertTrue(receipt["uncertainties"])
        captured = {entry["pid"]: entry for entry in receipt["captured"]}
        self.assertEqual(captured[child]["state"], "unknown")
        self.assertTrue(self.is_owned_live(child, token))
        self.assertEqual(marker.read_bytes(), saved_marker)
        self.assertEqual(native_retained_exit(process.pid), (process.pid, 1, 0))
        self.assertTrue(self.is_owned_live(peer.pid, peer_token))

    def test_already_exited_before_begin_confirms_only_retained_group_and_captured_scope(
        self,
    ) -> None:
        peer, peer_token = self.peer()
        posix = production("kernel._analysis_posix")
        actual_begin = posix.AnalysisPopen.begin_ownership
        for exit_code in (0, 23):
            for missing_birth in (False, True):
                with (
                    self.subTest(exit_code=exit_code, missing_birth=missing_birth),
                    ExitStack() as faults,
                ):
                    began_after_exit = []

                    def begin_after_native_exit(process):
                        # Observe the actual startup race, without a fake receipt.
                        self.owned.append((process, None))
                        proof = self.until(lambda: native_retained_exit(process.pid))
                        self.assertEqual(proof, (process.pid, 1, exit_code))
                        began_after_exit.append(proof)
                        faults.enter_context(
                            self.unavailable_leader_image(
                                process.pid, metadata=missing_birth, session=True
                            )
                        )
                        actual_begin(process)

                    with mock.patch.object(
                        posix.AnalysisPopen, "begin_ownership", begin_after_native_exit
                    ):
                        process, marker = self.processes.popen_owned(
                            [
                                sys.executable,
                                "-c",
                                f"print('startup exit'); raise SystemExit({exit_code})",
                            ],
                            marker_store=self.store,
                            analysis_ownership=True,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                    self.assertEqual(began_after_exit, [(process.pid, 1, exit_code)])
                    if marker is None:
                        self.assertIs(process.cleanup_receipt["leader_reaped"], True)
                    else:
                        saved = json.loads(marker.read_bytes())
                    if missing_birth:
                        self.assertIsNone(
                            marker, "Missing birth cannot manufacture a recovery token"
                        )
                    output, errors = process.communicate(timeout=5)
                    self.assertEqual((output, errors), (b"startup exit\n", b""))
                    self.assertEqual(process.returncode, exit_code)
                    self.assertTrue(
                        self.processes.terminate_process_group(process, grace_seconds=0.5)
                    )
                    receipt = process.cleanup_receipt
                    self.assert_scoped_receipt(receipt)
                    self.assertEqual(receipt["leader_exit"], exit_code)
                    self.assertEqual(
                        receipt["start_token"], None if marker is None else saved["start_token"]
                    )
                    leader = next(
                        entry for entry in receipt["captured"] if entry["pid"] == process.pid
                    )
                    self.assertEqual(leader["start_token"], receipt["start_token"])
                    self.assertTrue(leader.get("identity_source"), receipt)
                    self.assertIs(receipt["early_direct_child_exit"], True)
                    self.assertIs(receipt["new_session_spawn_succeeded"], True)
                    self.assertEqual(receipt["spawn_parent_pid"], os.getpid())
                    if sys.platform == "darwin":
                        terminal = receipt["native_terminal_group"]
                        self.assertEqual({entry["pid"] for entry in terminal}, {process.pid})
                        self.assertTrue(all(entry["state"] == "zombie" for entry in terminal))
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(process.pid, os.WNOHANG)
                    self.assertEqual(native_group_members(process.pid), set())
                    self.store.remove(marker)
                    self.assertFalse(list(self.store.root.glob("*.json")))
                    self.assertTrue(self.is_owned_live(peer.pid, peer_token))

    def assert_scoped_receipt(self, receipt) -> None:
        self.assertEqual(receipt["coverage"], "retained_group_and_captured_descendants")
        self.assertIs(receipt["scope_confirmed"], True)
        self.assertEqual(receipt["unobserved_detached_descendants"], "not_proven")
        self.assertIs(receipt["leader_reaped"], True)
        self.assertEqual(receipt["uncertainties"], [])

    def test_never_observed_native_escape_remains_explicitly_not_proven(self) -> None:
        peer, peer_token = self.peer()
        posix = production("kernel._analysis_posix")
        actual_begin = posix.AnalysisPopen.begin_ownership
        directory = self.base / "unobserved-escape"
        directory.mkdir()
        escaped_identity = []

        def begin_after_escape(process):
            self.owned.append((process, None))
            self.observe(process.pid)
            self.until(lambda: (directory / "ready").exists())
            (directory / "release").touch()
            self.until(
                lambda: (directory / "escaped").exists() and (directory / "reparented").exists()
            )
            escaped = int((directory / "escaped").read_text())
            middle = int((directory / "reparented").read_text())
            token = self.observe(escaped)
            self.until(lambda: psutil.Process(escaped).ppid() not in {middle, process.pid})
            self.assertNotEqual(os.getpgid(escaped), process.pid)
            self.assertNotEqual(os.getsid(escaped), process.pid)
            escaped_identity.append((escaped, token))
            # Product ownership has not begun yet: the independent test knows
            # this lineage, while the product has never seen the escaped child.
            actual_begin(process)

        with mock.patch.object(posix.AnalysisPopen, "begin_ownership", begin_after_escape):
            process, marker = self.processes.popen_owned(
                [sys.executable, "-c", ESCAPE, str(directory)],
                marker_store=self.store,
                analysis_ownership=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        escaped, token = escaped_identity[0]
        self.assertNotIn(escaped, {pid for pid, _ in process._captured})
        (directory / "exit").touch()
        self.assertEqual(self.until(lambda: native_retained_exit(process.pid)), (process.pid, 1, 0))
        self.confirm_cleanup(process, marker, [process.pid])
        self.assertEqual(native_group_members(process.pid), set())
        self.assertNotIn(escaped, {entry["pid"] for entry in process.cleanup_receipt["captured"]})
        self.assertTrue(
            self.is_owned_live(escaped, token), "Scope success cannot mean full-tree absence"
        )
        self.assertTrue(self.is_owned_live(peer.pid, peer_token))
        # emergency_cleanup owns only the independently observed exact native
        # fixture identities, including this escape; it never repairs a receipt.

    def test_incomplete_native_observation_can_recover_with_final_proof_in_same_reserve(
        self,
    ) -> None:
        process, marker, _, ready = self.tree()
        # Control one actual ownership iteration, then let cleanup perform its
        # fresh native observations. Previously acquired descendants stay owned.
        self.until(lambda: any(pid == ready["child"] for pid, _ in tuple(process._captured)))
        process._stop_observer.set()
        process._observer.join(1)
        self.assertFalse(process._observer.is_alive())
        actual_children = psutil.Process.children
        entered, release = threading.Event(), threading.Event()
        outcome, failures = [], []
        deadline = time.monotonic() + 0.1

        def bounded_children(candidate, recursive=False):
            children = actual_children(candidate, recursive=recursive)
            if candidate.pid == process.pid:
                entered.set()
                self.assertTrue(release.wait(2), "Fixture release must remain bounded")
            return children

        def observe_one_iteration():
            try:
                outcome.append(process._capture(deadline))
            except BaseException as exc:
                failures.append(exc)

        with mock.patch.object(psutil.Process, "children", bounded_children):
            observer = threading.Thread(target=observe_one_iteration)
            observer.start()
            try:
                self.assertTrue(
                    entered.wait(2), "Actual native child listing must reach the barrier"
                )
                self.until(lambda: time.monotonic() >= deadline)
            finally:
                release.set()
                observer.join(2)
        self.assertFalse(observer.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(outcome, [False], "Incomplete iteration is not complete proof")
        self.assertTrue(self.is_owned_live(ready["child"], self.observed[ready["child"]]))
        self.confirm_cleanup(process, marker, [process.pid, ready["child"]])
        self.assertGreater(process.cleanup_receipt["incomplete_observation_count"], 0)
        self.assertEqual(process.cleanup_receipt["budget_seconds"], 1.5)

    def test_missing_final_native_observation_never_uses_earlier_complete_snapshot(self) -> None:
        process, marker, _, ready = self.tree()
        self.until(lambda: any(pid == ready["child"] for pid, _ in tuple(process._captured)))
        actual_children = psutil.Process.children
        denied = []

        def denied_children(candidate, recursive=False):
            if candidate.pid == process.pid:
                denied.append(candidate.pid)
                raise psutil.AccessDenied(candidate.pid)
            return actual_children(candidate, recursive=recursive)

        before = time.monotonic()
        with mock.patch.object(psutil.Process, "children", denied_children):
            self.assertFalse(self.processes.terminate_process_group(process, grace_seconds=0.2))
        self.assertTrue(denied, "Actual live ownership observation must reach denied access")
        receipt = process.cleanup_receipt
        self.assertIs(receipt["scope_confirmed"], False)
        self.assertIs(receipt["leader_reaped"], False)
        self.assertTrue(receipt["uncertainties"])
        self.assertTrue(marker.exists())
        self.assertAlmostEqual(receipt["budget_seconds"], 0.6)
        self.assertLess(time.monotonic() - before, 0.6 + 0.3)

    def test_final_native_membership_exhaustion_retains_marker_and_unchanged_reserve(self) -> None:
        process, marker, _, ready = self.tree()
        self.until(lambda: any(pid == ready["child"] for pid, _ in tuple(process._captured)))
        process._stop_observer.set()
        process._observer.join(1)
        self.assertFalse(process._observer.is_alive())
        posix = production("kernel._analysis_posix")
        owner, name = (
            (posix, "_darwin_group_members") if sys.platform == "darwin" else (psutil, "pids")
        )
        actual_snapshot = getattr(owner, name)
        observed = []
        began = time.monotonic()
        expires = None

        def exhausted_snapshot(*args, **kwargs):
            nonlocal expires
            members = actual_snapshot(*args, **kwargs)
            observed.append(members)
            # A real native enumeration becomes too late to prove retirement.
            # This barrier consumes exactly the caller's original reserve.
            if expires is None:
                expires = time.monotonic() + 0.6
            threading.Event().wait(max(0, expires - time.monotonic()))
            return members

        with mock.patch.object(owner, name, side_effect=exhausted_snapshot):
            self.assertFalse(self.processes.terminate_process_group(process, grace_seconds=0.2))
        self.assertTrue(observed, "Actual native membership acquisition must reach the barrier")
        receipt = process.cleanup_receipt
        self.assertIs(receipt["scope_confirmed"], False)
        self.assertIs(receipt["leader_reaped"], False)
        self.assertTrue(receipt["uncertainties"])
        self.assertTrue(marker.exists())
        self.assertAlmostEqual(receipt["budget_seconds"], 0.6)
        self.assertLess(time.monotonic() - began, 0.6 + 0.3)
        self.assertTrue(self.is_owned_live(ready["child"], self.observed[ready["child"]]))

    @unittest.skipUnless(sys.platform == "darwin", "Darwin live-to-terminal group signal race")
    def test_darwin_term_eperm_after_real_child_exit_requires_fresh_terminal_proof(self) -> None:
        peer, peer_token = self.peer()
        actual_killpg = os.killpg
        for exit_code in (0, 23):
            with self.subTest(exit_code=exit_code):
                process, marker, gate = self.gated_exit(
                    exit_code, f"darwin-terminal-race-{exit_code}", release=False
                )
                actual_terminal = process._darwin_terminal_group
                terminal_checks, denied = [], []

                def terminal_group(deadline):
                    terminal = actual_terminal(deadline)
                    if not terminal_checks:
                        self.assertIsNone(native_retained_exit(process.pid))
                        self.assertIsNone(
                            terminal, "First check legitimately observes a live child"
                        )
                    terminal_checks.append(terminal)
                    return terminal

                def race_at_term(group, selected_signal):
                    if group == process.pid and selected_signal == signal.SIGTERM:
                        self.assertTrue(terminal_checks)
                        gate.touch()
                        self.assertEqual(
                            self.until(lambda: native_retained_exit(process.pid)),
                            (process.pid, 1, exit_code),
                        )
                        denied.append((group, selected_signal))
                        # Fault only the actual signal call after independently
                        # proving exit. Subsequent native membership/wait reads
                        # stay real; errno by itself supplies no cleanup proof.
                        raise PermissionError(errno.EPERM, "Darwin terminal signal race")
                    return actual_killpg(group, selected_signal)

                with (
                    mock.patch.object(
                        process, "_darwin_terminal_group", side_effect=terminal_group
                    ),
                    mock.patch.object(os, "killpg", side_effect=race_at_term),
                ):
                    self.confirm_cleanup(process, marker, [process.pid])
                self.assertEqual(denied, [(process.pid, signal.SIGTERM)])
                self.assertGreaterEqual(len(terminal_checks), 2)
                receipt = process.cleanup_receipt
                self.assertEqual(receipt["leader_exit"], exit_code)
                terminal = receipt["native_terminal_group"]
                self.assertEqual({entry["pid"] for entry in terminal}, {process.pid})
                self.assertTrue(all(entry["state"] == "zombie" for entry in terminal))
                self.assertTrue(
                    any(
                        operation["signal"] == signal.SIGTERM and operation["errno"] == errno.EPERM
                        for operation in receipt["group_signal_operations"]
                    ),
                    receipt,
                )
                self.assertTrue(
                    any(
                        operation["operation"] == "terminal-group-proof"
                        and operation["result"] == "terminal-membership-after-eperm"
                        and operation["signal"] == signal.SIGTERM
                        for operation in receipt["group_signal_operations"]
                    ),
                    receipt,
                )
                self.assertEqual(native_group_members(process.pid), set())
                self.assertTrue(self.is_owned_live(peer.pid, peer_token))

    @unittest.skipUnless(sys.platform == "darwin", "Darwin terminal signal failure controls")
    def test_darwin_eperm_retries_temporarily_incomplete_terminal_proof_in_same_reserve(self):
        peer, peer_token = self.peer()
        actual_killpg = os.killpg
        for exit_code in (0, 23):
            with self.subTest(exit_code=exit_code):
                process, marker, gate = self.gated_exit(
                    exit_code, f"darwin-delayed-proof-{exit_code}", release=False
                )
                actual_terminal = process._darwin_terminal_group
                denied, incomplete, complete = [], [], []
                proof_ready = threading.Event()
                before = time.monotonic()

                def terminal_group(deadline):
                    if denied and not proof_ready.is_set():
                        # Withhold one complete positive observation at the real
                        # post-error seam; never supply a synthetic success.
                        self.assertEqual(
                            native_retained_exit(process.pid), (process.pid, 1, exit_code)
                        )
                        self.assertEqual(native_group_members(process.pid), {process.pid})
                        incomplete.append((deadline, time.monotonic()))
                        proof_ready.set()
                        return None
                    terminal = actual_terminal(deadline)
                    if not denied:
                        self.assertIsNone(terminal)
                        self.assertIsNone(native_retained_exit(process.pid))
                    elif terminal is not None:
                        complete.append((deadline, time.monotonic()))
                    return terminal

                def race_at_term(group, selected_signal):
                    if group == process.pid and selected_signal == signal.SIGTERM:
                        gate.touch()
                        self.assertEqual(
                            self.until(lambda: native_retained_exit(process.pid), budget=0.25),
                            (process.pid, 1, exit_code),
                        )
                        denied.append((group, selected_signal))
                        raise PermissionError(errno.EPERM, "Darwin delayed terminal proof")
                    return actual_killpg(group, selected_signal)

                with (
                    mock.patch.object(
                        process, "_darwin_terminal_group", side_effect=terminal_group
                    ),
                    mock.patch.object(os, "killpg", side_effect=race_at_term),
                ):
                    self.confirm_cleanup(process, marker, [process.pid])
                self.assertEqual(denied, [(process.pid, signal.SIGTERM)])
                self.assertEqual(len(incomplete), 1)
                self.assertTrue(complete)
                self.assertLess(complete[0][1], incomplete[0][0])
                self.assertLessEqual(complete[0][0], incomplete[0][0])
                receipt = process.cleanup_receipt
                self.assertEqual(receipt["budget_seconds"], 1.5)
                self.assertLess(time.monotonic() - before, 1.7)
                self.assertEqual(receipt["leader_exit"], exit_code)
                operations = receipt["group_signal_operations"]
                self.assertTrue(
                    any(
                        op["operation"] == "killpg"
                        and op["result"] == "unavailable"
                        and op["signal"] == signal.SIGTERM
                        and op["errno"] == errno.EPERM
                        for op in operations
                    ),
                    receipt,
                )
                self.assertTrue(
                    any(
                        op["operation"] == "terminal-group-proof"
                        and op["result"] == "terminal-membership-after-eperm"
                        and op["signal"] == signal.SIGTERM
                        for op in operations
                    ),
                    receipt,
                )
                self.assertEqual(native_group_members(process.pid), set())
                self.assertTrue(self.is_owned_live(peer.pid, peer_token))

    @unittest.skipUnless(sys.platform == "darwin", "Darwin terminal signal failure controls")
    def test_darwin_term_eperm_cannot_confirm_live_or_unavailable_terminal_membership(self) -> None:
        peer, peer_token = self.peer()
        posix = production("kernel._analysis_posix")
        actual_killpg, actual_members = os.killpg, posix._darwin_group_members
        for case in ("live", "denied-membership", "incomplete-membership"):
            with self.subTest(case=case):
                process, marker, gate = self.gated_exit(0, "darwin-negative-" + case, release=False)
                saved_marker = marker.read_bytes()
                armed, denied, membership_failures = threading.Event(), [], []

                def signal_failure(group, selected_signal):
                    if group == process.pid:
                        if case != "live" and not armed.is_set():
                            gate.touch()
                            self.assertEqual(
                                self.until(lambda: native_retained_exit(process.pid)),
                                (process.pid, 1, 0),
                            )
                            armed.set()
                        denied.append(selected_signal)
                        raise PermissionError(errno.EPERM, "Darwin owned signal unavailable")
                    return actual_killpg(group, selected_signal)

                def unavailable_members(group, deadline):
                    if group == process.pid and armed.is_set():
                        membership_failures.append(group)
                        if case == "denied-membership":
                            raise PermissionError(errno.EPERM, "Native terminal membership denied")
                        raise TimeoutError("Native terminal membership snapshot incomplete")
                    return actual_members(group, deadline)

                with (
                    mock.patch.object(os, "killpg", side_effect=signal_failure),
                    mock.patch.object(
                        posix, "_darwin_group_members", side_effect=unavailable_members
                    ),
                ):
                    self.assertFalse(
                        self.processes.terminate_process_group(process, grace_seconds=0.2)
                    )
                self.assertTrue(denied, "The actual signal phase must reach the EPERM fault")
                receipt = process.cleanup_receipt
                self.assertIs(receipt["scope_confirmed"], False)
                self.assertIs(receipt["leader_reaped"], False)
                self.assertTrue(receipt["uncertainties"])
                self.assertEqual(marker.read_bytes(), saved_marker)
                if case == "live":
                    self.assertTrue(self.is_owned_live(process.pid, self.observed[process.pid]))
                else:
                    self.assertTrue(
                        membership_failures, "Fresh terminal membership must reach the fault"
                    )
                    self.assertEqual(native_retained_exit(process.pid), (process.pid, 1, 0))
                self.assertTrue(self.is_owned_live(peer.pid, peer_token))

    @unittest.skipUnless(
        sys.platform == "darwin", "Darwin scoped terminal proof excludes unknown descendants"
    )
    def test_darwin_fresh_terminal_proof_cannot_erase_prior_captured_identity_failure(self) -> None:
        peer, peer_token = self.peer()
        process, marker, directory, ready = self.tree(detach=True)
        child, token = ready["child"], self.observed[ready["child"]]
        self.until(lambda: any(pid == child for pid, _ in tuple(process._captured)))
        actual_birth, actual_killpg = psutil.Process.create_time, os.killpg
        denied_birth, denied_signal = [], []

        def inaccessible_child(candidate):
            if candidate.pid == child:
                denied_birth.append(child)
                raise psutil.AccessDenied(child)
            return actual_birth(candidate)

        def exit_at_term(group, selected_signal):
            if group == process.pid and selected_signal == signal.SIGTERM:
                (directory / "exit").touch()
                self.assertEqual(
                    self.until(lambda: native_retained_exit(process.pid)), (process.pid, 1, 0)
                )
                denied_signal.append(selected_signal)
                raise PermissionError(errno.EPERM, "Darwin leader became terminal before TERM")
            return actual_killpg(group, selected_signal)

        with (
            mock.patch.object(psutil.Process, "create_time", inaccessible_child),
            mock.patch.object(os, "killpg", side_effect=exit_at_term),
        ):
            self.assertFalse(self.processes.terminate_process_group(process, grace_seconds=0.2))
        self.assertTrue(denied_birth, "Actual captured identity observation must reach the fault")
        self.assertEqual(denied_signal, [signal.SIGTERM])
        receipt = process.cleanup_receipt
        self.assertIs(receipt["scope_confirmed"], False)
        self.assertIs(receipt["leader_reaped"], False)
        self.assertTrue(receipt["uncertainties"])
        self.assertEqual(receipt["leader_exit"], 0)
        self.assertIsNone(receipt["native_terminal_group"])
        self.assertEqual(native_group_members(process.pid), {process.pid})
        captured = {entry["pid"]: entry for entry in receipt["captured"]}
        self.assertEqual(captured[child]["state"], "unknown")
        self.assertTrue(self.is_owned_live(child, token))
        self.assertTrue(marker.exists())
        self.assertEqual(native_retained_exit(process.pid), (process.pid, 1, 0))
        self.assertTrue(self.is_owned_live(peer.pid, peer_token))

    def confirm_cleanup(self, process, marker, pids) -> None:
        before = time.monotonic()
        confirmed = self.processes.terminate_process_group(process, grace_seconds=0.5)
        self.assertLess(
            time.monotonic() - before, self.processes.MAX_OWNED_PROCESS_CLEANUP_SECONDS + 2
        )
        self.assertTrue(confirmed, getattr(process, "cleanup_receipt", None))
        receipt = process.cleanup_receipt
        self.assertIsInstance(receipt, dict)
        self.assertEqual(receipt["pid"], process.pid)
        self.assertEqual(receipt["group"], process.pid)
        self.assertEqual(receipt["start_token"], json.loads(marker.read_bytes())["start_token"])
        self.assertIs(receipt["confirmed"], True)
        self.assert_scoped_receipt(receipt)
        self.assertIs(receipt["leader_reaped"], True)
        self.assertIsNotNone(receipt["leader_exit"])
        self.assertEqual(receipt["uncertainties"], [])
        self.assertAlmostEqual(receipt["budget_seconds"], 3 * 0.5)
        self.assertLessEqual(receipt["elapsed_seconds"], receipt["budget_seconds"] + 0.2)
        captured = {entry["pid"]: entry for entry in receipt["captured"]}
        for pid in pids:
            if pid == process.pid:
                continue
            self.assertIn(pid, captured, "An observed owned descendant must remain in the receipt")
            self.assertTrue(captured[pid]["start_token"])
            self.assertIn(captured[pid]["state"], {"gone", "zombie", "reused"})
        self.assertIsNotNone(process.poll(), "Leader must be reaped separately from group proof")
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
        for pid in pids:
            self.assertFalse(
                self.is_owned_live(pid, self.observed[pid]),
                (pid, getattr(process, "cleanup_receipt", None)),
            )
        self.store.remove(marker)
        self.assertFalse(marker.exists())

    def test_normal_leader_exit_cleans_term_ignoring_descendant_and_preserves_peer(self) -> None:
        peer, peer_token = self.peer()
        process, marker, directory, ready = self.tree()
        (directory / "exit").touch()
        # Observe actual leader exit without reaping its retained native anchor.
        # The child inherits stdout, so group cleanup must happen before EOF.
        self.assertEqual(self.until(lambda: native_retained_exit(process.pid)), (process.pid, 1, 0))
        self.confirm_cleanup(process, marker, [ready["leader"], ready["child"]])
        self.assertEqual(process.cleanup_receipt["leader_exit"], 0)
        output, errors = process.communicate(timeout=2)
        self.assertIn(b"native-fixture-complete", output)
        self.assertEqual(errors, b"")
        self.assertTrue(self.is_owned_live(peer.pid, peer_token))

    def test_stale_child_listing_cannot_acquire_new_birth_with_foreign_parent(self) -> None:
        # Positive acquisition control: a previously observed detached child
        # forks a new descendant outside the leader's group. Its real ancestry
        # must grant ownership; group enumeration alone cannot discover it.
        control, control_marker, directory, ready = self.tree(detach=True)
        (directory / "fork-grandchild").touch()
        self.until(lambda: (directory / "grandchild-ready").exists())
        grandchild = int((directory / "grandchild-ready").read_text())
        self.observe(grandchild)
        self.assertEqual(psutil.Process(grandchild).ppid(), ready["child"])
        self.assertNotEqual(os.getpgid(grandchild), control.pid)
        self.assertNotEqual(os.getsid(grandchild), control.pid)
        self.confirm_cleanup(control, control_marker, [control.pid, ready["child"], grandchild])

        process, marker = self.spawn([sys.executable, "-c", "import time; time.sleep(60)"])
        leader_birth = psutil.Process(process.pid).create_time()
        # A stale global PPID map can name a PID whose current birth is newer
        # than the owned leader. Return that real new identity as children()
        # does; only fresh native ancestry distinguishes it from ownership.
        time.sleep(0.05)
        peer, peer_token = self.peer()
        proposal = psutil.Process(peer.pid)
        self.assertGreater(proposal.create_time(), leader_birth)
        self.assertEqual(proposal.ppid(), os.getpid())
        self.assertNotEqual(proposal.ppid(), process.pid)
        self.assertEqual(os.getpgid(peer.pid), peer.pid)
        self.assertEqual(os.getsid(peer.pid), peer.pid)
        actual_children, actual_kill, actual_killpg = psutil.Process.children, os.kill, os.killpg
        listed = threading.Event()
        forbidden_signals = []

        def stale_children(parent, recursive=False):
            children = actual_children(parent, recursive=recursive)
            if parent.pid == process.pid:
                listed.set()
                return [*children, proposal]
            return children

        def guard_pid(pid, sig):
            if pid == peer.pid:
                forbidden_signals.append(("pid", pid, sig))
                raise PermissionError("test guards the unrelated peer")
            return actual_kill(pid, sig)

        def guard_group(group, sig):
            if group == peer.pid:
                forbidden_signals.append(("group", group, sig))
                raise PermissionError("test guards the unrelated peer group")
            return actual_killpg(group, sig)

        with (
            mock.patch.object(psutil.Process, "children", stale_children),
            mock.patch.object(os, "kill", side_effect=guard_pid),
            mock.patch.object(os, "killpg", side_effect=guard_group),
        ):
            self.assertTrue(
                listed.wait(5), "The live ownership observer must see the stale listing"
            )
            confirmed = self.processes.terminate_process_group(process, grace_seconds=0.5)
        receipt = process.cleanup_receipt
        self.assertNotIn(peer.pid, {entry["pid"] for entry in receipt["captured"]}, receipt)
        self.assertEqual(forbidden_signals, [], "Foreign birth must never gain signal authority")
        self.assertTrue(self.is_owned_live(peer.pid, peer_token))
        self.assertTrue(confirmed, receipt)
        self.assertIs(receipt["leader_reaped"], True)
        self.assertEqual(receipt["uncertainties"], [])
        self.store.remove(marker)
        self.assertFalse(marker.exists())

    def test_retired_or_absent_ancestry_proposals_grant_nothing_without_uncertainty(self) -> None:
        posix = production("kernel._analysis_posix")
        directory = self.base / "ancestry-parent"
        directory.mkdir()
        parent = subprocess.Popen(
            [sys.executable, "-c", PARENT, str(directory)],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.owned.append((parent, None))
        self.addCleanup((directory / "exit-parent").touch)
        self.addCleanup((directory / "exit").touch)
        parent_token = f"psutil:{psutil.Process(parent.pid).create_time():.9f}"
        self.observe(parent.pid)
        self.until(lambda: (directory / "child-ready").exists())
        child = int((directory / "child-ready").read_text())
        self.observe(child)
        child_token = f"psutil:{psutil.Process(child).create_time():.9f}"
        deadline = time.monotonic() + 5
        identity = (parent.pid, parent_token)
        # Positive control: fresh exact ancestry through a live owned parent.
        self.assertEqual(posix._descendant_chain(child, identity, deadline), {(child, child_token)})
        for state in ("gone", "reused"):
            with self.subTest(parent_state=state):
                # A positively retired parent cannot authorize acquisition, and
                # its retirement is not uncertain ownership.
                self.assertEqual(
                    posix._descendant_chain(
                        child, identity, deadline, parent_state=lambda state=state: state
                    ),
                    set(),
                )
        with self.assertRaisesRegex(RuntimeError, "Owned parent identity lost"):
            posix._descendant_chain(child, identity, deadline, parent_state=lambda: "unknown")
        (directory / "exit").touch()
        self.until(lambda: (directory / "reaped").exists())
        self.assertEqual(posix._process_state(child, child_token), "gone")
        # The stale proposal is now absent; it grants nothing and never raises.
        self.assertEqual(posix._descendant_chain(child, identity, deadline), set())

    def test_churning_short_lived_descendants_keep_scoped_cleanup_confirmed(self) -> None:
        for exit_first in (True, False):
            with self.subTest(exit_first=exit_first):
                directory = self.base / f"churn-{exit_first}"
                directory.mkdir()
                process, marker = self.spawn([sys.executable, "-c", CHURN, str(directory)])
                self.until(lambda: (directory / "churning").exists())
                # Let the live observer race real retirement and reparenting.
                time.sleep(0.5)
                self.assertIsNone(process.poll())
                if exit_first:
                    (directory / "exit").touch()
                    self.assertEqual(
                        self.until(lambda: native_retained_exit(process.pid)),
                        (process.pid, 1, 0),
                    )
                confirmed = self.processes.terminate_process_group(process, grace_seconds=0.5)
                receipt = process.cleanup_receipt
                self.assertEqual(receipt["uncertainties"], [], receipt)
                self.assertTrue(confirmed, receipt)
                self.assert_scoped_receipt(receipt)
                self.assertIs(receipt["leader_reaped"], True)
                if exit_first:
                    self.assertEqual(receipt["leader_exit"], 0)
                self.store.remove(marker)

    def test_captured_detached_descendant_cannot_survive_normal_exit(self) -> None:
        peer, peer_token = self.peer()
        process, marker, directory, ready = self.tree(detach=True)
        (directory / "exit").touch()
        self.assertEqual(self.until(lambda: native_retained_exit(process.pid)), (process.pid, 1, 0))
        self.confirm_cleanup(process, marker, [ready["leader"], ready["child"]])
        self.assertEqual(process.cleanup_receipt["leader_exit"], 0)
        self.assertTrue(self.is_owned_live(peer.pid, peer_token))

    def test_natural_eof_retains_exact_exit_proof_until_owned_cleanup(self) -> None:
        for exit_code in (0, 23):
            with self.subTest(exit_code=exit_code):
                gate = self.base / ("eof-release-" + str(exit_code))
                process, marker = self.spawn(
                    [
                        sys.executable,
                        "-c",
                        "import pathlib,sys,time; gate=pathlib.Path(sys.argv[1]);\nwhile not gate.exists(): time.sleep(.01)\nprint('natural eof'); sys.exit(int(sys.argv[2]))",
                        str(gate),
                        str(exit_code),
                    ]
                )
                token = self.observed[process.pid]
                gate.touch()
                output, errors = process.communicate(timeout=5)
                self.assertEqual(output, b"natural eof\n")
                self.assertEqual(errors, b"")
                self.assertEqual(process.returncode, exit_code)
                # Exit observation must keep the exact native child available as
                # an unreaped group anchor until the real cleanup proof completes.
                self.assertEqual(native_retained_exit(process.pid), (process.pid, 1, exit_code))
                if sys.platform.startswith("linux"):
                    self.assertEqual(posix_process_identity(process.pid), (token, "Z"))
                # Darwin proc_pidinfo without its zombie option can return
                # None here. Repeated exact WNOWAIT, rather than missing live
                # metadata, proves this actual direct child remains unreaped.
                self.assertEqual(native_retained_exit(process.pid), (process.pid, 1, exit_code))
                self.confirm_cleanup(process, marker, [process.pid])
                self.assertEqual(process.cleanup_receipt["leader_exit"], exit_code)

    def test_deadline_and_cancellation_of_actual_version_probe_retire_exact_owned_process(
        self,
    ) -> None:
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                gate = self.base / ("cancel-ready" if cancel else "deadline-ready")
                spawned = []
                actual = self.runtime.popen_owned
                stop = threading.Event()

                def capture(argv, **kwargs):
                    result = actual(
                        [
                            sys.executable,
                            "-c",
                            "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(60)",
                            str(gate),
                        ],
                        **kwargs,
                    )
                    spawned.append(result)
                    self.owned.append(result)
                    self.observe(result[0].pid)
                    return result

                def cancel_when_ready():
                    deadline = time.monotonic() + 5
                    while not gate.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if gate.exists():
                        stop.set()

                thread = threading.Thread(target=cancel_when_ready) if cancel else None
                if thread:
                    thread.start()
                candidate = self.runtime.ResolvedAnalysisProgram(
                    "clangd", Path(sys.executable), origin="path", identity=("native-probe", cancel)
                )
                with (
                    mock.patch.object(self.runtime, "popen_owned", side_effect=capture),
                    mock.patch.object(self.runtime, "ProcessMarkerStore", return_value=self.store),
                ):
                    with self.assertRaises(TimeoutError):
                        self.runtime._probe_program(
                            candidate, time.monotonic() + (5 if cancel else 2), stop
                        )
                if thread:
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                self.assertTrue(gate.exists(), "The actual native probe must start before expiry")
                self.assertEqual(len(spawned), 1)
                process, marker = spawned[0]
                self.until(lambda: process.poll() is not None and not marker.exists())
                self.assertIsNotNone(process.poll())
                self.assertFalse(self.is_owned_live(process.pid, self.observed[process.pid]))
                self.assertFalse(marker.exists())
                self.assertIs(process.cleanup_receipt["confirmed"], True)
                self.assertIs(process.cleanup_receipt["leader_reaped"], True)
                self.assertFalse(self.runtime._probe_cache)

    def test_late_spawn_is_cleaned_after_caller_abandons_admission(self) -> None:
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        actual = self.runtime.popen_owned
        spawned = []

        def delayed(argv, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            result = actual(argv, **kwargs)
            self.owned.append(result)
            self.observe(result[0].pid)
            spawned.append(result)
            completed.set()
            return result

        try:
            with mock.patch.object(self.runtime, "popen_owned", side_effect=delayed):
                with self.assertRaises(TimeoutError):
                    self.runtime.spawn_before_deadline(
                        [sys.executable, "-c", "import time; time.sleep(60)"],
                        time.monotonic() + 0.15,
                        None,
                        self.store,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                self.assertTrue(entered.is_set())
                release.set()
                self.assertTrue(completed.wait(5))
                process, marker = spawned[0]
                self.until(lambda: process.poll() is not None and not marker.exists())
                self.assertFalse(self.is_owned_live(process.pid, self.observed[process.pid]))
                self.assertIsNone(self.runtime._spawn_cleanup_failure)
                self.assertIs(process.cleanup_receipt["confirmed"], True)
                following, following_marker = self.spawn(
                    [sys.executable, "-c", "import time; time.sleep(60)"]
                )
                self.assertIsNot(following, process)
                self.confirm_cleanup(following, following_marker, [following.pid])
        finally:
            release.set()

    def test_actual_unconfirmed_late_cleanup_retains_marker_and_refuses_new_spawn(self) -> None:
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        actual = self.runtime.popen_owned
        spawned = []

        def delayed(argv, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            result = actual(argv, **kwargs)
            self.owned.append(result)
            self.observe(result[0].pid)
            spawned.append(result)
            completed.set()
            return result

        original_killpg = os.killpg

        def unavailable_group_signal(group, sig):
            if spawned and group == spawned[0][0].pid and sig:
                raise PermissionError("injected owned group signal denial")
            return original_killpg(group, sig)

        try:
            with (
                mock.patch.object(self.runtime, "popen_owned", side_effect=delayed) as spawn,
                mock.patch.object(os, "killpg", side_effect=unavailable_group_signal),
            ):
                with self.assertRaises(TimeoutError):
                    self.runtime.spawn_before_deadline(
                        [sys.executable, "-c", "import time; time.sleep(60)"],
                        time.monotonic() + 0.15,
                        None,
                        self.store,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                self.assertTrue(entered.is_set())
                release.set()
                self.assertTrue(completed.wait(5))
                self.until(lambda: self.runtime._spawn_cleanup_failure is not None)
                process, marker = spawned[0]
                self.assertIs(process.cleanup_receipt["confirmed"], False)
                self.assertTrue(process.cleanup_receipt["uncertainties"])
                self.assertTrue(marker.exists())
                saved = json.loads(marker.read_bytes())
                self.assertEqual(saved["pid"], process.pid)
                self.assertEqual(saved["start_token"], process.cleanup_receipt["start_token"])
                with self.assertRaisesRegex(RuntimeError, "termination unconfirmed"):
                    self.runtime.spawn_before_deadline(
                        [sys.executable, "-c", "raise SystemExit('must not spawn')"],
                        time.monotonic() + 2,
                        None,
                        self.store,
                    )
                self.assertEqual(spawn.call_count, 1)
        finally:
            release.set()

    def test_actual_setup_exception_admission_depends_on_cleanup_for_timely_and_abandoned_callers(
        self,
    ):
        self.addCleanup(setattr, self.runtime, "_spawn_cleanup_failure", None)
        self.addCleanup(setattr, self.runtime, "_spawn_cleanup_receipt", None)
        peer, peer_token = self.peer()
        actual_spawn, actual_create = self.runtime.popen_owned, self.store.create
        actual_killpg = os.killpg
        for outcome in ("unconfirmed", "confirmed", "pre-spawn"):
            for abandoned in (False, True):
                with self.subTest(outcome=outcome, abandoned=abandoned):
                    entered, release, completed = (
                        threading.Event(),
                        threading.Event(),
                        threading.Event(),
                    )
                    children, markers, errors, signals = [], [], [], []
                    self.runtime._spawn_cleanup_failure = None
                    self.runtime._spawn_cleanup_receipt = None

                    def fail_initial_marker(process, argv):
                        if not children:
                            # Record the actual Popen before any fallible identity assertion.
                            children.append(process)
                            self.owned.append((process, None))
                            token = self.observe(process.pid)
                            self.assertEqual(os.getsid(process.pid), process.pid)
                            self.assertEqual(os.getpgid(process.pid), process.pid)
                            self.assertIsNone(native_retained_exit(process.pid))
                            self.assertTrue(self.is_owned_live(process.pid, token))
                            raise OSError("actual analysis marker setup failed")
                        self.assertIs(process, children[0])
                        marker = actual_create(process, argv)
                        markers.append(marker)
                        return marker

                    def setup_exception(argv, **kwargs):
                        entered.set()
                        try:
                            if abandoned:
                                self.assertTrue(release.wait(5))
                            if outcome == "pre-spawn":
                                raise OSError("ordinary pre-spawn failure")
                            return actual_spawn(argv, **kwargs)
                        except BaseException as exc:
                            errors.append(exc)
                            raise
                        finally:
                            completed.set()

                    def denied_cleanup(group, selected_signal):
                        if outcome == "unconfirmed" and children and group == children[0].pid:
                            signals.append((group, selected_signal))
                            raise PermissionError(errno.EPERM, "actual owned setup cleanup denied")
                        return actual_killpg(group, selected_signal)

                    try:
                        with (
                            mock.patch.object(
                                self.store, "create", side_effect=fail_initial_marker
                            ),
                            mock.patch.object(
                                self.runtime, "popen_owned", side_effect=setup_exception
                            ) as spawn,
                            mock.patch.object(os, "killpg", side_effect=denied_cleanup),
                        ):
                            expected = TimeoutError if abandoned else OSError
                            with self.assertRaises(expected) as delivered:
                                self.runtime.spawn_before_deadline(
                                    [sys.executable, "-c", "import time; time.sleep(60)"],
                                    time.monotonic() + (0.15 if abandoned else 5),
                                    None,
                                    self.store,
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                )
                            self.assertTrue(entered.is_set())
                            release.set()
                            self.assertTrue(completed.wait(5))
                            self.until(lambda: not self.runtime._spawn_admission.locked())
                            self.assertEqual(len(errors), 1)
                            primary = errors[0]
                            self.assertIsInstance(primary, OSError)
                            if not abandoned:
                                self.assertIs(delivered.exception, primary)
                            if outcome == "pre-spawn":
                                self.assertFalse(children)
                                self.assertFalse(hasattr(primary, "cleanup_receipt"))
                            else:
                                self.assertEqual(len(children), 1)
                                process = children[0]
                                receipt = primary.cleanup_receipt
                                self.assertEqual(receipt, process.cleanup_receipt)
                                self.assertEqual(receipt["pid"], process.pid)
                                self.assertEqual(receipt["group"], process.pid)
                                self.assertEqual(
                                    receipt["coverage"], "retained_group_and_captured_descendants"
                                )
                                self.assertEqual(
                                    receipt["unobserved_detached_descendants"], "not_proven"
                                )
                                if outcome == "unconfirmed":
                                    self.assertIs(receipt["scope_confirmed"], False)
                                    self.assertIs(receipt["leader_reaped"], False)
                                    self.assertTrue(receipt["uncertainties"])
                                    self.assertTrue(signals)
                                    self.assertIsNone(native_retained_exit(process.pid))
                                    self.assertTrue(
                                        self.is_owned_live(process.pid, self.observed[process.pid])
                                    )
                                    self.assertEqual(len(markers), 1)
                                    saved = json.loads(markers[0].read_bytes())
                                    self.assertEqual(saved["pid"], process.pid)
                                    self.assertEqual(saved["start_token"], receipt["start_token"])
                                else:
                                    self.assert_scoped_receipt(receipt)
                                    self.assertEqual(receipt["leader_exit"], -signal.SIGTERM)
                                    self.assertIsNone(receipt["wait_error"])
                                    with self.assertRaises(ChildProcessError):
                                        native_retained_exit(process.pid)
                                    self.assertFalse(
                                        self.is_owned_live(process.pid, self.observed[process.pid])
                                    )
                                    self.assertFalse(markers)
                            if outcome == "unconfirmed":
                                self.assertIsNotNone(
                                    self.runtime._spawn_cleanup_failure, primary.cleanup_receipt
                                )
                                retained = self.runtime._spawn_cleanup_receipt
                                self.assertEqual(retained, primary.cleanup_receipt)
                                self.assertIsNot(retained, primary.cleanup_receipt)
                                self.assertIsNot(
                                    retained["uncertainties"],
                                    primary.cleanup_receipt["uncertainties"],
                                )
                                with self.assertRaises(RuntimeError) as refused:
                                    self.runtime.spawn_before_deadline(
                                        [
                                            sys.executable,
                                            "-c",
                                            "raise SystemExit('must not spawn')",
                                        ],
                                        time.monotonic() + 2,
                                        None,
                                        self.store,
                                    )
                                self.assertEqual(refused.exception.cleanup_receipt, retained)
                                self.assertIsNot(refused.exception.cleanup_receipt, retained)
                                self.assertEqual(
                                    spawn.call_count, 1, "Refuse before another actual Popen"
                                )
                            else:
                                self.assertIsNone(self.runtime._spawn_cleanup_failure)
                                self.assertIsNone(self.runtime._spawn_cleanup_receipt)
                            self.assertTrue(self.is_owned_live(peer.pid, peer_token))
                        if outcome != "unconfirmed":
                            following, marker = self.spawn(
                                [sys.executable, "-c", "import time; time.sleep(60)"]
                            )
                            self.confirm_cleanup(following, marker, [following.pid])
                            self.assertTrue(self.is_owned_live(peer.pid, peer_token))
                    finally:
                        # Restore fault seams before exact-identity fixture cleanup.
                        release.set()
                        self.assertTrue(completed.wait(5))
                        for process in children:
                            token = self.observed.get(process.pid)
                            killed = False
                            if token and self.is_owned_live(process.pid, token):
                                os.kill(process.pid, signal.SIGKILL)
                                killed = True
                                self.assertEqual(
                                    self.until(lambda: native_retained_exit(process.pid)),
                                    (process.pid, 2, signal.SIGKILL),
                                )
                            try:
                                retained_exit = native_retained_exit(process.pid)
                            except ChildProcessError:
                                retained_exit = None
                            else:
                                self.assertIsNotNone(
                                    retained_exit, "Do not retire a live/unknown fixture marker"
                                )
                                waited, status = os.waitpid(process.pid, 0)
                                self.assertEqual(waited, process.pid)
                                if killed:
                                    self.assertEqual(
                                        os.waitstatus_to_exitcode(status), -signal.SIGKILL
                                    )
                            for marker in markers:
                                self.store.remove(marker)
                        self.runtime._spawn_cleanup_failure = None
                        self.runtime._spawn_cleanup_receipt = None

    @unittest.skipUnless(sys.platform == "darwin", "Native Darwin terminal group shrink")
    def test_terminal_group_shrink_by_actual_parent_reap_requires_fresh_stable_native_proof(self):
        peer, peer_token = self.peer()
        posix = production("kernel._analysis_posix")
        script = r"""
import json, os, pathlib, sys, time
base = pathlib.Path(sys.argv[1])
def gate(name):
    while not (base / name).exists(): time.sleep(.005)
reaper = os.fork()
if reaper == 0:
    member = os.fork()
    if member == 0:
        gate('member-exit')
        os._exit(0)
    os.setpgid(0, 0)
    (base / 'ready.tmp').write_text(json.dumps({'reaper': os.getpid(), 'member': member}))
    os.replace(base / 'ready.tmp', base / 'ready')
    gate('reap-member')
    pid, status = os.waitpid(member, 0)
    (base / 'reaped.tmp').write_text(json.dumps({'pid': pid, 'exit': os.waitstatus_to_exitcode(status)}))
    os.replace(base / 'reaped.tmp', base / 'reaped')
    os._exit(0)
gate('leader-exit')
os._exit(0)
"""
        for seam in ("between-lists", "before-birth"):
            with self.subTest(seam=seam):
                directory = self.base / ("terminal-shrink-" + seam)
                directory.mkdir()
                process, marker = self.spawn([sys.executable, "-c", script, str(directory)])
                self.until(lambda: (directory / "ready").exists())
                ready = json.loads((directory / "ready").read_text())
                member, reaper = ready["member"], ready["reaper"]
                member_token = self.observe(member)
                reaper_token = self.observe(reaper)
                self.assertEqual(os.getpgid(member), process.pid)
                self.assertEqual(os.getpgid(reaper), reaper)
                process._stop_observer.set()
                process._observer.join(0.5)
                self.assertFalse(
                    process._observer.is_alive(), "Isolate the actual proof invocation"
                )
                self.assertTrue(process._capture(time.monotonic() + 0.5))
                captured_tokens = [token for pid, token in process._captured if pid == member]
                self.assertEqual(
                    len(captured_tokens), 1, "Unambiguous existing exact member identity"
                )
                self.assertTrue(captured_tokens[0])
                (directory / "member-exit").touch()
                self.until(lambda: posix_process_identity(member) == (member_token, "Z"))
                (directory / "leader-exit").touch()
                self.assertEqual(
                    self.until(lambda: native_retained_exit(process.pid)), (process.pid, 1, 0)
                )
                actual_members = posix._darwin_group_members
                snapshots, invocations = [], []
                proof_thread = threading.get_ident()
                proof_deadline = time.monotonic() + 0.5
                armed = False

                def reap_exact_terminal_member():
                    self.assertEqual(posix_process_identity(member), (member_token, "Z"))
                    (directory / "reap-member").touch()
                    self.until(
                        lambda: (directory / "reaped").exists(),
                        budget=min(0.2, max(0, proof_deadline - time.monotonic())),
                    )
                    self.assertEqual(
                        json.loads((directory / "reaped").read_text()), {"pid": member, "exit": 0}
                    )
                    self.assertIsNone(posix_process_identity(member))
                    self.until(
                        lambda: not self.is_owned_live(reaper, reaper_token),
                        budget=min(0.2, max(0, proof_deadline - time.monotonic())),
                    )

                def membership_inside_one_proof(group, deadline):
                    if not armed or group != process.pid or threading.get_ident() != proof_thread:
                        return actual_members(group, deadline)
                    self.assertEqual(deadline, proof_deadline)
                    self.assertLess(time.monotonic(), deadline)
                    if seam == "between-lists" and len(snapshots) == 1:
                        reap_exact_terminal_member()
                    result = actual_members(group, deadline)
                    snapshots.append(set(result))
                    invocations.append((threading.get_ident(), group, deadline))
                    if seam == "before-birth" and len(snapshots) == 1:
                        # Return this actual complete list only after the real
                        # parent reaps; the following identity read must use the
                        # already captured exact birth, never invent a new one.
                        reap_exact_terminal_member()
                    return result

                with mock.patch.object(
                    posix, "_darwin_group_members", side_effect=membership_inside_one_proof
                ):
                    armed = True
                    try:
                        terminal = process._darwin_terminal_group(proof_deadline)
                    finally:
                        armed = False
                self.assertIsNotNone(terminal)
                self.assertEqual({entry["pid"] for entry in terminal}, {process.pid})
                self.assertTrue(all(entry["state"] == "zombie" for entry in terminal))
                self.assertEqual(snapshots[0], {process.pid, member})
                self.assertEqual(snapshots[1], {process.pid})
                self.assertGreaterEqual(
                    len(snapshots), 4, "Fresh stable native pair inside this proof"
                )
                self.assertTrue(all(snapshot == {process.pid} for snapshot in snapshots[1:]))
                self.assertTrue(
                    all(call == (proof_thread, process.pid, proof_deadline) for call in invocations)
                )
                self.assertLess(time.monotonic(), proof_deadline)
                self.assertEqual(native_group_members(process.pid), {process.pid})
                self.assertEqual(native_retained_exit(process.pid), (process.pid, 1, 0))
                self.assertTrue(marker.exists())
                self.confirm_cleanup(process, marker, [process.pid, member, reaper])
                self.assertEqual(native_group_members(process.pid), set())
                self.assertFalse(self.is_owned_live(member, member_token))
                self.assertFalse(self.is_owned_live(reaper, reaper_token))
                self.assertTrue(self.is_owned_live(peer.pid, peer_token))

    def test_registered_terminal_child_observer_failure_is_retired_without_rewriting_failure(self):
        from tests import code_analysis_managed_support as support

        peer, peer_token = self.peer()
        posix = production("kernel._analysis_posix")
        actual_token = support.native_start_token
        for case in ("healthy", "unavailable-wait"):
            with self.subTest(case=case):
                gate = self.base / ("observer-exit-" + case)
                observed = support.OwnedSpawns(self.base / ("observer-failure-" + case))
                captured = []
                with ExitStack() as faults:

                    def fail_after_registration(pid):
                        if not captured and observed.children:
                            process, marker, record = observed.children[0]
                            self.assertEqual(process.pid, pid)
                            original_receipt = json.loads(json.dumps(process.cleanup_receipt))
                            self.assertEqual(original_receipt, {})
                            record["original_cleanup_receipt"] = original_receipt
                            captured.append((process, marker))
                            self.owned.append((process, marker))
                            self.observe(pid)
                            self.assertTrue(marker.exists())
                            gate.touch()
                            self.assertEqual(
                                self.until(lambda: native_retained_exit(pid)), (pid, 1, 0)
                            )
                            self.assertEqual(process.poll(), 0)
                            if case == "unavailable-wait":
                                faults.enter_context(
                                    mock.patch.object(
                                        posix,
                                        "_exit_status",
                                        side_effect=PermissionError(
                                            errno.EPERM, "authoritative wait unavailable"
                                        ),
                                    )
                                )
                            raise OSError("independent observer failed after registration")
                        return actual_token(pid)

                    faults.enter_context(
                        mock.patch.object(
                            support, "native_start_token", side_effect=fail_after_registration
                        )
                    )
                    before = time.monotonic()
                    with self.assertRaisesRegex(OSError, "independent observer failed"):
                        with observed:
                            observed.spawn(
                                [
                                    sys.executable,
                                    "-c",
                                    "import pathlib,sys,time; gate=pathlib.Path(sys.argv[1]);\nwhile not gate.exists(): time.sleep(.005)\nsys.exit(0)",
                                    str(gate),
                                ],
                                analysis_ownership=True,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                    self.assertLess(time.monotonic() - before, 2)
                self.assertEqual(len(captured), 1)
                process, marker = captured[0]
                persisted = json.loads((observed.directory / "processes.json").read_text())[0]
                self.assertEqual(persisted["pid"], process.pid)
                self.assertEqual(persisted["exit_code"], 0)
                self.assertIs(persisted["marker_exists_after"], True)
                self.assertEqual(persisted["cleanup_receipt"], {})
                self.assertEqual(
                    persisted["cleanup_receipt"],
                    persisted["original_cleanup_receipt"],
                    "Keep original pre-cleanup failure evidence",
                )
                self.assertTrue(process._stop_observer.is_set())
                self.assertFalse(process._observer.is_alive())
                receipt = process.cleanup_receipt
                if case == "healthy":
                    self.assert_scoped_receipt(receipt)
                    self.assertEqual(receipt["leader_exit"], 0)
                    with self.assertRaises(ChildProcessError):
                        native_retained_exit(process.pid)
                    self.assertFalse(marker.exists())
                    self.assertEqual(native_group_members(process.pid), set())
                else:
                    self.assertIs(receipt["scope_confirmed"], False)
                    self.assertIs(receipt["leader_reaped"], False)
                    self.assertTrue(marker.exists())
                    self.assertEqual(native_retained_exit(process.pid), (process.pid, 1, 0))
                    waited, status = os.waitpid(process.pid, os.WNOHANG)
                    self.assertEqual(waited, process.pid)
                    self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                    self.assertIs(receipt["scope_confirmed"], False)
                    observed.store.remove(marker)
                self.assertTrue(self.is_owned_live(peer.pid, peer_token))

    def test_marker_exact_start_mismatch_or_unknown_identity_never_signals_peer(self) -> None:
        peer, peer_token = self.peer()
        ownership_before = dict(self.observed)
        marker = self.store.create(peer, tuple(peer.args))
        self.assertIsNotNone(marker)
        marker_before = marker.read_bytes()
        for observation in (
            "mismatched-start",
            None,
            self.processes.ProcessIdentityUnavailable("native access denied"),
        ):
            with self.subTest(observation=observation):
                kwargs = (
                    {"side_effect": observation}
                    if isinstance(observation, Exception)
                    else {"return_value": observation}
                )
                with (
                    mock.patch.object(self.processes, "_start_token", **kwargs),
                    mock.patch.object(self.processes.os, "killpg") as group_signal,
                    mock.patch.object(self.processes.os, "kill") as pid_signal,
                ):
                    self.assertFalse(
                        self.processes.terminate_marked_group(peer.pid, "recorded-owned-start")
                    )
                # Signal 0 only queries existence; no uncertain identity grants
                # authority to send a mutating PID or process-group signal.
                for signal_call in (*group_signal.call_args_list, *pid_signal.call_args_list):
                    self.assertEqual(signal_call.args, (peer.pid, 0))
                    self.assertEqual(signal_call.kwargs, {})
                self.assertEqual(self.observed, ownership_before)
                self.assertEqual(marker.read_bytes(), marker_before)
                self.assertIsNone(peer.poll())
                self.assertTrue(self.is_owned_live(peer.pid, peer_token))

    def test_owned_group_signal_permission_error_is_unconfirmed_and_marker_retained(self) -> None:
        # A clean native control must confirm before fault injection. Cleanup
        # uses actual group signals; an unused killpg(group, 0) mock proved nothing.
        control, control_marker = self.spawn([sys.executable, "-c", "import time; time.sleep(60)"])
        self.confirm_cleanup(control, control_marker, [control.pid])
        process, marker, _, ready = self.tree()
        actual = os.killpg
        denied = []
        self.assertTrue(self.is_owned_live(ready["child"], self.observed[ready["child"]]))

        def unavailable_owned_signal(group, sig):
            if group == process.pid:
                denied.append(sig)
                raise PermissionError("owned group signal unavailable")
            return actual(group, sig)

        with mock.patch.object(os, "killpg", side_effect=unavailable_owned_signal):
            self.assertFalse(self.processes.terminate_process_group(process, grace_seconds=0.2))
        self.assertTrue(denied, "The actual owned signal phase must reach the injected denial")
        self.assertTrue(marker.exists(), "Unconfirmed signaling must retain recovery ownership")
        self.assertIs(process.cleanup_receipt["confirmed"], False)
        self.assertTrue(process.cleanup_receipt["uncertainties"])

    def test_captured_detached_live_child_with_unavailable_birth_stays_unconfirmed(self) -> None:
        process, marker, _, ready = self.tree(detach=True)
        child, token = ready["child"], self.observed[ready["child"]]
        self.assertTrue(self.is_owned_live(child, token))
        original = psutil.Process.create_time
        denied = []

        def inaccessible_birth(candidate):
            if candidate.pid == child:
                denied.append(candidate.pid)
                raise psutil.AccessDenied(candidate.pid)
            return original(candidate)

        # Fault only the real observed descendant's birth observation, after it
        # leaves the session. The independent native oracle remains available.
        with mock.patch.object(psutil.Process, "create_time", inaccessible_birth):
            self.assertFalse(self.processes.terminate_process_group(process, grace_seconds=0.2))
        self.assertTrue(denied, "Actual descendant identity observation must reach the fault")
        self.assertTrue(self.is_owned_live(child, token))
        self.assertTrue(marker.exists())
        receipt = process.cleanup_receipt
        self.assertIs(receipt["confirmed"], False)
        self.assertTrue(receipt["uncertainties"])
        captured = {entry["pid"]: entry for entry in receipt["captured"]}
        self.assertIn(child, captured)
        self.assertEqual(captured[child]["state"], "unknown")

    def test_recovery_requires_exact_session_group_and_rechecks_birth_before_escalation(
        self,
    ) -> None:
        peer, peer_token = self.peer()
        for wrong in ("group", "session"):
            with (
                self.subTest(wrong=wrong),
                mock.patch.object(self.processes, "_start_token", return_value="expected-birth"),
                mock.patch.object(os, "getpgid", return_value=peer.pid + (wrong == "group")),
                mock.patch.object(os, "getsid", return_value=peer.pid + (wrong == "session")),
                mock.patch.object(os, "killpg") as group_signal,
                mock.patch.object(os, "kill") as pid_signal,
            ):
                self.assertFalse(self.processes.terminate_marked_group(peer.pid, "expected-birth"))
                group_signal.assert_not_called()
                pid_signal.assert_not_called()
        signaled = False

        def birth(pid):
            return "recycled-birth" if signaled else "expected-birth"

        def signal_group(group, sig):
            nonlocal signaled
            if sig:
                self.assertEqual(sig, signal.SIGTERM, "Reused PID must never receive SIGKILL")
                signaled = True

        with (
            mock.patch.object(self.processes, "_start_token", side_effect=birth),
            mock.patch.object(os, "getpgid", return_value=peer.pid),
            mock.patch.object(os, "getsid", return_value=peer.pid),
            mock.patch.object(os, "killpg", side_effect=signal_group) as group_signal,
        ):
            self.assertFalse(self.processes.terminate_marked_group(peer.pid, "expected-birth"))
        self.assertTrue(signaled, "A matching initial identity is the positive signal control")
        self.assertEqual(
            [call.args[1] for call in group_signal.call_args_list if call.args[1]], [signal.SIGTERM]
        )
        self.assertTrue(self.is_owned_live(peer.pid, peer_token))
