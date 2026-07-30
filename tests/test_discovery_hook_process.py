"""Step 1: capped, owned hook execution -- deadlines, caps, descriptors, descendants.

Every test here runs a real child process. The point of the fake hook fixture is that
process ownership, deadlines, output capping, and descriptor handling are genuinely
exercised; mocking them would test nothing that matters.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from pyocd_debug_mcp import discovery_hooks
from pyocd_debug_mcp.discovery_hooks import execute_hook, load_hook_snapshot
from pyocd_debug_mcp.kernel.processes import ProcessMarkerStore
from tests.discovery_hook_fixtures import (
    hook_entry,
    open_handle_count,
    single_spec,
    snapshot_for,
    write_manifest,
)


def _module_ast() -> ast.Module:
    return ast.parse(Path(discovery_hooks.__file__).read_text(encoding="utf-8"))


def _code_text() -> str:
    """The module's source with docstrings and comments about `run_owned` removed."""

    tree = _module_ast()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    return ast.unparse(tree)


def _called_names() -> set[str]:
    """Every attribute or plain name actually invoked in the module."""

    names: set[str] = set()
    for node in ast.walk(_module_ast()):
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name):
                names.add(function.id)
            elif isinstance(function, ast.Attribute):
                names.add(function.attr)
    return names


def _imported_names() -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_module_ast()):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _popen_keywords() -> dict[str, str]:
    """The keyword arguments the module passes to `popen_owned`."""

    for node in ast.walk(_module_ast()):
        if isinstance(node, ast.Call):
            function = node.func
            name = getattr(function, "id", None) or getattr(function, "attr", None)
            if name == "popen_owned":
                return {
                    keyword.arg: ast.unparse(keyword.value)
                    for keyword in node.keywords
                    if keyword.arg
                }
    raise AssertionError("discovery_hooks does not call popen_owned")


class _HookProcessCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self._markers = tempfile.TemporaryDirectory()
        self.addCleanup(self._markers.cleanup)
        self.marker_store = ProcessMarkerStore(Path(self._markers.name))

    def marker_files(self) -> list[Path]:
        return sorted(Path(self._markers.name).glob("*.json"))

    def spec(
        self,
        mode: str,
        *extra: str,
        kind: str = "probe",
        timeout_seconds: float = 15.0,
    ) -> discovery_hooks.DiscoveryHookSpec:
        return single_spec(
            self.root, kind, [mode, *extra], timeout_seconds=timeout_seconds
        )

    def run_hook(self, *args: str, **kwargs: object) -> discovery_hooks.HookExecution:
        spec = self.spec(*args, **kwargs)  # type: ignore[arg-type]
        return execute_hook(spec, marker_store=self.marker_store)


class SuccessPathTests(_HookProcessCase):
    def test_probe_hook_output_is_parsed(self) -> None:
        execution = self.run_hook("probe")

        self.assertTrue(execution.ok, execution.failure_detail)
        self.assertEqual(execution.outcome, "exited")
        self.assertEqual(execution.exit_code, 0)
        self.assertIsNone(execution.failure_code)
        assert execution.output is not None
        self.assertEqual(execution.output.probes[0].provider, "cmsisdap")
        self.assertEqual(execution.output.uarts, ())

    def test_uart_hook_output_is_parsed(self) -> None:
        execution = self.run_hook("uart", kind="uart")

        self.assertTrue(execution.ok, execution.failure_detail)
        assert execution.output is not None
        self.assertEqual(execution.output.uarts[0].port_path, "COM7")
        self.assertTrue(execution.output.uarts[0].has_stable_identity)

    def test_empty_result_is_success_not_failure(self) -> None:
        execution = self.run_hook("probe_empty")

        self.assertTrue(execution.ok, execution.failure_detail)
        assert execution.output is not None
        self.assertEqual(execution.output.probes, ())

    def test_successful_run_removes_its_ownership_marker(self) -> None:
        execution = self.run_hook("probe")

        self.assertTrue(execution.ok)
        self.assertEqual(self.marker_files(), [])

    def test_child_receives_utf8_io_encoding(self) -> None:
        recorded = self.root / "env.txt"

        execution = self.run_hook("record_env", str(recorded))

        self.assertTrue(execution.ok, execution.failure_detail)
        self.assertEqual(recorded.read_text(encoding="utf-8"), "utf-8")

    def test_noisy_stderr_does_not_prevent_a_successful_parse(self) -> None:
        execution = self.run_hook("noisy_stderr")

        self.assertTrue(execution.ok, execution.failure_detail)
        self.assertLessEqual(
            len(execution.stderr_excerpt), discovery_hooks.MAX_DIAGNOSTIC_CHARS + 32
        )


class DeadlineTests(_HookProcessCase):
    def test_timeout_is_reported_and_the_group_is_killed(self) -> None:
        started = time.monotonic()

        execution = self.run_hook("hang", timeout_seconds=1.0)

        elapsed = time.monotonic() - started
        self.assertEqual(execution.outcome, "timeout")
        self.assertEqual(execution.failure_code, "discovery/hook-timeout")
        self.assertIn("deadline", execution.failure_detail)
        self.assertIsNone(execution.output)
        self.assertLess(elapsed, 20.0, "the deadline did not bound the run")
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertEqual(self.marker_files(), [])

    def test_timeout_reports_the_configured_deadline(self) -> None:
        execution = self.run_hook("hang", timeout_seconds=1.0)

        self.assertEqual(execution.timeout_seconds, 1.0)
        self.assertIn("1s", execution.failure_detail)

    def test_output_written_before_a_timeout_does_not_make_it_a_success(self) -> None:
        execution = self.run_hook("hang_after_output", timeout_seconds=1.0)

        self.assertEqual(execution.outcome, "timeout")
        self.assertFalse(execution.ok)
        self.assertIsNone(execution.output)

    def test_timeout_kills_descendants_not_only_the_leader(self) -> None:
        pid_file = self.root / "descendant.pid"

        execution = self.run_hook("spawn_child", str(pid_file), timeout_seconds=2.0)

        self.assertEqual(execution.outcome, "timeout")
        descendant = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not self._alive(descendant):
                break
            time.sleep(0.05)
        self.assertFalse(
            self._alive(descendant),
            f"descendant {descendant} survived the owned-group termination",
        )

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return False
        try:
            return process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    def test_a_hook_reading_stdin_gets_eof_instead_of_hanging(self) -> None:
        started = time.monotonic()

        execution = self.run_hook("read_stdin", timeout_seconds=10.0)

        self.assertTrue(execution.ok, execution.failure_detail)
        assert execution.output is not None
        self.assertEqual(execution.output.probes[0].description, "stdin len 0")
        self.assertLess(time.monotonic() - started, 10.0)


class OutputCapTests(_HookProcessCase):
    def test_oversized_stdout_is_truncated_and_the_process_is_still_reaped(self) -> None:
        process = psutil.Process()
        before_rss = process.memory_info().rss

        execution = self.run_hook("flood", "50", timeout_seconds=60.0)

        growth = process.memory_info().rss - before_rss
        self.assertEqual(execution.outcome, "parse_failed")
        self.assertTrue(execution.stdout_truncated)
        self.assertEqual(execution.failure_code, "discovery/hook-output-invalid")
        self.assertIn("truncated", execution.failure_detail)
        self.assertEqual(execution.exit_code, 0, "the flooding child was not reaped")
        self.assertEqual(self.marker_files(), [])
        # 50 MB was written; peak memory must not track output size.
        self.assertLess(
            growth,
            8 * 1024 * 1024,
            f"resident memory grew {growth} bytes while capturing 50 MB of stdout",
        )

    def test_a_hook_that_never_stops_writing_is_still_bounded_by_its_deadline(self) -> None:
        """Draining past the cap is what stops the child blocking on a full pipe."""

        process = psutil.Process()
        before_rss = process.memory_info().rss
        started = time.monotonic()

        execution = self.run_hook("flood_forever", timeout_seconds=2.0)

        elapsed = time.monotonic() - started
        growth = process.memory_info().rss - before_rss
        self.assertEqual(execution.outcome, "timeout")
        self.assertLess(elapsed, 20.0, "an endless writer was not bounded by the deadline")
        self.assertLess(growth, 16 * 1024 * 1024, f"resident memory grew {growth} bytes")
        self.assertEqual(self.marker_files(), [])

    def test_captured_stdout_excerpt_is_bounded(self) -> None:
        execution = self.run_hook("flood", "2", timeout_seconds=60.0)

        self.assertLessEqual(
            len(execution.stdout_excerpt), discovery_hooks.MAX_DIAGNOSTIC_CHARS + 32
        )

    def test_oversized_stderr_is_bounded(self) -> None:
        execution = self.run_hook("noisy_stderr")

        self.assertLessEqual(
            len(execution.stderr_excerpt), discovery_hooks.MAX_DIAGNOSTIC_CHARS + 32
        )


class FailureClassTests(_HookProcessCase):
    def test_nonzero_exit_is_hook_failed(self) -> None:
        execution = self.run_hook("nonzero")

        self.assertEqual(execution.outcome, "exited")
        self.assertEqual(execution.exit_code, 3)
        self.assertEqual(execution.failure_code, "discovery/hook-failed")
        self.assertIn("refused to enumerate", execution.stderr_excerpt)

    def test_nonzero_exit_with_valid_output_is_still_a_failure(self) -> None:
        execution = self.run_hook("nonzero_with_valid_output")

        self.assertFalse(execution.ok)
        self.assertEqual(execution.failure_code, "discovery/hook-failed")
        self.assertIsNone(execution.output)

    def test_malformed_utf8_is_parse_failed_not_hook_failed(self) -> None:
        execution = self.run_hook("bad_utf8")

        self.assertEqual(execution.outcome, "parse_failed")
        self.assertEqual(execution.exit_code, 0)
        self.assertEqual(execution.failure_code, "discovery/hook-output-invalid")
        self.assertIn("UTF-8", execution.failure_detail)

    def test_malformed_json_is_parse_failed_not_hook_failed(self) -> None:
        execution = self.run_hook("bad_json")

        self.assertEqual(execution.outcome, "parse_failed")
        self.assertEqual(execution.failure_code, "discovery/hook-output-invalid")
        self.assertIn("JSON", execution.failure_detail)

    def test_every_failure_class_is_distinct(self) -> None:
        outcomes = {
            "nonzero": self.run_hook("nonzero").outcome,
            "bad_utf8": self.run_hook("bad_utf8").outcome,
            "hang": self.run_hook("hang", timeout_seconds=1.0).outcome,
        }

        self.assertEqual(
            outcomes, {"nonzero": "exited", "bad_utf8": "parse_failed", "hang": "timeout"}
        )
        self.assertEqual(len(set(outcomes.values())), 3)

    def test_unknown_output_field_is_parse_failed(self) -> None:
        execution = self.run_hook("unknown_field")

        self.assertEqual(execution.outcome, "parse_failed")
        self.assertIn("executable", execution.failure_detail)

    def test_a_uart_document_from_a_probe_hook_is_parse_failed(self) -> None:
        execution = self.run_hook("wrong_kind")

        self.assertEqual(execution.outcome, "parse_failed")
        self.assertIn("kind must be 'probe'", execution.failure_detail)

    def test_row_count_over_the_cap_is_parse_failed(self) -> None:
        execution = self.run_hook("too_many_rows")

        self.assertEqual(execution.outcome, "parse_failed")
        self.assertIn(str(discovery_hooks.MAX_HOOK_ROWS), execution.failure_detail)

    def test_authority_injection_in_output_is_parse_failed(self) -> None:
        execution = self.run_hook("authority_injection")

        self.assertEqual(execution.outcome, "parse_failed")
        self.assertIn("active_plan", execution.failure_detail)

    def test_unknown_mode_reports_the_nonzero_exit_faithfully(self) -> None:
        execution = self.run_hook("no_such_mode")

        self.assertEqual(execution.outcome, "exited")
        self.assertEqual(execution.exit_code, 2)
        self.assertEqual(execution.failure_code, "discovery/hook-failed")


class CleanupFailureTests(_HookProcessCase):
    def test_unconfirmed_cleanup_is_reported_and_keeps_the_marker(self) -> None:
        spec = self.spec("probe")

        with patch.object(discovery_hooks, "terminate_process_group", return_value=False):
            execution = execute_hook(spec, marker_store=self.marker_store)

        self.assertEqual(execution.outcome, "cleanup_failed")
        self.assertEqual(execution.failure_code, "discovery/hook-failed")
        self.assertIn("cleanup could not be confirmed", execution.failure_detail)
        self.assertIsNone(execution.output, "unconfirmed cleanup must not yield rows")
        self.assertEqual(
            len(self.marker_files()), 1, "the recovery marker must be retained"
        )

    def test_confirmed_cleanup_removes_the_marker(self) -> None:
        spec = self.spec("probe")

        execute_hook(spec, marker_store=self.marker_store)

        self.assertEqual(self.marker_files(), [])

    def test_cancellation_mid_run_still_terminates_the_group(self) -> None:
        """A BaseException during wait() must not outlive the ownership marker."""

        pid_file = self.root / "descendant.pid"
        spec = self.spec("spawn_child", str(pid_file), timeout_seconds=30.0)
        real_terminate = discovery_hooks.terminate_process_group
        observed: list[int] = []

        def recording_terminate(process: subprocess.Popen[bytes]) -> bool:
            observed.append(process.pid)
            return real_terminate(process)

        class _Cancelled(BaseException):
            pass

        original_wait = subprocess.Popen.wait
        raised = False

        def cancel_once(self_: subprocess.Popen[bytes], timeout: float | None = None) -> int:
            # Only the deadline wait is cancelled. `terminate_process_group` waits too,
            # and breaking its waits would test the harness rather than the product.
            nonlocal raised
            if not raised:
                # Cancel genuinely *mid-run*: wait until the hook has spawned its own
                # descendant, so group termination has something to prove.
                deadline = time.monotonic() + 20.0
                while time.monotonic() < deadline and not pid_file.exists():
                    time.sleep(0.02)
                raised = True
                raise _Cancelled
            return original_wait(self_, timeout=timeout)

        with patch.object(
            discovery_hooks, "terminate_process_group", recording_terminate
        ), patch.object(subprocess.Popen, "wait", cancel_once):
            with self.assertRaises(_Cancelled):
                execute_hook(spec, marker_store=self.marker_store)

        self.assertEqual(len(observed), 1, "cancellation did not terminate the group")
        self.assertEqual(
            self.marker_files(), [], "a confirmed cancellation cleanup left a marker"
        )
        descendant = int((self.root / "descendant.pid").read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and DeadlineTests._alive(descendant):
            time.sleep(0.05)
        self.assertFalse(
            DeadlineTests._alive(descendant),
            "cancellation mid-run left a descendant behind",
        )


class DescriptorLeakTests(_HookProcessCase):
    def test_repeated_execution_does_not_leak_descriptors(self) -> None:
        """`communicate()` closed the pipes for us; the hand-rolled reader must too.

        Hooks run on setup, connect, validate, and status paths, so a leak of two
        descriptors per execution would exhaust the process over a long server run.
        """

        spec = self.spec("probe")
        for _warmup in range(20):
            execute_hook(spec, marker_store=self.marker_store)

        baseline = open_handle_count()
        counts = [baseline]
        for _batch in range(3):
            for _run in range(100):
                execute_hook(spec, marker_store=self.marker_store)
            counts.append(open_handle_count())

        growth = counts[-1] - baseline
        self.assertLess(
            growth,
            50,
            f"handle count grew by {growth} over 300 executions: {counts}",
        )
        # A two-descriptor-per-run leak would be ~600; assert the tail is flat too.
        self.assertLess(counts[-1] - counts[-2], 25, f"handle count still climbing: {counts}")

    def test_failing_runs_do_not_leak_descriptors_either(self) -> None:
        specs = [self.spec("nonzero"), self.spec("bad_json"), self.spec("probe")]
        for spec in specs:
            for _warmup in range(5):
                execute_hook(spec, marker_store=self.marker_store)

        baseline = open_handle_count()
        for _run in range(40):
            for spec in specs:
                execute_hook(spec, marker_store=self.marker_store)

        growth = open_handle_count() - baseline
        self.assertLess(growth, 40, f"handle count grew by {growth} over 120 failing runs")


class DeterminismTests(_HookProcessCase):
    def test_execution_order_is_deterministic_across_repeated_snapshots(self) -> None:
        entries = [
            hook_entry("zulu", "probe", argv=["probe"]),
            hook_entry("alpha", "probe", argv=["probe"]),
            hook_entry("mike", "probe", argv=["probe"]),
            hook_entry("bravo", "uart", argv=["uart"]),
        ]
        write_manifest(self.root, entries)

        orders = []
        for _repeat in range(5):
            snapshot = load_hook_snapshot(self.root, environ={})
            orders.append([hook.hook_id for hook in snapshot.eligible("probe", "linux")])

        self.assertEqual(orders[0], ["alpha", "mike", "zulu"])
        self.assertEqual(len(set(map(tuple, orders))), 1)

    def test_execute_eligible_hooks_runs_each_hook_once_in_order(self) -> None:
        snapshot = snapshot_for(
            self.root,
            [
                hook_entry("second", "probe", argv=["probe_uid", "uid-second"]),
                hook_entry("first", "probe", argv=["probe_uid", "uid-first"]),
            ],
        )

        executions = discovery_hooks.execute_eligible_hooks(
            snapshot, "probe", platform="linux", marker_store=self.marker_store
        )

        self.assertEqual([execution.hook_id for execution in executions], ["first", "second"])
        self.assertEqual(
            [execution.output.probes[0].unique_id for execution in executions],  # type: ignore[union-attr]
            ["uid-first", "uid-second"],
        )

    def test_only_the_requested_kind_is_executed(self) -> None:
        launches = self.root / "launches.txt"
        snapshot = snapshot_for(
            self.root,
            [
                hook_entry("probe-one", "probe", argv=["probe"]),
                hook_entry("uart-one", "uart", argv=["record_launch", str(launches)]),
            ],
        )

        discovery_hooks.execute_eligible_hooks(
            snapshot, "probe", platform="linux", marker_store=self.marker_store
        )

        self.assertFalse(launches.exists(), "a uart hook ran during a probe scan")

    def test_ineligible_platform_hooks_are_never_launched(self) -> None:
        launches = self.root / "launches.txt"
        snapshot = snapshot_for(
            self.root,
            [
                hook_entry(
                    "win-only",
                    "probe",
                    argv=["record_launch", str(launches)],
                    platforms=["windows"],
                )
            ],
        )

        discovery_hooks.execute_eligible_hooks(
            snapshot, "probe", platform="linux", marker_store=self.marker_store
        )

        self.assertFalse(launches.exists())


class PlatformSpecificTests(_HookProcessCase):
    def test_a_hook_path_containing_spaces_runs(self) -> None:
        """argv is passed directly, so a space in the path needs no quoting."""

        spaced = self.root / "hook directory with spaces"
        spaced.mkdir()
        import shutil

        from tests.discovery_hook_fixtures import FAKE_HOOK

        shutil.copy(FAKE_HOOK, spaced / "my hook.py")
        write_manifest(
            self.root,
            [
                hook_entry(
                    "spaced",
                    "probe",
                    argv=["probe"],
                    entrypoint="hook directory with spaces/my hook.py",
                )
            ],
        )
        snapshot = load_hook_snapshot(self.root, environ={})

        execution = execute_hook(snapshot.hooks[0], marker_store=self.marker_store)

        self.assertTrue(execution.ok, execution.failure_detail)
        self.assertIn(" ", str(snapshot.hooks[0].entrypoint))

    @unittest.skipIf(os.name == "nt", "POSIX executable-permission semantics")
    def test_executable_runner_without_the_execute_bit_fails_cleanly(self) -> None:
        tool = self.root / "vendor_tool.sh"
        tool.write_text("#!/bin/sh\necho '{}'\n", encoding="utf-8")
        tool.chmod(0o644)
        registry_root = self.root / "operator"
        registry_root.mkdir()
        write_manifest(
            registry_root,
            [hook_entry("vendor", "probe", runner="executable", entrypoint=str(tool))],
            filename="registry.json",
        )
        snapshot = load_hook_snapshot(
            self.root,
            environ={
                discovery_hooks.DISCOVERY_HOOK_REGISTRY_ENV: str(
                    registry_root / "registry.json"
                )
            },
        )

        with self.assertRaises(PermissionError):
            execute_hook(snapshot.hooks[0], marker_store=self.marker_store)

    @unittest.skipIf(os.name == "nt", "POSIX executable-permission semantics")
    def test_executable_runner_with_the_execute_bit_runs(self) -> None:
        tool = self.root / "vendor_tool.sh"
        tool.write_text(
            '#!/bin/sh\nprintf \'{"schema_version":1,"kind":"probe","probes":[]}\'\n',
            encoding="utf-8",
        )
        tool.chmod(0o755)
        registry_root = self.root / "operator"
        registry_root.mkdir()
        write_manifest(
            registry_root,
            [hook_entry("vendor", "probe", runner="executable", entrypoint=str(tool))],
            filename="registry.json",
        )
        snapshot = load_hook_snapshot(
            self.root,
            environ={
                discovery_hooks.DISCOVERY_HOOK_REGISTRY_ENV: str(
                    registry_root / "registry.json"
                )
            },
        )

        execution = execute_hook(snapshot.hooks[0], marker_store=self.marker_store)

        self.assertTrue(execution.ok, execution.failure_detail)

    def test_no_shell_is_ever_used(self) -> None:
        """popen_owned refuses shell=True; assert the hook path never asks for it."""

        for keyword in _popen_keywords():
            self.assertNotEqual(keyword, "shell")
        self.assertNotIn("os.system", _code_text())

    def test_no_creationflags_are_passed_by_hand(self) -> None:
        """process_group_options owns creationflags; OR-ing in our own would fight it."""

        self.assertNotIn("creationflags", _popen_keywords())
        self.assertNotIn("CREATE_NO_WINDOW", _code_text())

    def test_run_owned_is_not_used_for_hooks(self) -> None:
        """Trap 1: communicate() buffers without limit, so run_owned is unusable here.

        Asserted against parsed calls and imports rather than raw text, so the module
        docstring may keep explaining *why* run_owned is unusable.
        """

        called = _called_names()

        self.assertNotIn("run_owned", called)
        self.assertNotIn("communicate", called)
        self.assertIn("popen_owned", called)
        self.assertNotIn("run_owned", _imported_names())
        self.assertIn("popen_owned", _imported_names())

    def test_stdin_is_devnull(self) -> None:
        self.assertEqual(_popen_keywords().get("stdin"), "subprocess.DEVNULL")


class InterpreterTests(_HookProcessCase):
    def test_server_python_hooks_run_under_the_servers_interpreter(self) -> None:
        spec = self.spec("probe")

        self.assertEqual(spec.command()[0], sys.executable)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
