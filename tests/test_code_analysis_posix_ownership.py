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

    def test_unavailable_exact_retained_exit_never_confirms_or_removes_marker(self) -> None:
        peer, peer_token = self.peer()
        posix = production("kernel._analysis_posix")
        actual_exit = posix._exit_status
        for exit_code in (0, 23):
            with self.subTest(exit_code=exit_code):
                process, marker, gate = self.gated_exit(
                    exit_code, f"unknown-exit-{exit_code}", release=False
                )
                saved_marker = marker.read_bytes()
                denied = []

                def unavailable_exit(pid):
                    if pid == process.pid and native_retained_exit(pid) is not None:
                        denied.append(pid)
                        raise PermissionError(errno.EPERM, "exact retained exit unavailable")
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
                self.assertIs(receipt["leader_reaped"], False)
                self.assertIsNone(receipt["leader_exit"], "Never invent exit zero from errno")
                self.assertTrue(receipt["uncertainties"])
                self.assertEqual(marker.read_bytes(), saved_marker)
                self.assertEqual(native_retained_exit(process.pid), (process.pid, 1, exit_code))
                self.assertFalse(self.is_owned_live(process.pid, self.observed[process.pid]))
                self.assertTrue(self.is_owned_live(peer.pid, peer_token))

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

    def test_already_exited_before_begin_keeps_initial_observation_gap_unconfirmed(self) -> None:
        peer, peer_token = self.peer()
        posix = production("kernel._analysis_posix")
        actual_begin = posix.AnalysisPopen.begin_ownership
        for exit_code in (0, 23):
            with self.subTest(exit_code=exit_code):
                began_after_exit = []

                def begin_after_native_exit(process):
                    # Exercise the real startup boundary, never manufacture a
                    # receipt or use a delayed spawn as evidence of this race.
                    self.owned.append((process, None))
                    proof = self.until(lambda: native_retained_exit(process.pid))
                    self.assertEqual(proof, (process.pid, 1, exit_code))
                    began_after_exit.append(proof)
                    with self.unavailable_leader_image(process.pid, metadata=False, session=True):
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
                self.assertIsNotNone(marker)
                saved_marker = marker.read_bytes()
                output, errors = process.communicate(timeout=5)
                self.assertEqual((output, errors), (b"startup exit\n", b""))
                self.assertEqual(process.returncode, exit_code)
                with self.unavailable_leader_image(process.pid, metadata=True, session=True):
                    self.assertFalse(
                        self.processes.terminate_process_group(process, grace_seconds=0.2)
                    )
                receipt = process.cleanup_receipt
                self.assertEqual(receipt["leader_exit"], exit_code)
                self.assertIs(receipt["leader_reaped"], False)
                self.assertIs(receipt["confirmed"], False)
                self.assertIn(
                    "leader exited before initial descendant observation", receipt["uncertainties"]
                )
                self.assertEqual(marker.read_bytes(), saved_marker)
                self.assertEqual(native_retained_exit(process.pid), (process.pid, 1, exit_code))
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
        self.until(lambda: (identity := posix_process_identity(process.pid)) and identity[1] == "Z")
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

    def test_captured_detached_descendant_cannot_survive_normal_exit(self) -> None:
        peer, peer_token = self.peer()
        process, marker, directory, ready = self.tree(detach=True)
        (directory / "exit").touch()
        self.until(lambda: (identity := posix_process_identity(process.pid)) and identity[1] == "Z")
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
                retained = posix_process_identity(process.pid)
                self.assertEqual(retained, (token, "Z"))
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

    def test_marker_exact_start_mismatch_or_unknown_identity_never_signals_peer(self) -> None:
        peer, peer_token = self.peer()
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
                group_signal.assert_not_called()
                pid_signal.assert_not_called()
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
