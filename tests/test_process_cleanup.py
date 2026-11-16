from __future__ import annotations
from tests.process_fixture_support import _NativePosixCase

import gc
import os
import signal
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch


from pyocd_debug_mcp.kernel import processes


class WindowsProcessCleanupTests(unittest.TestCase):

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
                expected_signals = (
                    [signal.SIGTERM, signal.SIGKILL] if ignore_term else [signal.SIGTERM]
                )
                self.assertEqual(signals, expected_signals)
                self.assert_child_terminal(boundaries[0])
                self.assert_reaped(self.created[-1][0], status)
                self.assertEqual(list(self.store.root.glob("*.json")), [])


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


if __name__ == "__main__":
    unittest.main()
