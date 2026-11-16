"""Actual native Python lifecycle contracts for ROOT's matching-host Actions.

No analyzer/header fixture is executed. Linux /proc and macOS libproc identify
each observed PID/start independently of production psutil ownership tokens.
Windows Job/hardware contracts remain in their original modules.
"""

from __future__ import annotations
from tests.code_analysis_managed_support import native_retained_exit

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


from tests.code_analysis_managed_support import posix_process_identity, production


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
        self.admission_threads = []
        actual_thread = threading.Thread

        def owned_thread(*args, **kwargs):
            thread = actual_thread(*args, **kwargs)
            target = kwargs.get("target")
            if (
                kwargs.get("name") == "analysis-spawn"
                and getattr(target, "__module__", None) == self.runtime.__name__
            ):
                self.admission_threads.append(thread)
            return thread

        self.enterContext(mock.patch.object(self.runtime.threading, "Thread", owned_thread))
        self.addCleanup(self.emergency_cleanup)
        # These actual fixture-owned handles must finish before the patched
        # admission lock is restored, including after a failed assertion.
        self.addCleanup(self.quiesce_admissions)

    def quiesce_admissions(self, deadline: float | None = None) -> None:
        deadline = time.monotonic() + 5 if deadline is None else deadline
        events = []
        for thread in self.admission_threads:
            if thread.ident is not None:
                thread.join(max(0, deadline - time.monotonic()))
            alive = thread.is_alive()
            events.append(
                {
                    "boundary": "fixture-owned admission thread completion",
                    "thread_name": thread.name,
                    "thread_ident": thread.ident,
                    "native_id": thread.native_id,
                    "started": thread.ident is not None,
                    "alive_after_join": alive,
                }
            )
        evidence = os.environ.get("BYO_FINAL_CLEANUP_EVIDENCE")
        if evidence and events:
            path = Path(evidence) / (self._testMethodName + ".admission-threads.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps({"test": self.id(), "at": time.monotonic(), "events": events}) + "\n"
                )
        self.assertFalse(
            any(event["alive_after_join"] for event in events),
            "Owned admission/late cleanup exceeded the fixture barrier: " + repr(events),
        )

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

    def assert_scoped_receipt(self, receipt) -> None:
        self.assertEqual(receipt["coverage"], "retained_group_and_captured_descendants")
        self.assertIs(receipt["scope_confirmed"], True)
        self.assertEqual(receipt["unobserved_detached_descendants"], "not_proven")
        self.assertIs(receipt["leader_reaped"], True)
        self.assertEqual(receipt["uncertainties"], [])

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

    def test_deadline_and_cancellation_of_actual_version_probe_retire_exact_owned_process(
        self,
    ) -> None:
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                gate = self.base / ("cancel-ready" if cancel else "deadline-ready")
                spawned = []
                actual = self.runtime.popen_owned
                stop = threading.Event()
                admitted = threading.Event()

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
                    admitted.set()
                    return result

                def cancel_when_ready():
                    deadline = time.monotonic() + 5
                    while not (gate.exists() and admitted.is_set()) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if gate.exists() and admitted.is_set():
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
                    try:
                        with self.assertRaises(TimeoutError):
                            self.runtime._probe_program(
                                candidate, time.monotonic() + (5 if cancel else 2), stop
                            )
                    finally:
                        # Share the existing five-second fixture barrier; a
                        # child's ready file alone does not complete admission.
                        cleanup_deadline = time.monotonic() + 5
                        try:
                            if thread:
                                thread.join(max(0, cleanup_deadline - time.monotonic()))
                                self.assertFalse(thread.is_alive())
                        finally:
                            self.quiesce_admissions(cleanup_deadline)
                self.assertTrue(gate.exists(), "The actual native probe must start before expiry")
                self.assertTrue(admitted.is_set(), "Actual ownership admission must be recorded")
                if cancel:
                    self.assertTrue(stop.is_set(), "Cancel only the admitted live probe")
                self.assertEqual(len(spawned), 1)
                process, marker = spawned[0]
                self.until(
                    lambda: process.poll() is not None and not marker.exists(),
                    budget=max(0, cleanup_deadline - time.monotonic()),
                )
                self.assertIsNotNone(process.poll())
                self.assertFalse(self.is_owned_live(process.pid, self.observed[process.pid]))
                self.assertFalse(marker.exists())
                self.assertIs(process.cleanup_receipt["confirmed"], True)
                self.assertIs(process.cleanup_receipt["leader_reaped"], True)
                self.assertFalse(self.runtime._probe_cache)

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
