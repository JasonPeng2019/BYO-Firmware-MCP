from __future__ import annotations

import gc
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

import psutil

from pyocd_debug_mcp.kernel import processes


class WindowsProcessCleanupTests(unittest.TestCase):
    def test_exported_cleanup_budget_covers_every_cross_platform_phase(self) -> None:
        windows_two_phase_bound = 2 * processes.DEFAULT_PROCESS_GROUP_CLEANUP_GRACE_SECONDS
        posix_three_phase_bound = 3 * processes.DEFAULT_PROCESS_GROUP_CLEANUP_GRACE_SECONDS
        self.assertEqual(
            processes.MAX_OWNED_PROCESS_CLEANUP_SECONDS,
            max(windows_two_phase_bound, posix_three_phase_bound),
        )

    def test_job_zero_and_leader_reap_have_separate_bounded_phases(self) -> None:
        process = cast(Any, SimpleNamespace(pid=42, returncode=None))
        process.poll = Mock(return_value=None)

        def wait(*, timeout: float) -> int:
            self.assertAlmostEqual(timeout, 0.5)
            process.returncode = 1
            return 1

        process.wait = Mock(side_effect=wait)
        with (
            patch.object(processes.os, "name", "nt"),
            patch.dict(processes._WINDOWS_JOB_HANDLES, {42: 7}, clear=True),
            patch.object(processes.time, "monotonic", side_effect=[10.0, 10.5]),
            patch.object(processes, "_close_windows_job", return_value=True) as close_job,
        ):
            self.assertTrue(processes.terminate_process_group(process, grace_seconds=0.5))

        close_job.assert_called_once_with(42, 7, terminate=True, deadline=10.5)
        process.wait.assert_called_once()
        self.assertEqual(process.returncode, 1)

    def test_unreaped_leader_after_separate_reap_grace_is_unconfirmed(self) -> None:
        process = cast(Any, SimpleNamespace(pid=42, returncode=None))
        process.poll = Mock(return_value=None)
        process.wait = Mock(side_effect=subprocess.TimeoutExpired("worker", 0.5))
        with (
            patch.object(processes.os, "name", "nt"),
            patch.dict(processes._WINDOWS_JOB_HANDLES, {42: 7}, clear=True),
            patch.object(processes.time, "monotonic", side_effect=[10.0, 10.5]),
            patch.object(processes, "_close_windows_job", return_value=True),
        ):
            self.assertFalse(processes.terminate_process_group(process, grace_seconds=0.5))

        process.wait.assert_called_once_with(timeout=0.5)
        self.assertIsNone(process.returncode)

    @unittest.skipUnless(os.name == "nt", "Windows suspended-process behavior")
    def test_real_job_creation_failure_reaps_suspended_leader_without_marker(self) -> None:
        created: list[subprocess.Popen[Any]] = []
        real_popen = processes.subprocess.Popen

        def capture_process(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
            process = cast(subprocess.Popen[Any], real_popen(*args, **kwargs))
            created.append(process)
            return process

        primary = OSError("real Job creation failed")
        with (
            tempfile.TemporaryDirectory() as temporary,
            warnings.catch_warnings(record=True) as seen,
        ):
            warnings.simplefilter("always", ResourceWarning)
            store = processes.ProcessMarkerStore(Path(temporary))
            with (
                patch.object(processes.subprocess, "Popen", side_effect=capture_process),
                patch.object(processes, "_create_windows_kill_job", side_effect=primary),
            ):
                with self.assertRaises(OSError) as raised:
                    processes.popen_owned(
                        [sys.executable, "-c", "import time; time.sleep(60)"],
                        marker_store=store,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )

            self.assertIs(raised.exception, primary)
            self.assertEqual(len(created), 1)
            self.assertIsNotNone(created[0].returncode)
            self.assertIsNotNone(created[0].poll())
            self.assertEqual(list(Path(temporary).glob("*.json")), [])
            created.clear()
            gc.collect()
            self.assertFalse(
                any(issubclass(item.category, ResourceWarning) for item in seen),
                seen,
            )

    def test_unconfirmed_pre_resume_cleanup_retains_marker_and_primary(self) -> None:
        process = cast(
            Any,
            SimpleNamespace(pid=42, returncode=None),
        )
        marker = Path("suspended-leader-recovery.json")
        store = Mock()
        store.create.return_value = marker
        primary = OSError("Job assignment failed")
        # This fixture models a suspended Windows spawn on every test host.
        # POSIX subprocess omits these constants; the real group-options helper
        # has its own contract, so keep this mock focused on failed Job setup.
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | getattr(
            subprocess, "CREATE_SUSPENDED", 0x00000004
        )
        with (
            patch.object(processes.os, "name", "nt"),
            patch.object(
                processes, "process_group_options", return_value={"creationflags": creationflags}
            ) as group_options,
            patch.object(processes.subprocess, "Popen", return_value=process) as popen,
            patch.object(processes, "_create_windows_kill_job", side_effect=primary),
            patch.object(
                processes,
                "_terminate_suspended_windows_leader",
                return_value=False,
            ) as terminate,
        ):
            with self.assertRaises(OSError) as raised:
                processes.popen_owned(["python"], marker_store=store)

        self.assertIs(raised.exception, primary)
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertIn("recovery marker retained", str(raised.exception.__cause__))
        terminate.assert_called_once_with(process)
        group_options.assert_called_once_with()
        popen.assert_called_once_with(("python",), creationflags=creationflags)
        store.create.assert_called_once()
        store.remove.assert_not_called()


class _NativePosixCase(unittest.TestCase):
    """Real owned processes and an exact unrelated peer; Actions-only fixtures."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="fr13-native-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = processes.ProcessMarkerStore(self.root / "markers")
        self.created: list[tuple[subprocess.Popen[Any], Path | None]] = []
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
        for process, marker in reversed(self.created):
            try:
                if processes.terminate_process_group(process):
                    self.store.remove(marker)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        if self.native_state(self.peer.pid, self.peer_birth) == "live":
            self.peer.terminate()
        self.peer.wait(timeout=5)


@unittest.skipUnless(os.name == "posix", "real POSIX retained group ownership")
class PosixProcessCleanupTests(_NativePosixCase):
    def test_normal_run_owned_clears_live_descendant_then_reaps_and_preserves_output(self) -> None:
        from pyocd_debug_mcp.kernel._analysis_posix import _exit_status

        for status, check, ignore_term in ((0, False, False), (7, False, True), (7, True, False)):
            with self.subTest(status=status, check=check, ignore_term=ignore_term):
                real_terminate = processes.terminate_process_group
                real_signal = os.killpg
                boundaries: list[tuple[int, float]] = []
                signals: list[int] = []

                def signal_group(group: int, selected: int) -> None:
                    signals.append(selected)
                    real_signal(group, selected)

                def terminate(process: subprocess.Popen[Any]) -> bool:
                    self.assertEqual(process.returncode, status)
                    self.assertEqual(_exit_status(process.pid), status)
                    boundaries.append(self.assert_live_child(process))
                    return real_terminate(process)

                with (
                    patch.object(processes, "popen_owned", self.capture_spawn),
                    patch.object(processes, "terminate_process_group", terminate),
                    patch.object(processes.os, "killpg", signal_group),
                ):
                    if check:
                        with self.assertRaises(subprocess.CalledProcessError) as raised:
                            processes.run_owned(
                                [sys.executable, "-c", self.descendant_program(exit_code=status)],
                                timeout=15,
                                capture_output=True,
                                text=True,
                                check=True,
                                marker_store=self.store,
                            )
                        result = raised.exception
                    else:
                        result = processes.run_owned(
                            [
                                sys.executable,
                                "-c",
                                self.descendant_program(exit_code=status, ignore_term=ignore_term),
                            ],
                            timeout=15,
                            capture_output=True,
                            text=True,
                            marker_store=self.store,
                        )
                self.assertEqual(result.returncode, status)
                self.assertEqual(result.stdout, "owned-output\n")
                self.assertEqual(result.stderr, "owned-error\n")
                self.assertEqual(len(boundaries), 1)
                expected_signals = [signal.SIGTERM, signal.SIGKILL] if ignore_term else [signal.SIGTERM]
                self.assertEqual(signals, expected_signals)
                self.assert_child_terminal(boundaries[0])
                self.assert_reaped(self.created[-1][0], status)
                self.assertEqual(list(self.store.root.glob("*.json")), [])

    def test_terminal_only_group_is_confirmed_without_signalling_and_actually_reaped(self) -> None:
        process = self.owned("raise SystemExit(23)")
        self.assertEqual(process.wait(timeout=10), 23)
        from pyocd_debug_mcp.kernel._analysis_posix import _exit_status

        self.assertEqual(_exit_status(process.pid), 23, "normal wait released the anchor")
        with patch.object(processes.os, "killpg", wraps=os.killpg) as signal_group:
            self.assertTrue(processes.terminate_process_group(process))
        signal_group.assert_not_called()
        self.assert_reaped(process, 23)
        # Idempotence uses the completed proof, without probing/signalling a
        # possibly recycled numeric ID after that reap.
        with patch.object(processes.os, "killpg", side_effect=AssertionError("new signal")):
            self.assertTrue(processes.terminate_process_group(process))
        self.assert_peer()

    def test_timeout_and_baseexception_clear_live_group_preserving_original_error(self) -> None:
        class Cancelled(BaseException):
            pass

        for cancellation in (False, True):
            with self.subTest(cancellation=cancellation):
                primary = Cancelled("cancelled during communicate")
                original_communicate = subprocess.Popen.communicate
                first = True

                def communicate(process: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> Any:
                    nonlocal first
                    if first:
                        first = False
                        self.assert_live_child(process)
                        if cancellation:
                            raise primary
                    return original_communicate(process, *args, **kwargs)

                with (
                    patch.object(processes, "popen_owned", self.capture_spawn),
                    patch.object(subprocess.Popen, "communicate", communicate),
                ):
                    error_type = Cancelled if cancellation else subprocess.TimeoutExpired
                    with self.assertRaises(error_type) as raised:
                        processes.run_owned(
                            [sys.executable, "-c", self.descendant_program(linger=True)],
                            timeout=1,
                            capture_output=True,
                            text=True,
                            marker_store=self.store,
                        )
                if cancellation:
                    self.assertIs(raised.exception, primary)
                else:
                    assert isinstance(raised.exception, subprocess.TimeoutExpired)
                    self.assertEqual(raised.exception.stdout, "owned-output\n")
                    self.assertEqual(raised.exception.stderr, "owned-error\n")
                self.assert_child_terminal(self.child_identity())
                self.assert_reaped(self.created[-1][0])
                self.assertEqual(list(self.store.root.glob("*.json")), [])

    def test_denied_live_group_is_unconfirmed_and_retains_ownership(self) -> None:
        process = self.owned(self.descendant_program(linger=True))
        identity = self.assert_live_child(process)
        with patch.object(
            processes.os, "killpg", side_effect=PermissionError("live group denied")
        ) as signal_group:
            self.assertFalse(processes.terminate_process_group(process))
        self.assertEqual(
            [call.args for call in signal_group.call_args_list],
            [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)],
        )
        self.assertIsNone(process.returncode)
        self.assertEqual(self.native_state(*identity), "live")
        self.assertEqual(len(list(self.store.root.glob("*.json"))), 1)
        self.assert_peer()

    def test_run_owned_denied_live_descendant_retains_marker_and_reports_failure(self) -> None:
        with (
            patch.object(processes, "popen_owned", self.capture_spawn),
            patch.object(processes.os, "killpg", side_effect=PermissionError("live group denied")),
        ):
            with self.assertRaisesRegex(RuntimeError, "descendants could not be cleared"):
                processes.run_owned(
                    [sys.executable, "-c", self.descendant_program()],
                    timeout=15,
                    capture_output=True,
                    text=True,
                    marker_store=self.store,
                )
        process = self.created[-1][0]
        self.assert_live_child(process)
        from pyocd_debug_mcp.kernel._analysis_posix import _exit_status

        self.assertEqual(_exit_status(process.pid), 7)
        self.assertEqual(len(list(self.store.root.glob("*.json"))), 1)
        self.assert_peer()

    def test_terminal_descendant_requires_full_group_proof_and_actual_reap(self) -> None:
        process = self.owned(self.descendant_program())
        identity = self.assert_live_child(process)
        self.assertEqual(process.wait(timeout=10), 7)
        child = psutil.Process(identity[0])
        self.assertEqual(child.create_time(), identity[1])
        child.kill()
        deadline = time.monotonic() + 5
        while self.native_state(*identity) == "live" and time.monotonic() < deadline:
            time.sleep(0.01)
        state = self.native_state(*identity)
        self.assertIn(state, {"zombie", "gone"})
        members = processes._current_group_members(process.pid, time.monotonic() + 0.5)
        self.assertIn(process.pid, members)
        if state == "zombie":
            # The glibc2.28 PID1-zombie premise; a host that already reaped the
            # child establishes absence instead, without claiming that premise.
            self.assertIn(identity[0], members)
            self.assertEqual(os.getpgid(identity[0]), process.pid)
            self.assertEqual(os.getsid(identity[0]), process.pid)
        with patch.object(processes.os, "killpg", wraps=os.killpg) as signal_group:
            self.assertTrue(processes.terminate_process_group(process))
        signal_group.assert_not_called()
        self.assert_reaped(process, 7)
        self.assert_child_terminal(identity)

    def test_unknown_incomplete_foreign_and_reused_members_never_authorize_signals(self) -> None:
        from pyocd_debug_mcp.kernel import _analysis_posix

        process = self.owned(self.descendant_program())
        identity = self.assert_live_child(process)
        self.assertEqual(process.wait(timeout=10), 7)
        real_members = processes._current_group_members
        real_state = _analysis_posix._process_state
        for fault in ("incomplete", "membership_error", "unknown", "reused", "foreign"):
            with self.subTest(fault=fault):
                def members(group: int, deadline: float) -> set[int]:
                    if fault == "incomplete":
                        return set()
                    if fault == "membership_error":
                        raise PermissionError("native membership denied")
                    observed = real_members(group, deadline)
                    return observed | {self.peer.pid} if fault == "foreign" else observed

                def state(pid: int, token: str) -> str:
                    if pid == identity[0] and fault in {"unknown", "reused"}:
                        return fault
                    return real_state(pid, token)

                with (
                    patch.object(processes, "_current_group_members", members),
                    patch.object(_analysis_posix, "_process_state", state),
                    patch.object(processes.os, "killpg", wraps=os.killpg) as signal_group,
                ):
                    self.assertFalse(processes.terminate_process_group(process))
                signal_group.assert_not_called()
                self.assertEqual(_analysis_posix._exit_status(process.pid), 7)
                self.assertEqual(self.native_state(*identity), "live")
                self.assertEqual(len(list(self.store.root.glob("*.json"))), 1)
                self.assert_peer()

    def test_faulted_anchor_and_failed_real_reap_cannot_confirm_cleanup(self) -> None:
        from pyocd_debug_mcp.kernel import _analysis_posix

        process = self.owned("raise SystemExit(17)")
        self.assertEqual(process.wait(timeout=10), 17)
        with (
            patch.object(
                _analysis_posix, "_exit_status", side_effect=ChildProcessError("anchor lost")
            ),
            patch.object(processes.os, "killpg", wraps=os.killpg) as signal_group,
        ):
            self.assertFalse(processes.terminate_process_group(process))
        signal_group.assert_not_called()
        for token in (None, "psutil:deterministic-reused-birth"):
            with self.subTest(leader_birth=token):
                with (
                    patch.object(processes, "_posix_start_token", return_value=token),
                    patch.object(processes.os, "killpg", wraps=os.killpg) as signal_group,
                ):
                    self.assertFalse(processes.terminate_process_group(process))
                signal_group.assert_not_called()
                self.assertEqual(_analysis_posix._exit_status(process.pid), 17)
                self.assert_peer()
        with patch.object(processes.os, "waitpid", return_value=(0, 0)) as reap:
            self.assertFalse(processes.terminate_process_group(process))
        reap.assert_called_once_with(process.pid, os.WNOHANG)
        self.assertEqual(_analysis_posix._exit_status(process.pid), 17)
        marker = self.created[0][1]
        assert marker is not None
        self.assertTrue(marker.is_file())
        self.assert_peer()
        self.assertTrue(processes.terminate_process_group(process))
        self.assert_reaped(process, 17)

    def test_already_reaped_empty_group_succeeds_but_foreign_group_is_probe_only(self) -> None:
        with patch.object(processes.os, "killpg", wraps=os.killpg) as signal_group:
            self.assertFalse(processes.terminate_process_group(self.peer))
        signal_group.assert_not_called()
        self.assert_peer()
        raw = subprocess.Popen(
            [sys.executable, "-c", "raise SystemExit(19)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(raw.wait(timeout=10), 19)
        self.assert_reaped(raw, 19)
        self.assertTrue(processes.terminate_process_group(raw))
        original_pid = raw.pid
        try:
            # Deterministic stale-object/foreign-group control, not a claim of
            # actual native PID reuse. The peer's exact live birth is real.
            raw.pid = self.peer.pid
            with patch.object(processes.os, "killpg", wraps=os.killpg) as signal_group:
                self.assertFalse(processes.terminate_process_group(raw, grace_seconds=0.05))
            self.assertTrue(signal_group.call_args_list)
            self.assertTrue(
                all(call.args == (self.peer.pid, 0) for call in signal_group.call_args_list)
            )
            self.assert_peer()
        finally:
            raw.pid = original_pid


if __name__ == "__main__":
    unittest.main()
