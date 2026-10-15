from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import tempfile
import unittest
import warnings
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from pyocd_debug_mcp.kernel import processes


@unittest.skipUnless(os.name == "posix", "POSIX process groups")
class PosixProcessCleanupTests(unittest.TestCase):
    def test_missing_marker_leader_requires_confirmed_group_absence(self) -> None:
        for probe_result, expected in (
            (ProcessLookupError(), True),
            (None, False),
            (PermissionError(), False),
            (OSError("probe unavailable"), False),
        ):
            with self.subTest(probe_result=probe_result):
                with (
                    patch.object(processes, "_start_token", return_value=None),
                    patch.object(processes.os, "killpg", side_effect=probe_result) as probe,
                ):
                    self.assertEqual(processes.terminate_marked_group(42, "old"), expected)
                probe.assert_called_once_with(42, 0)

    def test_reused_marker_identity_never_signals_the_new_process_group(self) -> None:
        with (
            patch.object(processes, "_start_token", return_value="replacement"),
            patch.object(processes.os, "killpg") as probe,
        ):
            self.assertFalse(processes.terminate_marked_group(42, "old"))
        probe.assert_not_called()

    def test_startup_retires_absent_group_but_retains_uncertain_markers(self) -> None:
        from pyocd_debug_mcp.kernel.hygiene import require_clean_startup

        for probe_result in (ProcessLookupError(), None, PermissionError()):
            with (
                self.subTest(probe_result=probe_result),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                marker = processes.ProcessMarker(
                    2, "old", 10, "owner", 20, "child", "hash", "tool", "now"
                )
                path = root / "old.json"
                path.write_text(json.dumps(asdict(marker)), encoding="utf-8")
                with (
                    patch("pyocd_debug_mcp.kernel.hygiene._start_token", return_value=None),
                    patch.object(processes, "_start_token", return_value=None),
                    patch.object(processes.os, "killpg", side_effect=probe_result) as probe,
                ):
                    if isinstance(probe_result, ProcessLookupError):
                        self.assertEqual(require_clean_startup(root).stale_removed, 1)
                        self.assertFalse(path.exists())
                    else:
                        with self.assertRaisesRegex(RuntimeError, "1 unresolved marker"):
                            require_clean_startup(root)
                        self.assertTrue(path.exists())
                probe.assert_called_once_with(20, 0)

    def test_exited_worker_eperm_is_reaped_before_group_absence_is_confirmed(self) -> None:
        process = Mock(pid=42)
        with patch.object(
            processes.os, "killpg", side_effect=[PermissionError(), ProcessLookupError()]
        ) as signal_group:
            self.assertTrue(processes.terminate_process_group(process))
        process.wait.assert_called_once_with(
            timeout=processes.DEFAULT_PROCESS_GROUP_CLEANUP_GRACE_SECONDS
        )
        self.assertEqual(signal_group.call_args_list[-1].args, (42, 0))

    def test_permission_denied_is_not_cleanup_proof_for_remaining_group(self) -> None:
        process = Mock(pid=42)
        with patch.object(processes.os, "killpg", side_effect=PermissionError()):
            self.assertFalse(processes.terminate_process_group(process))

    def test_exited_leader_does_not_hide_live_descendants(self) -> None:
        process = Mock(pid=42, returncode=0)
        process.poll.return_value = 0
        # The surviving group is checked even after reaping the leader. If its
        # SIGKILL cannot be delivered, cleanup must remain unconfirmed.
        with patch.object(
            processes.os, "killpg", side_effect=[None, None, PermissionError()]
        ) as signal_group:
            self.assertFalse(processes.terminate_process_group(process))
        self.assertEqual(signal_group.call_args_list[-1].args, (42, processes.signal.SIGKILL))

    def test_real_exited_child_is_reaped_and_its_group_disappears(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"], start_new_session=True, stdout=subprocess.PIPE
        )
        try:
            assert process.stdout is not None
            # EOF synchronizes with child exit without reaping it first.
            self.assertEqual(process.stdout.read(), b"")
            self.assertTrue(processes.terminate_process_group(process))
            self.assertIsNotNone(process.poll())
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()


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
        with (
            patch.object(processes.os, "name", "nt"),
            patch.object(processes.subprocess, "Popen", return_value=process),
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
        store.create.assert_called_once()
        store.remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
