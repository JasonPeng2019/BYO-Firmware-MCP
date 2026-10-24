from __future__ import annotations

import json
import functools
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
from io import BytesIO
from pathlib import Path
from queue import Queue
from threading import Event, RLock
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from pyocd_debug_mcp.timeouts import DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS
from tests.process_cleanup_observations import (
    CleanupTrace,
    execution_relationship,
    marker_facts,
    popen_facts,
    process_facts,
    read_receipt,
)
from tests.test_process_cleanup import _NativePosixCase

OBSERVATIONS = {}


def observe_cleanup(method):
    """Observe the real routes only for these cases; retain the original failure."""

    @functools.wraps(method)
    def observed(self):
        from pyocd_debug_mcp.adapters import swd_process
        from pyocd_debug_mcp.kernel import processes

        with ExitStack() as stack:
            directory = os.environ.get("BYO_FINAL_CLEANUP_EVIDENCE")
            if directory is None:
                directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="swd-cleanup-"))
            trace = CleanupTrace(self.id(), directory)
            self.cleanup_trace = trace
            worker_receipt = trace.path.with_suffix(".worker.jsonl")
            stack.enter_context(
                patch.dict(os.environ, {"BYO_FAKE_PROVIDER_RECEIPT": str(worker_receipt)})
            )
            created = []
            known_births = {}

            def known_identity(process):
                birth = known_births.get(id(process))
                return (
                    process_facts(process.pid, birth)
                    if birth is not None
                    else {
                        "pid": process.pid,
                        "gap": "Spawn birth unavailable; no PID-only attribution",
                    }
                )

            def snapshot(client, boundary):
                process = getattr(client, "_process", None)
                trace.record(
                    "client-boundary",
                    boundary=boundary,
                    closed=getattr(client, "_closed", None),
                    cleanup_confirmed=getattr(client, "_cleanup_confirmed", None),
                    request_id=getattr(client, "_request_id", None),
                    marker=marker_facts(getattr(client, "_marker", None)),
                    popen=None if process is None else popen_facts(process),
                    identity=None if process is None else known_identity(process),
                )

            original_popen = swd_process.popen_owned

            def popen(*args, **kwargs):
                result = trace.invoke("popen_owned", original_popen, *args, **kwargs)
                process, marker = result
                identity = trace.role(
                    process.pid, "test-provider-worker", "actual popen_owned return"
                )
                known_births[id(process)] = identity.get("creation_time")
                created.append((process, marker))
                trace.record("owned-start", popen=popen_facts(process), marker=marker_facts(marker))
                for name in ("poll", "wait", "terminate", "kill", "send_signal"):
                    original = getattr(process, name)
                    stack.enter_context(
                        patch.object(process, name, trace.delegate("Popen." + name, original))
                    )
                for stream_name, names in (
                    ("stdin", ("write", "flush", "close")),
                    ("stdout", ("read", "close")),
                ):
                    stream = getattr(process, stream_name)
                    if stream is not None:
                        for name in names:
                            stack.enter_context(
                                patch.object(
                                    stream,
                                    name,
                                    trace.delegate(stream_name + "." + name, getattr(stream, name)),
                                )
                            )
                return result

            stack.enter_context(patch.object(swd_process, "popen_owned", popen))
            stack.enter_context(
                patch.object(
                    swd_process,
                    "terminate_process_group",
                    trace.delegate("terminate_process_group", swd_process.terminate_process_group),
                )
            )
            stack.enter_context(
                patch.object(
                    swd_process.ProcessMarkerStore,
                    "remove",
                    staticmethod(
                        trace.delegate(
                            "ProcessMarkerStore.remove", swd_process.ProcessMarkerStore.remove
                        )
                    ),
                )
            )
            if hasattr(processes.os, "killpg"):
                stack.enter_context(
                    patch.object(
                        processes.os, "killpg", trace.delegate("os.killpg", processes.os.killpg)
                    )
                )
            if hasattr(processes, "_close_windows_job"):
                stack.enter_context(
                    patch.object(
                        processes,
                        "_close_windows_job",
                        trace.delegate("_close_windows_job", processes._close_windows_job),
                    )
                )

            def client_delegate(name, original):
                @functools.wraps(original)
                def call(client, *args, **kwargs):
                    # Full identity/marker snapshots at causal boundaries; actual
                    # _read/_write and pipe calls retain their own raw results.
                    boundary = name in {"close", "_terminate", "_invalidate"}
                    if boundary:
                        snapshot(client, name + ":enter")
                    try:
                        return trace.invoke(
                            "WorkerClient." + name, original, client, *args, **kwargs
                        )
                    finally:
                        if boundary:
                            snapshot(client, name + ":finally")

                return call

            for name in (
                "call",
                "close",
                "_read",
                "_write",
                "_terminate",
                "_invalidate",
                "_close_pipes",
            ):
                stack.enter_context(
                    patch.object(
                        swd_process._WorkerClient,
                        name,
                        client_delegate(name, getattr(swd_process._WorkerClient, name)),
                    )
                )
            try:
                return method(self)
            finally:
                for process, marker in created:
                    trace.record(
                        "test-finally",
                        popen=popen_facts(process),
                        identity=known_identity(process),
                        marker=marker_facts(marker),
                    )
                helper = read_receipt(worker_receipt)
                helper["lifecycle_gaps"] = [
                    {
                        "pid": process.pid,
                        "gap": "No helper-finally receipt; signal/abrupt exit may prevent it",
                    }
                    for process, _ in created
                    if not any(
                        row.get("kind") == "helper-finally" and row.get("pid") == process.pid
                        for row in helper.get("records", [])
                    )
                ]
                OBSERVATIONS[self.id()] = {"cleanup": trace.summary(), "helper": helper}

    return observed


class ProcessIsolationContractTests(unittest.TestCase):
    def test_worker_bootstrap_isolates_import_and_between_request_stdout(self) -> None:
        script = r"""
import builtins
import json
import os
import sys
import types

from pyocd_debug_mcp.adapters import provider_worker

original_import = builtins.__import__

def injected_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "pyocd_debug_mcp.adapters.provider_worker_runtime":
        print("import-python", flush=True)
        os.write(1, b"import-fd\n")
        runtime = types.ModuleType(name)

        def run(protocol):
            protocol.write(b'{"version":1,"ready":true}\n')
            protocol.flush()
            first = json.loads(sys.stdin.buffer.readline())
            protocol.write(
                (json.dumps({"version": 1, "request_id": first["request_id"],
                             "ok": True, "result": "first"}, separators=(",", ":")) + "\n").encode()
            )
            protocol.flush()
            print("idle-python", flush=True)
            os.write(1, b"idle-fd\n")
            second = json.loads(sys.stdin.buffer.readline())
            protocol.write(
                (json.dumps({"version": 1, "request_id": second["request_id"],
                             "ok": True, "result": "second"}, separators=(",", ":")) + "\n").encode()
            )
            protocol.flush()

        runtime.main = run
        return runtime
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = injected_import
provider_worker.main()
"""
        requests = (
            '{"version":1,"request_id":1,"operation":"one","arguments":{}}\n'
            '{"version":1,"request_id":2,"operation":"two","arguments":{}}\n'
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            input=requests,
            capture_output=True,
            text=True,
            timeout=DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [json.loads(line) for line in result.stdout.splitlines()],
            [
                {"version": 1, "ready": True},
                {"version": 1, "request_id": 1, "ok": True, "result": "first"},
                {"version": 1, "request_id": 2, "ok": True, "result": "second"},
            ],
        )
        for marker in ("import-python", "import-fd", "idle-python", "idle-fd"):
            self.assertIn(marker, result.stderr)

    def test_importing_worker_bootstrap_does_not_redirect_test_process(self) -> None:
        before = sys.stdout
        from pyocd_debug_mcp.adapters import provider_worker

        self.assertIsNotNone(provider_worker.main)
        self.assertIs(sys.stdout, before)

    def test_backend_stdout_is_forwarded_to_inherited_stderr_at_both_layers(self) -> None:
        script = r"""
import os
import sys
from pyocd_debug_mcp.adapters.swd_pyocd import _backend_stdout_to_stderr

with _backend_stdout_to_stderr():
    print("python-stdout", flush=True)
    os.write(1, b"fd-stdout\n")
    print("python-stderr", file=sys.stderr, flush=True)
    os.write(2, b"fd-stderr\n")
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        for marker in ("python-stdout", "fd-stdout", "python-stderr", "fd-stderr"):
            self.assertIn(marker, result.stderr)

    def test_selection_and_discovery_chatter_cannot_corrupt_protocol_result(self) -> None:
        script = r"""
import json
import os
from unittest.mock import patch

from pyocd_debug_mcp.adapters import swd_pyocd
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.target_errors import TargetConnectionError

def noisy_selection(*_args, **_kwargs):
    print("selection-python", flush=True)
    os.write(1, b"selection-fd\n")
    raise TargetConnectionError("typed selection failure")

def noisy_discovery(**_kwargs):
    print("discovery-python", flush=True)
    os.write(1, b"discovery-fd\n")
    return []

board = BoardConfig(
    board_id="board",
    display_name="Board",
    mcu_family="family",
    probe_family="jlink",
    pyocd_target="part",
    probe_type="jlink",
    probe_hint_terms=(),
    serial_hint_terms=(),
    test_addr=0,
)
payload = {}
with (
    patch.object(swd_pyocd, "_choose_unique_probe", noisy_selection),
    patch.object(swd_pyocd.ConnectHelper, "get_all_connected_probes", noisy_discovery),
    patch.object(swd_pyocd, "list_connected_probes_cli", return_value=[]),
):
    try:
        swd_pyocd.PyOCDSWDInterface._choose_session(probe_uid="probe", options=None)
    except TargetConnectionError as exc:
        payload["error"] = str(exc)
        payload["kind"] = type(exc).__name__
    payload["visible"] = swd_pyocd._single_matching_probe_visible_for_board_family(board)

print(json.dumps(payload, separators=(",", ":")))
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["kind"], "TargetConnectionError")
        self.assertEqual(payload["error"], "typed selection failure")
        self.assertFalse(payload["visible"])
        for marker in (
            "selection-python",
            "selection-fd",
            "discovery-python",
            "discovery-fd",
        ):
            self.assertIn(marker, result.stderr)

    @staticmethod
    def _client(mode: str, *, deadline: float | None = None):
        from pyocd_debug_mcp.adapters.swd_process import _WorkerClient

        worker = Path(__file__).with_name("fake_provider_worker.py")
        return _WorkerClient(
            worker_argv=(sys.executable, str(worker), mode),
            deadline=deadline,
        )

    def test_process_backend_exposes_complete_swd_surface(self) -> None:
        from pyocd_debug_mcp.adapters.swd_process import ProcessIsolatedSWDInterface

        required = {
            "open",
            "close",
            "connect_under_reset",
            "get_state",
            "read_memory",
            "read_memory_block",
            "write_memory",
            "read_core_register",
            "write_core_register",
            "supported_core_registers",
            "halt",
            "resume",
            "step",
            "reset",
            "reset_and_halt",
            "release_reset",
            "flash",
            "recover",
            "supports_recovery",
            "set_breakpoint",
            "remove_breakpoint",
        }
        self.assertTrue(required.issubset(set(dir(ProcessIsolatedSWDInterface))))

    def test_parent_production_modules_do_not_dereference_native_handle_session(self) -> None:
        root = Path(__file__).parents[1] / "src" / "pyocd_debug_mcp"
        offenders = []
        for path in root.rglob("*.py"):
            if path.name in {"swd_pyocd.py", "provider_worker_runtime.py"}:
                continue
            if "handle.session" in path.read_text(encoding="utf-8"):
                offenders.append(path.relative_to(root).as_posix())
        self.assertEqual(offenders, [])

    def test_result_contracts_preserve_large_valid_values_and_exact_block_length(self) -> None:
        from pyocd_debug_mcp.adapters.swd_process import _validate_result

        long_text = "x" * 20_000
        registers = [f"register_{index}_{long_text}" for index in range(300)]
        metadata = {
            "board_name": long_text,
            "probe_description": long_text,
            "probe_family": "jlink",
            "probe_uid": None,
            "live_part_number": None,
            "route_used": "pyocd-native",
            "target_override": long_text,
            "runtime_token": long_text,
        }
        block = [index & 0xFF for index in range(70_000)]

        self.assertEqual(_validate_result("open", metadata), metadata)
        self.assertEqual(_validate_result("supported_core_registers", registers), registers)
        self.assertEqual(
            _validate_result("read_memory_block", block, {"address": 0, "length": len(block)}),
            block,
        )
        with self.assertRaisesRegex(ValueError, "length did not match"):
            _validate_result("read_memory_block", block[:-1], {"address": 0, "length": len(block)})
        with self.assertRaises(ValueError):
            _validate_result("read_memory", 256, {"address": 0, "width_bits": 8})
        with self.assertRaises(ValueError):
            _validate_result("halt", "unexpected")

    def test_reader_accepts_large_complete_frames_and_partial_frames_timeout(self) -> None:
        from pyocd_debug_mcp.adapters.swd_process import _WorkerClient
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        responses: Queue[object] = Queue()
        payload = {"value": "x" * 1_500_000}
        _WorkerClient._start_reader(
            BytesIO((json.dumps(payload) + "\n").encode("utf-8")),
            responses,
        )
        self.assertEqual(responses.get(timeout=2), payload)

        partial = self._client("partial_reply")
        with self.assertRaises(TargetConnectionError):
            partial.call("get_state", {}, timeout=0.05)

    def test_wrong_id_malformed_frame_crash_and_timeout_are_terminal(self) -> None:
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        for mode in ("open_hang", "crash", "wrong_id", "malformed_reply"):
            with self.subTest(mode=mode):
                client = self._client(mode)
                with self.assertRaises(TargetConnectionError):
                    client.call("get_state", {}, timeout=0.05)
                with self.assertRaises(TargetConnectionError):
                    client.call("get_state", {}, timeout=0.05)

    def test_startup_and_first_call_share_one_absolute_deadline(self) -> None:
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        # D16: restores construction to outside assertRaises (it was moved in
        # Iteration 4, but that broke coverage). The invariant under test is
        # "call() honors the caller's deadline," and we must prove .call()
        # actually ran. Construction succeeds with an ample budget (0.27s
        # typical + variance). Call is then invoked with the same deadline,
        # which will expire while the worker hangs. Entry into .call() is
        # proven by the assertion below; respect for the deadline by the
        # escalation of TimeoutError to TargetConnectionError.
        started = time.monotonic()
        deadline = started + 2.0
        client = self._client("ready_then_hang", deadline=deadline)
        try:
            with self.assertRaises(TargetConnectionError):
                client.call("get_state", {}, deadline=deadline)
            # Prove .call() was entered by asserting request_id incremented.
            self.assertEqual(client._request_id, 1)
        finally:
            client.close()
        self.assertLess(time.monotonic() - started, 2.5)

    def test_ready_hang_is_bounded(self) -> None:
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        started = time.monotonic()
        with self.assertRaises(TargetConnectionError):
            self._client("ready_hang", deadline=started + 0.05)
        self.assertLess(time.monotonic() - started, 0.3)

    @observe_cleanup
    def test_typed_child_errors_are_preserved_without_retry(self) -> None:
        from pyocd_debug_mcp.target_errors import LockedTargetError, TargetControlError

        typed = self._client("typed_error")
        with self.assertRaisesRegex(TargetControlError, "fake target error"):
            typed.call("get_state", {}, timeout=1)
        typed.close()

        locked = self._client("locked_error")
        with self.assertRaisesRegex(LockedTargetError, "fake locked target"):
            locked.call("get_state", {}, timeout=1)
        locked.close()

    @observe_cleanup
    def test_confirmed_process_termination_makes_close_successful_and_idempotent(self) -> None:
        typed = self._client("typed_error")
        typed.close()
        typed.close()
        self.assertIsNotNone(typed._process.returncode)

        hanging = self._client("close_hang")
        hanging.close(deadline=time.monotonic() + 0.05)
        hanging.close()
        self.assertIsNotNone(hanging._process.returncode)

    @observe_cleanup
    def test_abrupt_locked_child_error_and_close_are_independent(self) -> None:
        from pyocd_debug_mcp.target_errors import LockedTargetError

        locked = self._client("locked_error")
        with self.assertRaisesRegex(LockedTargetError, "fake locked target"):
            locked.call("get_state", {}, timeout=1)
        self.assertEqual(locked._request_id, 1)
        # This may independently error. Never catch it to manufacture a PASS.
        locked.close()

    def _persistent_error_control(self, mode, error_type, message):
        client = self._client(mode)
        process = client._process
        marker = client._marker
        before = process_facts(process.pid)
        try:
            helper = read_receipt(self.cleanup_trace.path.with_suffix(".worker.jsonl"))
            self.assertNotIn("gap", helper)
            starts = [r for r in helper["records"] if r["kind"] == "helper-start"]
            self.assertEqual(len(starts), 1)
            start = starts[0]
            self.assertEqual(start["pid"], start["identity"]["pid"])
            worker_argv = [str(Path(__file__).with_name("fake_provider_worker.py")), mode]
            self.assertEqual(list(process.args), [sys.executable, *worker_argv])
            relationship = execution_relationship(
                before, start["identity"], process.args, start["argv"], worker_argv, parent=before
            )
            self.cleanup_trace.record("persistent-worker-start-identity", **relationship)
            self.assertFalse(relationship["evidence_gaps"], relationship)
            self.assertIn(relationship["relationship"], {"direct", "launcher-child"})
            for request_id in (1, 2):
                with self.assertRaisesRegex(error_type, message):
                    client.call("get_state", {}, timeout=1)
                self.assertEqual(client._request_id, request_id)
                self.assertFalse(client._closed)
                self.assertFalse(client._cleanup_confirmed)
                self.assertIsNone(process.returncode)
                current = process_facts(process.pid, before["creation_time"])
                self.assertEqual(current["identity"], "same_birth")
                self.assertEqual(current["birth_after_queries"], before["creation_time"])
                self.assertIsInstance(current["status"], str)
                self.assertNotEqual(current["status"], "zombie")
                interpreter = process_facts(start["pid"], start["identity"]["creation_time"])
                relationship = execution_relationship(
                    current, interpreter, process.args, start["argv"], worker_argv, parent=current
                )
                self.cleanup_trace.record(
                    "persistent-worker-error-identity", request_id=request_id, **relationship
                )
                self.assertEqual(interpreter["identity"], "same_birth")
                self.assertFalse(relationship["evidence_gaps"], relationship)
                self.assertIn(relationship["relationship"], {"direct", "launcher-child"})
                self.assertIsNotNone(marker)
                self.assertTrue(marker.is_file())
        finally:
            # Genuine product close; the observer never supplies its authority.
            client.close()
        self.assertTrue(client._cleanup_confirmed)
        self.assertIsNotNone(process.returncode)
        self.assertIsNone(client._marker)
        self.assertFalse(marker.exists())
        self.assertEqual(client._request_id, 3)
        close_operations = len(self.cleanup_trace.events)
        client.close()
        self.assertEqual(client._request_id, 3)
        later = self.cleanup_trace.events[close_operations:]
        self.assertFalse(any(e.get("operation") == "terminate_process_group" for e in later))
        # Join protocol requests to the self-identified interpreter, which can
        # be the owned launcher's specific child on Windows venv installations.
        helper = read_receipt(self.cleanup_trace.path.with_suffix(".worker.jsonl"))
        self.assertNotIn("gap", helper)
        requests = [r for r in helper["records"] if r["kind"] == "request"]
        self.assertEqual([r["request"]["request_id"] for r in requests], [1, 2, 3])
        self.assertEqual(
            [r["request"]["operation"] for r in requests], ["get_state", "get_state", "close"]
        )
        self.assertEqual({r["pid"] for r in requests}, {start["pid"]})

    @observe_cleanup
    def test_normal_persistent_typed_error_closes_and_retires_owned_worker(self) -> None:
        from pyocd_debug_mcp.target_errors import TargetControlError

        self._persistent_error_control(
            "persistent_typed_error", TargetControlError, "fake target error"
        )

    @observe_cleanup
    def test_normal_persistent_locked_error_closes_and_retires_owned_worker(self) -> None:
        from pyocd_debug_mcp.target_errors import LockedTargetError

        self._persistent_error_control(
            "persistent_locked_error", LockedTargetError, "fake locked target"
        )

    def test_actual_worker_dispatches_short_request_while_stdin_remains_open(self) -> None:
        from pyocd_debug_mcp.adapters.swd_process import _WorkerClient
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        client = _WorkerClient(deadline=time.monotonic() + 10)
        try:
            with self.assertRaisesRegex(TargetConnectionError, "no live target session"):
                client.call("get_state", {}, timeout=1)
        finally:
            client.close()

    def test_faulted_worker_does_not_interrupt_a_healthy_worker(self) -> None:
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        healthy = self._client("good")
        faulted = self._client("open_hang")
        try:
            self.assertEqual(healthy.call("get_state", {}, timeout=1), "RUNNING")
            with self.assertRaises(TargetConnectionError):
                faulted.call("get_state", {}, timeout=0.05)
            self.assertIsNotNone(faulted._process.returncode)
            self.assertEqual(healthy.call("get_state", {}, timeout=1), "RUNNING")
        finally:
            healthy.close()
        self.assertIsNotNone(healthy._process.returncode)

    def test_blocked_parent_write_has_a_hard_deadline(self) -> None:
        from pyocd_debug_mcp.adapters.swd_process import _WorkerClient

        released = Event()

        class BlockingPipe:
            def write(self, _frame: bytes) -> int:
                released.wait(1)
                return 0

            def flush(self) -> None:
                return None

        client = cast(Any, object.__new__(_WorkerClient))
        client._process = SimpleNamespace(stdin=BlockingPipe())
        try:
            with self.assertRaises(TimeoutError):
                client._write(b"request\n", time.monotonic() + 0.01)
        finally:
            released.set()

    def test_unconfirmed_termination_retains_recovery_marker(self) -> None:
        from pyocd_debug_mcp.adapters import swd_process
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        client = cast(Any, object.__new__(swd_process._WorkerClient))
        marker = Path("unconfirmed-worker-marker.json")
        client._closed = False
        client._cleanup_confirmed = False
        client._marker = marker
        client._process = SimpleNamespace(stdin=None, stdout=None)
        with (
            patch.object(swd_process, "terminate_process_group", return_value=False),
            patch.object(swd_process.ProcessMarkerStore, "remove") as remove,
            self.assertRaisesRegex(TargetConnectionError, "marker retained"),
        ):
            client._invalidate("forced cleanup failure")
        remove.assert_not_called()
        self.assertEqual(client._marker, marker)

    def test_marker_unlink_failure_is_typed_retained_and_retried_by_close(self) -> None:
        from pyocd_debug_mcp.adapters import swd_process
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        client = cast(Any, object.__new__(swd_process._WorkerClient))
        marker = Path("unlink-failed-worker-marker.json")
        client._guard = RLock()
        client._closed = False
        client._cleanup_confirmed = False
        client._marker = marker
        client._process = SimpleNamespace(stdin=None, stdout=None)
        client._terminate = Mock(return_value=True)

        with patch.object(
            swd_process.ProcessMarkerStore,
            "remove",
            side_effect=(OSError("marker unlink denied"), None),
        ) as remove:
            with self.assertRaises(TargetConnectionError) as raised:
                client._invalidate("Worker read_memory failed: EOFError: pipe closed.")

            self.assertIn("read_memory failed", str(raised.exception))
            self.assertIn("marker", str(raised.exception).casefold())
            self.assertIn("marker unlink denied", str(raised.exception))
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertTrue(client._cleanup_confirmed)
            self.assertEqual(client._marker, marker)

            client.close()

        self.assertIsNone(client._marker)
        self.assertEqual(remove.call_count, 2)
        client._terminate.assert_called_once_with()

    def test_open_rollback_preserves_primary_when_marker_cleanup_fails(self) -> None:
        from pyocd_debug_mcp.adapters import swd_process
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        primary = TargetConnectionError("primary worker open protocol failure")
        client = cast(Any, object.__new__(swd_process._WorkerClient))
        client._guard = RLock()
        client._closed = False
        client._cleanup_confirmed = False
        client._marker = Path("open-rollback-marker.json")
        client._process = SimpleNamespace(stdin=None, stdout=None)
        client.call = Mock(side_effect=primary)
        client._terminate = Mock(return_value=True)
        backend = swd_process.ProcessIsolatedSWDInterface()

        with (
            patch.object(swd_process, "_WorkerClient", return_value=client),
            patch.object(
                swd_process.ProcessMarkerStore,
                "remove",
                side_effect=(OSError("rollback marker unlink denied"), None),
            ) as remove,
        ):
            with self.assertRaises(TargetConnectionError) as raised:
                backend._open(
                    "open",
                    board=None,
                    unique_id=None,
                    target=None,
                    server_timeouts=None,
                    operation_timeout_seconds=1.0,
                )

            self.assertIs(raised.exception, primary)
            self.assertEqual(str(raised.exception), "primary worker open protocol failure")
            self.assertIsInstance(raised.exception.__cause__, TargetConnectionError)
            self.assertIn("rollback marker unlink denied", str(raised.exception.__cause__))
            self.assertEqual(client._marker, Path("open-rollback-marker.json"))
            self.assertTrue(client._cleanup_confirmed)
            client._terminate.assert_called_once_with()

            client.close()

            self.assertIsNone(client._marker)
            self.assertEqual(remove.call_count, 2)
            client._terminate.assert_called_once_with()

    def test_close_fails_only_when_process_termination_is_unconfirmed(self) -> None:
        from pyocd_debug_mcp.adapters import swd_process
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        client = cast(Any, object.__new__(swd_process._WorkerClient))
        marker = Path("unconfirmed-close-marker.json")
        client._guard = RLock()
        client._closed = False
        client._cleanup_confirmed = False
        client._marker = marker
        client._process = SimpleNamespace(stdin=None, stdout=None)
        client.call = Mock(return_value=None)
        with (
            patch.object(swd_process, "terminate_process_group", return_value=False),
            patch.object(swd_process.ProcessMarkerStore, "remove") as remove,
            self.assertRaisesRegex(TargetConnectionError, "marker retained"),
        ):
            client.close(deadline=time.monotonic() + 1)
        remove.assert_not_called()
        self.assertEqual(client._marker, marker)

    def test_close_terminates_when_default_deadline_is_already_exhausted(self) -> None:
        from pyocd_debug_mcp.adapters import swd_process

        client = cast(Any, object.__new__(swd_process._WorkerClient))
        client._guard = RLock()
        client._closed = False
        client._cleanup_confirmed = False
        client._marker = Path("expired-default-marker.json")
        client._process = SimpleNamespace(stdin=Mock(), stdout=Mock(), poll=Mock())
        with (
            patch.object(
                swd_process,
                "_operation_deadline",
                side_effect=swd_process.TargetConnectionError("no time remains"),
            ),
            patch.object(swd_process, "terminate_process_group", return_value=True) as terminate,
            patch.object(swd_process.ProcessMarkerStore, "remove") as remove,
        ):
            client.close()

        terminate.assert_called_once_with(client._process)
        remove.assert_called_once_with(Path("expired-default-marker.json"))

    def test_close_terminates_when_deadline_expires_between_check_and_call(self) -> None:
        from pyocd_debug_mcp.adapters import swd_process

        client = cast(Any, object.__new__(swd_process._WorkerClient))
        client._guard = RLock()
        client._closed = False
        client._cleanup_confirmed = False
        client._marker = Path("deadline-race-marker.json")
        client._process = SimpleNamespace(stdin=Mock(), stdout=Mock(), poll=Mock())
        client.call = Mock(side_effect=TimeoutError("deadline raced"))
        with (
            patch.object(swd_process, "terminate_process_group", return_value=True) as terminate,
            patch.object(swd_process.ProcessMarkerStore, "remove") as remove,
        ):
            client.close(deadline=time.monotonic() + 1)

        terminate.assert_called_once_with(client._process)
        remove.assert_called_once_with(Path("deadline-race-marker.json"))

    def test_terminate_helper_exception_closes_pipes_and_retains_marker(self) -> None:
        from pyocd_debug_mcp.adapters import swd_process
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        stdin = Mock()
        stdout = Mock()
        client = cast(Any, object.__new__(swd_process._WorkerClient))
        client._guard = RLock()
        client._closed = False
        client._cleanup_confirmed = False
        client._marker = Path("terminate-exception-marker.json")
        client._process = SimpleNamespace(stdin=stdin, stdout=stdout)
        client.call = Mock(return_value=None)
        with (
            patch.object(
                swd_process,
                "terminate_process_group",
                side_effect=RuntimeError("job cleanup failed"),
            ),
            patch.object(swd_process.ProcessMarkerStore, "remove") as remove,
            self.assertRaisesRegex(TargetConnectionError, "marker retained"),
        ):
            client.close(deadline=time.monotonic() + 1)

        stdin.close.assert_called_once_with()
        stdout.close.assert_called_once_with()
        remove.assert_not_called()

    def test_confirmed_terminate_uses_helper_as_sole_cleanup_authority(self) -> None:
        from pyocd_debug_mcp.adapters import swd_process

        process = SimpleNamespace(stdin=Mock(), stdout=Mock(), wait=Mock(), poll=Mock())
        client = cast(Any, object.__new__(swd_process._WorkerClient))
        client._process = process

        with patch.object(swd_process, "terminate_process_group", return_value=True) as terminate:
            self.assertTrue(client._terminate())

        terminate.assert_called_once_with(process)
        process.wait.assert_not_called()
        process.poll.assert_called_once_with()
        process.stdin.close.assert_called_once_with()
        process.stdout.close.assert_called_once_with()

    def test_parent_deadlines_use_explicit_or_managed_remaining_budget(self) -> None:
        from pyocd_debug_mcp.adapters import swd_process

        with (
            patch.object(swd_process.time, "monotonic", return_value=100.0),
            patch.object(swd_process, "current_operation", return_value=None),
        ):
            self.assertEqual(swd_process._operation_deadline(5.0), 105.0)
            self.assertEqual(
                swd_process._operation_deadline(),
                100.0 + swd_process.DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
            )

        managed = SimpleNamespace(started_at=90.0, timeout_seconds=20.0)
        with (
            patch.object(swd_process.time, "monotonic", return_value=100.0),
            patch.object(swd_process, "current_operation", return_value=managed),
        ):
            self.assertEqual(
                swd_process._operation_deadline(),
                110.0 - swd_process.CANCELLATION_CLEANUP_GRACE_SECONDS,
            )

        for invalid in (True, float("nan"), float("inf"), -1.0, 0.0):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                swd_process._operation_deadline(cast(Any, invalid))

    def test_generic_flash_and_recovery_calls_use_remaining_outer_deadline(self) -> None:
        from pyocd_debug_mcp.adapters import swd_process
        from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle

        client = cast(Any, object.__new__(swd_process._WorkerClient))
        handle = TargetSessionHandle(None, None, None, "route", None, worker=client)
        managed = SimpleNamespace(started_at=90.0, timeout_seconds=40.0)
        seen: list[tuple[str, float]] = []

        def call(
            _client: object,
            operation: str,
            _arguments: dict[str, object],
            *,
            deadline: float | None = None,
            timeout: float | None = None,
        ) -> object:
            self.assertIsNone(timeout)
            assert deadline is not None
            seen.append((operation, deadline))
            return {"get_state": "HALTED", "supports_recovery": True}.get(operation)

        interface = swd_process.ProcessIsolatedSWDInterface()
        with (
            patch.object(swd_process.time, "monotonic", return_value=100.0),
            patch.object(swd_process, "current_operation", return_value=managed),
            patch.object(swd_process._WorkerClient, "call", new=call),
        ):
            self.assertEqual(interface.get_state(handle), "HALTED")
            interface.flash(handle, Path("firmware.hex"), halt_after_reset=False)
            interface.recover(handle)
            self.assertTrue(interface.supports_recovery(handle, "backend_mass_erase"))

        expected = 130.0 - swd_process.CANCELLATION_CLEANUP_GRACE_SECONDS
        self.assertEqual(
            seen,
            [
                ("get_state", expected),
                ("flash", expected),
                ("recover", expected),
                ("supports_recovery", expected),
            ],
        )

    def test_stderr_is_inherited_not_captured_or_sanitized(self) -> None:
        from pyocd_debug_mcp.adapters import swd_process

        ready = BytesIO(b'{"version":1,"ready":true}\n')
        process = Mock(stdin=BytesIO(), stdout=ready)
        marker = Path("worker-marker.json")
        with (
            patch.object(swd_process, "popen_owned", return_value=(process, marker)) as popen,
            patch.object(swd_process, "terminate_process_group", return_value=True),
            patch.object(swd_process.ProcessMarkerStore, "remove"),
        ):
            client = swd_process._WorkerClient(deadline=time.monotonic() + 1)
            client.close(deadline=time.monotonic())

        self.assertIsNone(popen.call_args.kwargs["stderr"])
        self.assertFalse(hasattr(client, "_stderr"))

    def test_frozen_metadata_and_uidless_runtime_identity(self) -> None:
        from pyocd_debug_mcp.adapters.swd_interface import (
            TargetSessionHandle,
            TargetSessionMetadata,
        )
        from pyocd_debug_mcp.services.connections import (
            ConnectionManager,
            stable_connection_identity,
        )

        def handle(token: str) -> TargetSessionHandle:
            metadata = TargetSessionMetadata(
                board_name="board",
                probe_description="probe",
                probe_family="family",
                probe_uid=None,
                live_part_number=None,
                route_used="route",
                target_override=None,
                runtime_token=token,
            )
            return TargetSessionHandle(None, None, None, "route", None, metadata=metadata)

        first = handle("runtime-a")
        second = handle("runtime-b")
        self.assertEqual(stable_connection_identity(first), "session:runtime-a")
        self.assertNotEqual(
            stable_connection_identity(first),
            stable_connection_identity(second),
        )
        ConnectionManager().assign("board", first, cast(Any, Mock()))
        assert first.metadata is not None
        with self.assertRaises(FrozenInstanceError):
            first.metadata.runtime_token = "changed"  # type: ignore[misc]

    def test_child_metadata_uses_detector_and_preserves_absent_uid_and_part(self) -> None:
        from pyocd_debug_mcp.adapters.provider_worker_runtime import _metadata
        from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle

        session = SimpleNamespace(
            board=SimpleNamespace(name="board"),
            probe=SimpleNamespace(unique_id=None, description="probe"),
            target=SimpleNamespace(part_number=None),
        )
        handle = TargetSessionHandle(session, None, None, "route", None)
        with patch(
            "pyocd_debug_mcp.adapters.provider_worker_runtime.probe_family_from_pyocd_probe",
            return_value="jlink",
        ):
            metadata = _metadata(handle)
        self.assertEqual(metadata["probe_family"], "jlink")
        self.assertIsNone(metadata["probe_uid"])
        self.assertIsNone(metadata["live_part_number"])
        self.assertTrue(metadata["runtime_token"])

    def test_child_accepts_large_trusted_values_and_dispatches_complete_surface(self) -> None:
        from pyocd_debug_mcp.adapters.provider_worker_runtime import (
            _board,
            _dispatch,
            _validate_arguments,
        )
        from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
        from pyocd_debug_mcp.adapters.swd_process import _board_record
        from pyocd_debug_mcp.board_config import BoardConfig
        from pyocd_debug_mcp.timeouts import default_server_timeout_config

        long_text = "x" * 20_000
        board = BoardConfig(
            board_id="fake",
            display_name=long_text,
            mcu_family="fake",
            probe_family="fake",
            pyocd_target="fake",
            probe_type="fake",
            probe_hint_terms=tuple(f"probe-{index}" for index in range(1_000)),
            serial_hint_terms=tuple(f"serial-{index}" for index in range(1_000)),
            test_addr=0,
            source_path=Path(long_text),
        )
        record = json.loads(json.dumps(_board_record(board)))
        self.assertEqual(_board(record), board)
        _validate_arguments("read_memory_block", {"address": 0, "length": 70_000})
        _validate_arguments("flash", {"path": long_text, "halt_after_reset": False})

        reset = Mock(return_value=None)
        session = SimpleNamespace(
            board=SimpleNamespace(name="fake board"),
            probe=SimpleNamespace(unique_id="fake-probe", description="fake probe"),
            target=SimpleNamespace(part_number="fake-part"),
        )
        handle = TargetSessionHandle(session, board, "fake-probe", "fake-route", None)

        class FakeAdapter:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def __getattr__(self, name: str):
                results: dict[str, object] = {
                    "open": handle,
                    "connect_under_reset": handle,
                    "get_state": "HALTED",
                    "read_memory": 0x12,
                    "read_memory_block": [0, 1, 255],
                    "write_memory": None,
                    "read_core_register": 17,
                    "write_core_register": None,
                    "supported_core_registers": ("pc", "r0"),
                    "halt": None,
                    "resume": None,
                    "step": None,
                    "reset": None,
                    "reset_and_halt": None,
                    "release_reset": None,
                    "flash": None,
                    "recover": None,
                    "supports_recovery": True,
                    "set_breakpoint": None,
                    "remove_breakpoint": None,
                    "close": None,
                }
                if name not in results:
                    raise AttributeError(name)

                def call(*_args: object, **_kwargs: object) -> object:
                    self.calls.append(name)
                    if name == "release_reset":
                        reset(False)
                    return results[name]

                return call

        open_arguments = {
            "board": record,
            "unique_id": "fake-probe",
            "target": None,
            "server_timeouts": default_server_timeout_config().to_record(),
            "protocol": "swd",
            "connect_mode": "attach",
            "pack_path": None,
            "pack_sha256": None,
            "pdsc_device": None,
            "frequency_hz": 1_000_000,
        }
        with patch(
            "pyocd_debug_mcp.adapters.provider_worker_runtime.probe_family_from_pyocd_probe",
            return_value="fake",
        ):
            for operation in ("open", "connect_under_reset"):
                adapter = FakeAdapter()
                opened, metadata, exit_after = _dispatch(
                    cast(Any, adapter), None, operation, dict(open_arguments)
                )
                self.assertIs(opened, handle)
                self.assertEqual(metadata["live_part_number"], "fake-part")
                self.assertFalse(exit_after)

        cases = {
            "get_state": ({}, "HALTED"),
            "read_memory": ({"address": 0, "width_bits": 8}, 0x12),
            "read_memory_block": ({"address": 0, "length": 3}, [0, 1, 255]),
            "write_memory": ({"address": 0, "value": 0x12, "width_bits": 8}, None),
            "read_core_register": ({"name": "pc"}, 17),
            "write_core_register": ({"name": "pc", "value": 17}, None),
            "supported_core_registers": ({}, ["pc", "r0"]),
            "halt": ({}, None),
            "resume": ({}, None),
            "step": ({}, None),
            "reset": ({}, None),
            "reset_and_halt": ({}, None),
            "release_reset": ({}, None),
            "flash": ({"path": long_text, "halt_after_reset": False}, None),
            "recover": ({}, None),
            "supports_recovery": ({"mechanism": "fake"}, True),
            "set_breakpoint": ({"address": 4}, None),
            "remove_breakpoint": ({"address": 4}, None),
        }
        for operation, (arguments, expected) in cases.items():
            with self.subTest(operation=operation):
                _validate_arguments(operation, arguments)
                adapter = FakeAdapter()
                retained, result, exit_after = _dispatch(
                    cast(Any, adapter), handle, operation, arguments
                )
                self.assertIs(retained, handle)
                self.assertEqual(result, expected)
                self.assertFalse(exit_after)
        reset.assert_called_once_with(False)


@unittest.skipUnless(os.name == "posix", "real POSIX worker terminal groups")
class PosixWorkerGroupCleanupTests(_NativePosixCase):
    def worker(self, mode: str):
        from pyocd_debug_mcp.adapters import swd_process

        def spawn(*args: Any, **kwargs: Any):
            kwargs["marker_store"] = self.store
            return self.capture_spawn(*args, **kwargs)

        with patch.object(swd_process, "popen_owned", spawn):
            return swd_process._WorkerClient(
                worker_argv=(
                    sys.executable,
                    str(Path(__file__).with_name("fake_provider_worker.py")),
                    mode,
                ),
                deadline=time.monotonic() + 10,
            )

    def test_actual_terminal_typed_and_locked_workers_close_without_signal_and_reap(self) -> None:
        from pyocd_debug_mcp.kernel import processes
        from pyocd_debug_mcp.kernel._analysis_posix import _exit_status
        from pyocd_debug_mcp.target_errors import LockedTargetError, TargetControlError

        for mode, error_type, message in (
            ("typed_error", TargetControlError, "fake target error"),
            ("locked_error", LockedTargetError, "fake locked target"),
        ):
            with self.subTest(mode=mode):
                client = self.worker(mode)
                process, marker = self.created[-1]
                with self.assertRaisesRegex(error_type, message):
                    client.call("get_state", {}, timeout=2)
                self.assertEqual(client._request_id, 1)
                self.assertEqual(process.wait(timeout=10), 0)
                self.assertEqual(_exit_status(process.pid), 0)
                assert marker is not None
                self.assertTrue(marker.is_file())
                with patch.object(processes.os, "killpg", wraps=os.killpg) as signal_group:
                    client.close()
                    client.close()
                signal_group.assert_not_called()
                self.assertTrue(client._cleanup_confirmed)
                self.assertIsNone(client._marker)
                self.assertFalse(marker.exists())
                self.assert_reaped(process, 0)
                self.assert_peer()

    def test_actual_persistent_worker_preserves_errors_identity_and_idempotent_close(self) -> None:
        import psutil
        from pyocd_debug_mcp.target_errors import TargetControlError

        client = self.worker("persistent_typed_error")
        process, marker = self.created[-1]
        birth = psutil.Process(process.pid).create_time()
        for request_id in (1, 2):
            with self.assertRaisesRegex(TargetControlError, "fake target error"):
                client.call("get_state", {}, timeout=2)
            self.assertEqual(client._request_id, request_id)
            self.assertEqual(self.native_state(process.pid, birth), "live")
            self.assertFalse(client._closed)
            assert marker is not None
            self.assertTrue(marker.is_file())
            self.assert_peer()
        client.close()
        client.close()
        self.assertTrue(client._cleanup_confirmed)
        self.assertIsNone(client._marker)
        self.assertFalse(marker.exists())
        self.assert_reaped(process)
        self.assert_peer()

    def test_actual_live_worker_with_faulted_membership_retains_marker(self) -> None:
        from pyocd_debug_mcp.kernel import processes
        from pyocd_debug_mcp.target_errors import TargetConnectionError

        client = self.worker("good")
        process, marker = self.created[-1]
        self.assertEqual(client.call("get_state", {}, timeout=2), "RUNNING")
        self.assertIsNone(process.returncode)
        with (
            patch.object(
                processes,
                "_current_group_members",
                side_effect=PermissionError("native group denied"),
            ),
            patch.object(processes.os, "killpg", wraps=os.killpg) as signal_group,
        ):
            with self.assertRaisesRegex(TargetConnectionError, "recovery marker retained"):
                client.close(deadline=time.monotonic())
        signal_group.assert_not_called()
        self.assertFalse(client._cleanup_confirmed)
        self.assertEqual(client._marker, marker)
        assert marker is not None
        self.assertTrue(marker.is_file())
        self.assert_peer()


if __name__ == "__main__":
    unittest.main()
