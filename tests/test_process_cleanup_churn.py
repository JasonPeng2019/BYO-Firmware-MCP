"""Real ordinary callers and directed native schedules; no synthetic process state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import psutil

from pyocd_debug_mcp import discovery_hooks, native_build
from pyocd_debug_mcp.kernel import processes
from tests.test_process_cleanup import _NativePosixCase

FIXTURE = Path(__file__).parent / "fixtures" / "process_churn.py"


@unittest.skipUnless(os.name == "posix", "retained POSIX group churn")
class OrdinaryChurnContracts(_NativePosixCase):
    def setUp(self) -> None:
        super().setUp()
        self.events: list[dict] = []
        # Last cleanup, after separate fixture recovery; it never changes results.
        self.addCleanup(self.write_events)

    def write_events(self) -> None:
        destination = os.environ.get("BYO_FINAL_CLEANUP_EVIDENCE")
        if destination:
            path = Path(destination) / (self._testMethodName + ".churn.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"test": self.id(), "events": self.events}, indent=2) + "\n")

    def argv(self, mode: str = "ordinary", status: str = "7") -> list[str]:
        return [sys.executable, str(FIXTURE), "leader", str(self.root), mode, status]

    def read_role(self, role: str) -> dict:
        path = self.root / (role + ".json")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                self.events.append({"role": role, **value})
                return value
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.002)
        self.fail("real native identity not ready: " + role)

    def capture_ready(self, *args, **kwargs):
        kwargs["marker_store"] = self.store
        process, marker = self.capture_spawn(*args, **kwargs)
        branch = self.read_role("branch")
        self.assertEqual(branch["ppid"], process.pid)
        self.assertEqual((branch["pgid"], branch["sid"]), (process.pid, process.pid))
        self.assertEqual(self.native_state(branch["pid"], branch["birth"]), "live")
        self.descendants.append((branch["pid"], branch["birth"]))
        self.assert_peer()
        return process, marker

    def clean_postconditions(self, process, status=None) -> None:
        self.assert_reaped(process, status)
        self.assertFalse(list(self.store.root.glob("*.json")))
        branch = self.read_role("branch")
        self.assert_child_terminal((branch["pid"], branch["birth"]))
        if (self.root / "leaf.json").exists():
            leaf = self.read_role("leaf")
            self.assert_child_terminal((leaf["pid"], leaf["birth"]))
        self.assert_peer()
        self.events.append({"boundary": "actual reap/terminal children/removed marker/peer live"})

    def test_ordinary_normal_exit_preserves_status_streams_and_clears_churning_branch(self) -> None:
        for status, check in (("0", False), ("7", False), ("7", True)):
            with self.subTest(status=status, check=check):
                with patch.object(processes, "popen_owned", self.capture_ready):
                    if check:
                        with self.assertRaises(subprocess.CalledProcessError) as raised:
                            processes.run_owned(
                                self.argv(status=status),
                                timeout=15,
                                capture_output=True,
                                text=True,
                                check=True,
                                marker_store=self.store,
                            )
                        result = raised.exception
                    else:
                        result = processes.run_owned(
                            self.argv(status=status),
                            timeout=15,
                            capture_output=True,
                            text=True,
                            marker_store=self.store,
                        )
                self.assertEqual(
                    (result.returncode, result.stdout, result.stderr),
                    (int(status), "churn-output\n", "churn-error\n"),
                )
                self.clean_postconditions(self.created[-1][0], int(status))
                for path in self.root.glob("*.json"):
                    path.unlink()

    def test_ordinary_timeout_and_cancellation_preserve_error_and_clean_live_tree(self) -> None:
        class Cancelled(BaseException):
            pass

        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                primary = Cancelled("ordinary caller cancellation")
                real_communicate = subprocess.Popen.communicate

                def communicate(process, *args, **kwargs):
                    if cancel:
                        self.read_role("leaf")
                        raise primary
                    return real_communicate(process, *args, **kwargs)

                with (
                    patch.object(processes, "popen_owned", self.capture_ready),
                    patch.object(subprocess.Popen, "communicate", communicate),
                ):
                    with self.assertRaises(
                        Cancelled if cancel else subprocess.TimeoutExpired
                    ) as raised:
                        processes.run_owned(
                            self.argv(status="linger"),
                            timeout=1,
                            capture_output=True,
                            text=True,
                            marker_store=self.store,
                        )
                if cancel:
                    self.assertIs(raised.exception, primary)
                else:
                    self.assertEqual(raised.exception.stdout, "churn-output\n")
                    self.assertEqual(raised.exception.stderr, "churn-error\n")
                self.clean_postconditions(self.created[-1][0])
                for path in self.root.glob("*.json"):
                    path.unlink()

    def test_real_fork_between_complete_scans_still_escalates_and_actually_reaps(self) -> None:
        real_members = processes._current_group_members
        triggered = False

        def members(group, deadline):
            nonlocal triggered
            observed = real_members(group, deadline)
            if not triggered:
                triggered = True
                (self.root / "fork-now").touch()
                leaf = self.read_role("leaf")
                self.descendants.append((leaf["pid"], leaf["birth"]))
                self.assertNotIn(leaf["pid"], observed)
                self.assertEqual(os.getpgid(leaf["pid"]), group)
                self.assertLess(time.monotonic(), deadline)
                self.events.append(
                    {
                        "boundary": "actual growth after first scan",
                        "snapshot": sorted(observed),
                        "leaf": leaf,
                    }
                )
            return observed

        with (
            patch.object(processes, "popen_owned", self.capture_ready),
            patch.object(processes, "_current_group_members", members),
        ):
            result = processes.run_owned(
                self.argv("directed"),
                timeout=15,
                capture_output=True,
                text=True,
                marker_store=self.store,
            )
        self.assertTrue(triggered)
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (7, "churn-output\n", "churn-error\n"),
        )
        self.clean_postconditions(self.created[-1][0], 7)

    def test_actual_member_reap_after_live_read_is_retried_within_existing_budget(self) -> None:
        from pyocd_debug_mcp.kernel import _analysis_posix

        process, _marker = self.capture_ready(
            self.argv("member-exit"),
            marker_store=self.store,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process.wait(timeout=10)
        (self.root / "fork-now").touch()
        leaf = self.read_role("leaf")
        real_state = _analysis_posix._process_state
        triggered = False

        def state(pid, token):
            nonlocal triggered
            observed = real_state(pid, token)
            if pid == leaf["pid"] and observed == "live" and not triggered:
                triggered = True
                (self.root / "leaf-exit").touch()
                deadline = time.monotonic() + 0.4
                while not (self.root / "leaf-reaped").exists() and time.monotonic() < deadline:
                    time.sleep(0.002)
                self.assertTrue((self.root / "leaf-reaped").exists())
                with self.assertRaises(ProcessLookupError):
                    os.getpgid(pid)
                self.events.append(
                    {"boundary": "actual member released between live and getpgid", "leaf": leaf}
                )
            return observed

        with patch.object(_analysis_posix, "_process_state", state):
            self.assertTrue(processes.terminate_process_group(process))
        self.assertTrue(triggered)
        self.store.remove(_marker)
        self.clean_postconditions(process, 7)

    def test_discovery_actual_churning_hook_normal_timeout_and_cancel(self) -> None:
        from tests.discovery_hook_fixtures import single_spec

        class Cancelled(BaseException):
            pass

        for mode in ("normal", "timeout", "cancel"):
            with self.subTest(mode=mode):
                spec = single_spec(
                    self.root, "probe", [], timeout_seconds=1 if mode == "timeout" else 15
                )
                real_wait = processes._OwnedPosixPopen.wait
                primary = Cancelled("discovery cancelled")

                def wait(process, *args, **kwargs):
                    if mode == "cancel":
                        self.read_role("leaf")
                        raise primary
                    return real_wait(process, *args, **kwargs)

                with (
                    patch.object(discovery_hooks, "popen_owned", self.capture_ready),
                    patch.object(processes._OwnedPosixPopen, "wait", wait),
                ):
                    if mode == "cancel":
                        with self.assertRaises(Cancelled) as raised:
                            discovery_hooks._execute(
                                spec, self.argv(status="linger"), marker_store=self.store
                            )
                        self.assertIs(raised.exception, primary)
                    else:
                        result = discovery_hooks._execute(
                            spec,
                            self.argv(status="7" if mode == "normal" else "linger"),
                            marker_store=self.store,
                        )
                        self.assertEqual(
                            result.outcome, "exited" if mode == "normal" else "timeout"
                        )
                        self.assertEqual(result.stdout, b"churn-output\n")
                        self.assertEqual(result.stderr_text, "churn-error\n")
                self.clean_postconditions(self.created[-1][0], 7 if mode == "normal" else None)
                for path in self.root.glob("*.json"):
                    path.unlink()

    def test_native_build_actual_churning_command_normal_and_timeout(self) -> None:
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                args = native_build.build_parser().parse_args(
                    [
                        "--project-dir",
                        str(self.root),
                        "--build-dir",
                        str(self.root / "build"),
                        "--timeout-seconds",
                        "1" if timeout else "15",
                        "--",
                        *self.argv(status="linger" if timeout else "7"),
                    ]
                )
                with patch.object(processes, "popen_owned", self.capture_ready):
                    if timeout:
                        with self.assertRaisesRegex(
                            native_build.BuildEvidenceError, "exceeded the 1-second timeout"
                        ):
                            native_build.run_build(args)
                    else:
                        self.assertEqual(native_build.run_build(args), 7)
                self.clean_postconditions(self.created[-1][0], None if timeout else 7)
                for path in self.root.glob("*.json"):
                    path.unlink()

    def test_directed_fixture_member_exit_diagnostic_records_actual_return_branch(self) -> None:
        # Reproduce the old fixture sequence, separately from ordinary callers.
        # Trace only this function's returns/exceptions; no extra membership scans.
        process = self.owned(self.descendant_program(linger=True))
        pid, birth = self.assert_live_child(process)
        child = psutil.Process(pid)
        self.assertEqual(child.create_time(), birth)
        child.kill()
        code = processes._retained_group_state.__code__

        def trace(frame, event, value):
            if frame.f_code is not code:
                return None
            frame.f_trace_lines = False
            if event == "return":
                self.events.append(
                    {
                        "diagnostic": "actual retained membership return",
                        "state": value,
                        "line": frame.f_lineno,
                        "leader_exit": frame.f_locals.get("leader_exit"),
                        "members": sorted(frame.f_locals.get("members", [])),
                        "following": sorted(frame.f_locals.get("following", [])),
                        "member_states": frame.f_locals.get("states", {}),
                    }
                )
            elif event == "exception":
                self.events.append(
                    {
                        "diagnostic": "actual native observation exception",
                        "line": frame.f_lineno,
                        "type": value[0].__name__,
                        "detail": str(value[1]),
                    }
                )
            return trace

        previous = sys.gettrace()
        started = time.monotonic()
        try:
            sys.settrace(trace)
            confirmed = processes.terminate_process_group(process)
        finally:
            sys.settrace(previous)
        elapsed = time.monotonic() - started
        self.events.append(
            {
                "diagnostic": "fixture sequence only; recovery is separate",
                "confirmed": confirmed,
                "elapsed": elapsed,
                "leader_state": self.native_state(process.pid, self.created_births[process.pid]),
                "marker_retained": bool(list(self.store.root.glob("*.json"))),
                "platform": sys.platform,
            }
        )
        self.assertLess(elapsed, 5)
        self.assert_peer()
        if confirmed:
            self.store.remove(self.created[-1][1])
            self.assert_reaped(process)
        # A diagnostic return is never counted as proof of product cleanup.


if __name__ == "__main__":
    unittest.main()
