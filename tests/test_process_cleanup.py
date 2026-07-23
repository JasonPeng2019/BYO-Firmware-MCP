from __future__ import annotations

import asyncio
import gc
import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

import anyio

from firmware_mcp.kernel.operations import (
    OperationCancelledError,
    OperationCleanupError,
    OperationManager,
    dispatch,
)
from firmware_mcp.kernel import processes


class WindowsProcessCleanupTests(unittest.TestCase):
    def test_cleanup_has_no_project_defined_grace_budget(self) -> None:
        self.assertFalse(hasattr(processes, "DEFAULT_PROCESS_GROUP_CLEANUP_GRACE_SECONDS"))
        self.assertFalse(hasattr(processes, "MAX_OWNED_PROCESS_CLEANUP_SECONDS"))

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

    def test_windows_job_nonzero_active_count_is_unconfirmed_without_a_scheduler_loop(self) -> None:
        """One failed proof leaves the Job handle for marker-backed recovery."""

        class Kernel32:
            def __init__(self) -> None:
                self.terminate_calls = 0
                self.query_calls = 0
                self.close_calls = 0

            def TerminateJobObject(self, _handle: int, _exit_code: int) -> int:
                self.terminate_calls += 1
                return 1

            def QueryInformationJobObject(
                self,
                _handle: int,
                _info_class: int,
                accounting_pointer: Any,
                _size: int,
                _reserved: Any,
            ) -> int:
                self.query_calls += 1
                # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION.ActiveProcesses is
                # the sixth 32-bit field after four 64-bit counters.
                pointer = ctypes.cast(accounting_pointer, ctypes.c_void_p).value
                assert pointer is not None
                ctypes.c_uint32.from_address(pointer + 40).value = 1
                return 1

            def CloseHandle(self, _handle: int) -> int:
                self.close_calls += 1
                return 1

        kernel32 = Kernel32()
        retained_jobs = {42: 99}
        with patch.object(processes, "_WINDOWS_JOB_HANDLES", retained_jobs):
            cleaned = processes._close_windows_job(42, 99, terminate=True, kernel32=kernel32)
            # The retained Job is the proof boundary used by the caller to
            # retain its owned-process recovery marker rather than claiming
            # cleanup.
            self.assertEqual(processes._WINDOWS_JOB_HANDLES, {42: 99})

        self.assertFalse(cleaned)
        self.assertEqual(kernel32.terminate_calls, 1)
        self.assertEqual(kernel32.query_calls, 1)
        self.assertEqual(kernel32.close_calls, 0)


class OwnedProcessMarkerCleanupTests(unittest.TestCase):
    def test_omitted_stdin_is_closed_and_explicit_none_remains_inheritance_opt_in(self) -> None:
        """MCP-owned children cannot accidentally inherit the protocol input pipe."""

        observed: list[object] = []
        process = SimpleNamespace(returncode=0, communicate=Mock(return_value=(b"", b"")))
        store = Mock()
        with (
            patch.object(processes, "popen_owned", return_value=(process, None)) as popen,
            patch.object(processes, "terminate_process_group", return_value=True),
        ):
            processes.run_owned(["provider-tool"], marker_store=store)
            observed.append(popen.call_args.kwargs["stdin"])
            processes.run_owned(["provider-tool"], marker_store=store, stdin=None)
            observed.append(popen.call_args.kwargs["stdin"])

        self.assertEqual(observed, [subprocess.DEVNULL, None])

    @unittest.skipUnless(os.name == "nt", "Windows taskkill ownership proof")
    def test_windows_taskkill_recovery_receives_closed_stdin(self) -> None:
        """The direct recovery command retains PID/start-token semantics with closed input."""

        with (
            patch.object(processes, "_start_token", return_value="started"),
            patch.object(processes, "identity_matches", return_value=False),
            patch.object(
                processes.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as taskkill,
        ):
            self.assertTrue(processes.terminate_marked_group(123, "started"))

        self.assertEqual(taskkill.call_args.args[0][-4:], ["/PID", "123", "/T", "/F"])
        self.assertIs(taskkill.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(taskkill.call_args.kwargs["check"])

    def _run_with_marker_remove_failure(
        self,
        process: object,
        *,
        check: bool = False,
        timeout_seconds: float | None = None,
    ) -> processes.OwnedProcessMarkerCleanupError:
        marker = Path("retained-marker.json")
        store = Mock()
        store.remove.side_effect = OSError("marker unlink denied")
        with (
            patch.object(processes, "popen_owned", return_value=(process, marker)),
            patch.object(processes, "terminate_process_group", return_value=True),
            self.assertRaises(processes.OwnedProcessMarkerCleanupError) as raised,
        ):
            processes.run_owned(
                ["provider-tool"],
                marker_store=store,
                check=check,
                timeout_seconds=timeout_seconds,
            )
        self.assertEqual(raised.exception.marker, marker)
        self.assertIsInstance(raised.exception.cleanup, OSError)
        self.assertIn("marker cleanup", str(raised.exception))
        self.assertIn("recovery marker may be retained", str(raised.exception))
        self.assertIn("marker unlink denied", str(raised.exception))
        return raised.exception

    def test_checked_nonzero_and_marker_cleanup_failure_retain_both_facts(self) -> None:
        process = SimpleNamespace(returncode=9, communicate=Mock(return_value=(b"out", b"err")))
        error = self._run_with_marker_remove_failure(process, check=True)
        self.assertIsInstance(error.primary, subprocess.CalledProcessError)
        primary = cast(subprocess.CalledProcessError, error.primary)
        self.assertEqual(primary.returncode, 9)
        self.assertIs(error.__cause__, error.primary)

    def test_timeout_and_marker_cleanup_failure_retain_both_facts(self) -> None:
        timed_out = subprocess.TimeoutExpired(["provider-tool"], 1.5)
        process = SimpleNamespace(
            returncode=0,
            communicate=Mock(side_effect=(timed_out, (b"out", b"err"))),
        )
        error = self._run_with_marker_remove_failure(process, timeout_seconds=1.5)
        self.assertIs(error.primary, timed_out)
        self.assertEqual(timed_out.stdout, b"out")
        self.assertEqual(timed_out.stderr, b"err")

    def test_primary_exception_and_marker_cleanup_failure_retain_both_facts(self) -> None:
        primary = RuntimeError("provider interrupted")
        process = SimpleNamespace(returncode=None, communicate=Mock(side_effect=primary))
        error = self._run_with_marker_remove_failure(process)
        self.assertIs(error.primary, primary)
        self.assertIs(error.__cause__, primary)

    def test_successful_child_and_marker_cleanup_failure_is_not_success(self) -> None:
        process = SimpleNamespace(returncode=0, communicate=Mock(return_value=(b"out", b"err")))
        error = self._run_with_marker_remove_failure(process)
        self.assertIsNone(error.primary)
        self.assertIsInstance(error.__cause__, OSError)

    def test_sole_marker_removal_failure_is_reported_once_and_retains_marker(self) -> None:
        """A failed worker-owned unlink leaves exactly one recovery marker."""

        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "retained-marker.json"
            marker.write_text("recovery evidence", encoding="utf-8")
            process = SimpleNamespace(
                returncode=0,
                communicate=Mock(return_value=(b"out", b"err")),
            )
            store = Mock()
            store.remove.side_effect = PermissionError("marker unlink denied")
            with (
                patch.object(processes, "popen_owned", return_value=(process, marker)),
                patch.object(processes, "terminate_process_group", return_value=True),
                self.assertRaises(processes.OwnedProcessMarkerCleanupError) as raised,
            ):
                processes.run_owned(["provider-tool"], marker_store=store)

            store.remove.assert_called_once_with(marker)
            self.assertIsInstance(raised.exception.cleanup, PermissionError)
            self.assertIn("marker unlink denied", str(raised.exception))
            self.assertTrue(marker.exists())

    def test_cancellation_terminates_once_and_worker_owns_marker_removal(self) -> None:
        """Cancellation cannot race the reaper for a Windows marker unlink."""

        class BlockingProcess:
            def __init__(self) -> None:
                self.returncode: int | None = None
                self.communicating = threading.Event()
                self.release = threading.Event()

            def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
                del timeout
                self.communicating.set()
                self.release.wait()
                self.returncode = -9
                return b"", b""

        process = BlockingProcess()
        manager = OperationManager()
        captured = []
        termination_calls: list[object] = []
        removal_calls: list[Path | None] = []
        removal_started = threading.Event()
        allow_removal = threading.Event()

        def terminate_owned(candidate: object) -> bool:
            termination_calls.append(candidate)
            process.release.set()
            return True

        def remove_marker(marker: Path | None) -> None:
            removal_calls.append(marker)
            removal_started.set()
            allow_removal.wait()
            if marker is not None:
                marker.unlink(missing_ok=True)

        class BlockingMarkerStore(processes.ProcessMarkerStore):
            @staticmethod
            def remove(path: Path | None) -> None:
                remove_marker(path)

        async def scenario(marker: Path) -> list[BaseException]:
            observed: list[BaseException] = []

            async def invoke() -> None:
                try:
                    await dispatch(
                        "build_firmware",
                        None,
                        lambda: processes.run_owned(
                            ["provider-tool"],
                            marker_store=BlockingMarkerStore(),
                        ),
                        request_id="single-owner-marker",
                        manager=manager,
                        resource_binder=captured.append,
                    )
                except BaseException as exc:  # expected cancellation result
                    observed.append(exc)

            async with anyio.create_task_group() as group:
                group.start_soon(invoke)
                await asyncio.to_thread(process.communicating.wait)
                cancelled = await asyncio.to_thread(
                    lambda: manager.cancel_request("single-owner-marker", "test cancellation")
                )
                self.assertEqual(cancelled, 1)
                await asyncio.to_thread(removal_started.wait)
                self.assertFalse(captured[0].done.is_set())
                allow_removal.set()
            return observed

        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "owned-marker.json"
            marker.write_text("owned process", encoding="utf-8")
            with (
                patch.object(processes, "popen_owned", return_value=(process, marker)),
                patch.object(processes, "terminate_process_group", side_effect=terminate_owned),
            ):
                observed = anyio.run(scenario, marker)

        self.assertEqual(len(observed), 1)
        self.assertIsInstance(observed[0], OperationCancelledError)
        self.assertEqual(str(observed[0]), "test cancellation")
        self.assertEqual(termination_calls, [process])
        self.assertEqual(removal_calls, [marker])
        self.assertFalse(marker.exists())
        self.assertEqual(captured[0].resources.cleanup_errors, [])
        self.assertEqual(captured[0].resources.fatal_cleanup_errors, [])

    def test_managed_operation_records_one_marker_cleanup_fact_without_losing_primary(self) -> None:
        process = SimpleNamespace(returncode=7, communicate=Mock(return_value=(b"out", b"err")))
        marker = Path("managed-retained-marker.json")
        store = Mock()
        store.remove.side_effect = OSError("managed marker unlink denied")
        captured = []

        async def scenario() -> None:
            with (
                patch.object(processes, "popen_owned", return_value=(process, marker)),
                patch.object(processes, "terminate_process_group", return_value=True),
                self.assertRaises(OperationCleanupError) as raised,
            ):
                await dispatch(
                    "build_firmware",
                    None,
                    lambda: processes.run_owned(["provider-tool"], marker_store=store, check=True),
                    request_id="managed-marker-cleanup",
                    manager=OperationManager(),
                    resource_binder=captured.append,
                )
            self.assertIn("CalledProcessError", str(raised.exception))
            self.assertIn("marker cleanup", str(raised.exception))

        anyio.run(scenario)
        self.assertEqual(len(captured), 1)
        resources = captured[0].resources
        self.assertEqual(len(resources.cleanup_errors), 1)
        self.assertEqual(resources.cleanup_errors, resources.fatal_cleanup_errors)


if __name__ == "__main__":
    unittest.main()
