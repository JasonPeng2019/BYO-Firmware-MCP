"""Actual native Python lifecycle contracts for ROOT's matching-host Actions.

No analyzer/header fixture is executed. Linux /proc and macOS libproc identify
each observed PID/start independently of production psutil ownership tokens.
Windows Job/hardware contracts remain in their original modules.
"""

from __future__ import annotations

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
    while True: time.sleep(.05)
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
            except subprocess.TimeoutExpired:
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

    def test_captured_detached_descendant_cannot_survive_normal_exit(self) -> None:
        peer, peer_token = self.peer()
        process, marker, directory, ready = self.tree(detach=True)
        (directory / "exit").touch()
        self.until(lambda: (identity := posix_process_identity(process.pid)) and identity[1] == "Z")
        self.confirm_cleanup(process, marker, [ready["leader"], ready["child"]])
        self.assertEqual(process.cleanup_receipt["leader_exit"], 0)
        self.assertTrue(self.is_owned_live(peer.pid, peer_token))

    def test_natural_eof_retains_exact_exit_proof_until_owned_cleanup(self) -> None:
        gate = self.base / "eof-release"
        process, marker = self.spawn(
            [
                sys.executable,
                "-c",
                "import pathlib,sys,time; gate=pathlib.Path(sys.argv[1]);\nwhile not gate.exists(): time.sleep(.01)\nprint('natural eof')",
                str(gate),
            ]
        )
        token = self.observed[process.pid]
        gate.touch()
        output, errors = process.communicate(timeout=5)
        self.assertEqual(output, b"natural eof\n")
        self.assertEqual(errors, b"")
        self.assertFalse(self.is_owned_live(process.pid, token))
        self.confirm_cleanup(process, marker, [process.pid])
        self.assertEqual(process.cleanup_receipt["leader_exit"], 0)

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

    def test_terminal_group_permission_error_is_unconfirmed_and_marker_retained(self) -> None:
        process, marker, directory, ready = self.tree()
        actual = os.killpg

        def unknown_terminal(group, sig):
            if group == process.pid and sig == 0:
                raise PermissionError("native terminal group proof unavailable")
            return actual(group, sig)

        with mock.patch.object(os, "killpg", side_effect=unknown_terminal):
            self.assertFalse(self.processes.terminate_process_group(process, grace_seconds=0.2))
        self.assertTrue(marker.exists(), "Unknown group identity must retain recovery ownership")
        self.assertIs(process.cleanup_receipt["confirmed"], False)
        self.assertTrue(process.cleanup_receipt["uncertainties"])
        self.confirm_cleanup(process, marker, [ready["leader"], ready["child"]])

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
