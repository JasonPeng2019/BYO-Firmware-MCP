from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from io import BytesIO
from pathlib import Path
from queue import Queue
from threading import Event, RLock, Thread
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

import anyio
from anyio.to_thread import run_sync as run_sync_in_thread


class ProcessIsolationContractTests(unittest.TestCase):
    def test_worker_bootstrap_isolates_import_and_between_request_stdout(self) -> None:
        script = r"""
import builtins
import json
import os
import sys
import types

from firmware_mcp.adapters import provider_worker

original_import = builtins.__import__

def injected_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "firmware_mcp.adapters.provider_worker_runtime":
        print("import-python", flush=True)
        os.write(1, b"import-fd\n")
        runtime = types.ModuleType(name)

        def run(protocol):
            protocol.write(b'{"version":2,"ready":true}\n')
            protocol.flush()
            first = json.loads(sys.stdin.buffer.readline())
            protocol.write(
                (json.dumps({"version": 2, "request_id": first["request_id"],
                             "ok": True, "result": "first"}, separators=(",", ":")) + "\n").encode()
            )
            protocol.flush()
            print("idle-python", flush=True)
            os.write(1, b"idle-fd\n")
            second = json.loads(sys.stdin.buffer.readline())
            protocol.write(
                (json.dumps({"version": 2, "request_id": second["request_id"],
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
            '{"version":2,"request_id":1,"operation":"one","arguments":{}}\n'
            '{"version":2,"request_id":2,"operation":"two","arguments":{}}\n'
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            input=requests,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [json.loads(line) for line in result.stdout.splitlines()],
            [
                {"version": 2, "ready": True},
                {"version": 2, "request_id": 1, "ok": True, "result": "first"},
                {"version": 2, "request_id": 2, "ok": True, "result": "second"},
            ],
        )
        for marker in ("import-python", "import-fd", "idle-python", "idle-fd"):
            self.assertIn(marker, result.stderr)

    def test_importing_worker_bootstrap_does_not_redirect_test_process(self) -> None:
        before = sys.stdout
        from firmware_mcp.adapters import provider_worker

        self.assertIsNotNone(provider_worker.main)
        self.assertIs(sys.stdout, before)

    def test_backend_stdout_is_forwarded_to_inherited_stderr_at_both_layers(self) -> None:
        script = r"""
import os
import sys
from firmware_mcp.adapters.swd_pyocd import _backend_stdout_to_stderr

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
            timeout=10,
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

from firmware_mcp.adapters import swd_pyocd
from firmware_mcp.board_config import BoardConfig
from firmware_mcp.target_errors import TargetConnectionError

def noisy_selection(**_kwargs):
    print("selection-python", flush=True)
    os.write(1, b"selection-fd\n")
    raise TargetConnectionError("typed selection failure")

def noisy_discovery(**_kwargs):
    print("discovery-python", flush=True)
    os.write(1, b"discovery-fd\n")
    return []

payload = {}
with (
    patch.object(swd_pyocd.ConnectHelper, "session_with_chosen_probe", noisy_selection),
):
    try:
        swd_pyocd.PyOCDSWDInterface._choose_session(probe_uid="probe", options=None)
    except TargetConnectionError as exc:
        payload["error"] = str(exc)
        payload["kind"] = type(exc).__name__
print(json.dumps(payload, separators=(",", ":")))
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["kind"], "TargetConnectionError")
        self.assertEqual(payload["error"], "typed selection failure")
        for marker in (
            "selection-python",
            "selection-fd",
        ):
            self.assertIn(marker, result.stderr)

    @staticmethod
    def _client(mode: str):
        from firmware_mcp.adapters.debug_process import _WorkerClient

        worker = Path(__file__).with_name("fake_provider_worker.py")
        return _WorkerClient(worker_argv=(sys.executable, str(worker), mode))

    def test_process_backend_exposes_complete_swd_surface(self) -> None:
        from firmware_mcp.adapters.debug_process import ProcessIsolatedDebugInterface

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
            "recovery_capabilities",
            "set_breakpoint",
            "remove_breakpoint",
        }
        self.assertTrue(required.issubset(set(dir(ProcessIsolatedDebugInterface))))

    def test_parent_production_modules_do_not_dereference_native_handle_session(self) -> None:
        root = Path(__file__).parents[1] / "src" / "firmware_mcp"
        offenders = []
        for path in root.rglob("*.py"):
            if path.name in {"swd_pyocd.py", "provider_worker_runtime.py"}:
                continue
            if "handle.session" in path.read_text(encoding="utf-8"):
                offenders.append(path.relative_to(root).as_posix())
        self.assertEqual(offenders, [])

    def test_result_contracts_preserve_large_valid_values_and_exact_block_length(self) -> None:
        from firmware_mcp.adapters.debug_process import _validate_result

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
            "live_identity": {
                "capability": "compatible",
                "part_number": None,
                "provenance": "provider live metadata",
                "support_identity": "provider:test",
                "evidence": {"large": long_text},
            },
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

    def test_reader_accepts_large_complete_frames(self) -> None:
        from firmware_mcp.adapters.debug_process import _WorkerClient

        responses: Queue[object] = Queue()
        payload = {"value": "x" * 1_500_000}
        _WorkerClient._start_reader(
            BytesIO((json.dumps(payload) + "\n").encode("utf-8")),
            responses,
        )
        self.assertEqual(responses.get(timeout=2), payload)

    def test_wrong_id_malformed_frame_and_crash_are_terminal(self) -> None:
        from firmware_mcp.target_errors import TargetConnectionError

        for mode in ("crash", "wrong_id", "malformed_reply"):
            with self.subTest(mode=mode):
                client = self._client(mode)
                with self.assertRaises(TargetConnectionError):
                    client.call("get_state", {})
                with self.assertRaises(TargetConnectionError):
                    client.call("get_state", {})

    def test_typed_child_errors_are_preserved_without_retry(self) -> None:
        from firmware_mcp.target_errors import LockedTargetError, TargetControlError

        typed = self._client("typed_error")
        with self.assertRaisesRegex(TargetControlError, "fake target error"):
            typed.call("get_state", {})
        typed.close()

        locked = self._client("locked_error")
        with self.assertRaisesRegex(LockedTargetError, "fake locked target"):
            locked.call("get_state", {})
        locked.close()

    def test_worker_cleanup_diagnostics_are_typed_and_malformed_records_are_terminal(self) -> None:
        from firmware_mcp.target_errors import TargetConnectionCleanupError, TargetConnectionError

        client = self._client("cleanup_error")
        with self.assertRaises(TargetConnectionCleanupError) as raised:
            client.call("get_state", {})
        self.assertEqual(raised.exception.primary_error_type, "RuntimeError")
        self.assertEqual(raised.exception.primary_error_message, "open failed")
        self.assertEqual(
            [item.stage for item in raised.exception.cleanup_diagnostics],
            ["reset_release", "session_close"],
        )
        self.assertIn("revalidate", str(raised.exception))
        client.close()

        malformed = self._client("malformed_cleanup_error")
        with self.assertRaisesRegex(TargetConnectionError, "malformed cleanup diagnostics"):
            malformed.call("get_state", {})
        with self.assertRaises(TargetConnectionError):
            malformed.call("get_state", {})

    def test_public_interface_preserves_worker_cleanup_diagnostics(self) -> None:
        from firmware_mcp.adapters.debug_process import ProcessIsolatedDebugInterface
        from firmware_mcp.target_errors import TargetConnectionCleanupError

        interface = ProcessIsolatedDebugInterface()
        with self.assertRaises(TargetConnectionCleanupError) as raised:
            interface.open(
                board=None,
                unique_id=None,
                target=None,
                worker_argv=(
                    sys.executable,
                    str(Path(__file__).with_name("fake_provider_worker.py")),
                    "cleanup_error",
                ),
            )

        error = raised.exception
        self.assertEqual(error.primary_error_type, "RuntimeError")
        self.assertEqual(
            [item.stage for item in error.cleanup_diagnostics],
            ["reset_release", "session_close"],
        )

    def test_confirmed_process_termination_makes_close_successful_and_idempotent(self) -> None:
        typed = self._client("typed_error")
        typed.close()
        typed.close()
        self.assertIsNotNone(typed._process.returncode)

        hanging = self._client("close_hang")
        hanging._terminate()
        hanging.close()
        self.assertIsNotNone(hanging._process.returncode)

    def test_actual_worker_dispatches_short_request_while_stdin_remains_open(self) -> None:
        from firmware_mcp.adapters.debug_process import _WorkerClient
        from firmware_mcp.target_errors import TargetConnectionError

        client = _WorkerClient()
        try:
            with self.assertRaisesRegex(TargetConnectionError, "no live target session"):
                client.call("get_state", {})
        finally:
            client.close()

    def test_faulted_worker_does_not_interrupt_a_healthy_worker(self) -> None:
        healthy = self._client("good")
        faulted = self._client("open_hang")
        try:
            self.assertEqual(healthy.call("get_state", {}), "RUNNING")
            faulted._terminate()
            self.assertIsNotNone(faulted._process.returncode)
            self.assertEqual(healthy.call("get_state", {}), "RUNNING")
        finally:
            healthy.close()
        self.assertIsNotNone(healthy._process.returncode)

    def test_cancelled_ready_handshake_terminates_only_its_connect_worker(self) -> None:
        """A never-ready connect worker remains request-owned until promotion."""

        from firmware_mcp.adapters import debug_process
        from firmware_mcp.kernel.operations import (
            OperationCancelledError,
            OperationManager,
            dispatch,
        )
        from firmware_mcp.kernel.processes import ProcessMarkerStore

        async def scenario() -> None:
            manager = OperationManager()
            healthy = self._client("good")
            started = Event()
            captured: dict[str, Path] = {}
            real_popen_owned = debug_process.popen_owned
            with tempfile.TemporaryDirectory() as temporary:
                store = ProcessMarkerStore(Path(temporary))

                def spawn_owned(*args: Any, **kwargs: Any) -> tuple[subprocess.Popen[Any], Path]:
                    process, marker = real_popen_owned(*args, marker_store=store, **kwargs)
                    assert marker is not None
                    captured["marker"] = marker
                    started.set()
                    return process, marker

                async def connect_hanging_worker() -> None:
                    with self.assertRaises(OperationCancelledError):
                        await dispatch(
                            "connect_board",
                            "board-a",
                            lambda: self._client("ready_hang"),
                            request_id="cancel-ready-frame",
                            manager=manager,
                        )

                with patch.object(debug_process, "popen_owned", side_effect=spawn_owned):
                    async with anyio.create_task_group() as tasks:
                        tasks.start_soon(connect_hanging_worker)
                        await run_sync_in_thread(started.wait)
                        self.assertEqual(manager.cancel_request("cancel-ready-frame"), 1)

                self.assertIn("marker", captured)
                self.assertFalse(captured["marker"].exists())
                self.assertEqual(healthy.call("get_state", {}), "RUNNING")
            healthy.close()

        anyio.run(scenario)

    def test_cancelled_bootstrap_and_rollback_share_one_close_transaction(self) -> None:
        """A rollback joins bootstrap cleanup instead of terminating twice."""

        from firmware_mcp.adapters import debug_process

        marker = Path("bootstrap-close-transaction-marker.json")
        termination_started = Event()
        release_termination = Event()
        client = cast(Any, object.__new__(debug_process._WorkerClient))
        client._guard = RLock()
        client._closed = False
        client._cleanup_confirmed = False
        client._cleanup_diagnostics = []
        client._marker = marker
        client._process = SimpleNamespace(stdin=None, stdout=None)
        owner = SimpleNamespace(
            resources=SimpleNamespace(cleanup_errors=[], fatal_cleanup_errors=[])
        )
        client._bootstrap_owner = owner
        client._bootstrap_callback = None
        client._bootstrap_detached = False
        client._bootstrap_cleanup_started = False
        client._bootstrap_cleanup_evidence = None

        def blocked_terminate() -> bool:
            termination_started.set()
            release_termination.wait()
            return True

        client._terminate = Mock(side_effect=blocked_terminate)
        other = self._client("good")
        cancellation: dict[str, object] = {}
        rollback: dict[str, object] = {}
        first = Thread(
            target=lambda: cancellation.__setitem__(
                "evidence", client._finish_cancelled_bootstrap()
            )
        )
        second = Thread(target=lambda: rollback.__setitem__("evidence", client.close()))
        try:
            with patch.object(debug_process.ProcessMarkerStore, "remove") as remove:
                first.start()
                self.assertTrue(termination_started.wait(1))
                second.start()
                # The rollback is blocked on the first closer's transaction,
                # rather than observing a half-closed worker and starting
                # another cleanup.
                self.assertTrue(second.is_alive())
                release_termination.set()
                first.join(1)
                second.join(1)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            client._terminate.assert_called_once_with()
            remove.assert_called_once_with(marker)
            self.assertEqual(cancellation["evidence"], rollback["evidence"])
            self.assertEqual(
                cancellation["evidence"],
                {
                    "closed": True,
                    "graceful": False,
                    "marker_retained": False,
                    "diagnostic": None,
                },
            )
            self.assertEqual(other.call("get_state", {}), "RUNNING")
        finally:
            release_termination.set()
            first.join(1)
            second.join(1)
            other.close()

    def test_rollback_first_and_bootstrap_cancellation_share_one_transaction(self) -> None:
        """Rollback-first cleanup has the same cached bootstrap evidence."""

        from firmware_mcp.adapters import debug_process

        marker = Path("bootstrap-reverse-close-transaction-marker.json")
        termination_started = Event()
        release_termination = Event()
        client = cast(Any, object.__new__(debug_process._WorkerClient))
        client._guard = RLock()
        client._closed = False
        client._cleanup_confirmed = False
        client._cleanup_diagnostics = []
        client._marker = marker
        client._process = SimpleNamespace(stdin=None, stdout=None)
        client._bootstrap_owner = SimpleNamespace(
            resources=SimpleNamespace(cleanup_errors=[], fatal_cleanup_errors=[])
        )
        client._bootstrap_callback = None
        client._bootstrap_detached = False
        client._bootstrap_cleanup_started = False
        client._bootstrap_cleanup_evidence = None

        def blocked_terminate() -> bool:
            termination_started.set()
            release_termination.wait()
            return True

        client._terminate = Mock(side_effect=blocked_terminate)
        other = self._client("good")
        rollback: dict[str, object] = {}
        cancellation: dict[str, object] = {}
        first = Thread(target=lambda: rollback.__setitem__("evidence", client.close()))
        second = Thread(
            target=lambda: cancellation.__setitem__("evidence", client._cancel_bootstrap())
        )
        try:
            with patch.object(debug_process.ProcessMarkerStore, "remove") as remove:
                first.start()
                self.assertTrue(termination_started.wait(1))
                second.start()
                self.assertTrue(second.is_alive())
                release_termination.set()
                first.join(1)
                second.join(1)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            client._terminate.assert_called_once_with()
            remove.assert_called_once_with(marker)
            self.assertEqual(rollback["evidence"], cancellation["evidence"])
            evidence = cast(dict[str, object], rollback["evidence"])
            self.assertEqual(evidence["closed"], True)
            self.assertFalse(evidence["marker_retained"])
            self.assertEqual(other.call("get_state", {}), "RUNNING")
        finally:
            release_termination.set()
            first.join(1)
            second.join(1)
            other.close()

    def test_rollback_first_marker_failure_reaches_bootstrap_owner(self) -> None:
        """A late cancellation still receives the first closer's marker error."""

        from firmware_mcp.adapters import debug_process

        marker = Path("bootstrap-reverse-marker-failure.json")
        owner = SimpleNamespace(
            resources=SimpleNamespace(cleanup_errors=[], fatal_cleanup_errors=[])
        )
        client = cast(Any, object.__new__(debug_process._WorkerClient))
        client._guard = RLock()
        client._closed = False
        client._cleanup_confirmed = False
        client._cleanup_diagnostics = []
        client._marker = marker
        client._process = SimpleNamespace(stdin=None, stdout=None)
        client._bootstrap_owner = owner
        client._bootstrap_callback = None
        client._bootstrap_detached = False
        client._bootstrap_cleanup_started = False
        client._bootstrap_cleanup_evidence = None
        client._terminate = Mock(return_value=True)
        other = self._client("good")
        try:
            with patch.object(
                debug_process.ProcessMarkerStore,
                "remove",
                side_effect=OSError("reverse marker removal denied"),
            ) as remove:
                rollback = client.close()
                cancellation = client._cancel_bootstrap()

            client._terminate.assert_called_once_with()
            remove.assert_called_once_with(marker)
            self.assertEqual(rollback, cancellation)
            self.assertTrue(rollback["closed"])
            self.assertTrue(rollback["marker_retained"])
            self.assertIn("reverse marker removal denied", rollback["diagnostic"])
            self.assertEqual(owner.resources.cleanup_errors, owner.resources.fatal_cleanup_errors)
            self.assertEqual(len(owner.resources.fatal_cleanup_errors), 1)
            self.assertIn("reverse marker removal denied", owner.resources.fatal_cleanup_errors[0])
            self.assertEqual(other.call("get_state", {}), "RUNNING")
        finally:
            other.close()

    def test_cancelled_ready_handshake_surfaces_retained_marker_cleanup_uncertainty(self) -> None:
        """A proven worker close still reports a retained-marker cleanup failure."""

        from firmware_mcp.adapters import debug_process
        from firmware_mcp.kernel.operations import (
            OperationCleanupError,
            OperationManager,
            dispatch,
        )
        from firmware_mcp.kernel.processes import ProcessMarkerStore

        async def scenario() -> None:
            manager = OperationManager()
            healthy = self._client("good")
            started = Event()
            captured: dict[str, object] = {}
            real_popen_owned = debug_process.popen_owned
            real_terminate = debug_process.terminate_process_group
            with tempfile.TemporaryDirectory() as temporary:
                store = ProcessMarkerStore(Path(temporary))

                def spawn_owned(*args: Any, **kwargs: Any) -> tuple[subprocess.Popen[Any], Path]:
                    process, marker = real_popen_owned(*args, marker_store=store, **kwargs)
                    assert marker is not None
                    captured["process"] = process
                    captured["marker"] = marker
                    started.set()
                    return process, marker

                def terminate_owned(process: subprocess.Popen[Any]) -> bool:
                    confirmed = real_terminate(process)
                    captured["termination_confirmed"] = confirmed
                    return confirmed

                async def connect_hanging_worker() -> None:
                    with self.assertRaises(OperationCleanupError) as raised:
                        await dispatch(
                            "connect_board",
                            "board-a",
                            lambda: self._client("ready_hang"),
                            request_id="cancel-ready-marker-removal",
                            manager=manager,
                        )
                    self.assertIn("OperationCancelledError", str(raised.exception))
                    self.assertIn(
                        "Cancelled worker bootstrap was terminated. "
                        "Recovery marker removal failed: OSError: marker removal denied. "
                        "The marker remains retained.",
                        str(raised.exception),
                    )

                with (
                    patch.object(debug_process, "popen_owned", side_effect=spawn_owned),
                    patch.object(
                        debug_process,
                        "terminate_process_group",
                        side_effect=terminate_owned,
                    ),
                    patch.object(
                        debug_process.ProcessMarkerStore,
                        "remove",
                        side_effect=OSError("marker removal denied"),
                    ) as remove,
                ):
                    async with anyio.create_task_group() as tasks:
                        tasks.start_soon(connect_hanging_worker)
                        await run_sync_in_thread(started.wait)
                        self.assertEqual(manager.cancel_request("cancel-ready-marker-removal"), 1)

                    self.assertEqual(remove.call_count, 1)

                self.assertTrue(captured["termination_confirmed"])
                self.assertIsNotNone(cast(subprocess.Popen[Any], captured["process"]).poll())
                self.assertTrue(cast(Path, captured["marker"]).exists())
                self.assertEqual(healthy.call("get_state", {}), "RUNNING")
            healthy.close()

        anyio.run(scenario)

    def test_parent_write_waits_for_its_owned_writer(self) -> None:
        from firmware_mcp.adapters.debug_process import _WorkerClient

        released = Event()

        class BlockingPipe:
            def write(self, _frame: bytes) -> int:
                released.wait(1)
                return 0

            def flush(self) -> None:
                return None

        client = cast(Any, object.__new__(_WorkerClient))
        client._process = SimpleNamespace(stdin=BlockingPipe())
        writer: Thread | None = None
        try:
            writer = Thread(target=client._write, args=(b"request\n",))
            writer.start()
            self.assertTrue(writer.is_alive())
        finally:
            released.set()
            if writer is not None:
                writer.join(1)

    def test_unconfirmed_termination_retains_recovery_marker(self) -> None:
        from firmware_mcp.adapters import debug_process
        from firmware_mcp.target_errors import TargetConnectionError

        client = cast(Any, object.__new__(debug_process._WorkerClient))
        marker = Path("unconfirmed-worker-marker.json")
        client._closed = False
        client._cleanup_confirmed = False
        client._marker = marker
        client._process = SimpleNamespace(stdin=None, stdout=None)
        with (
            patch.object(debug_process, "terminate_process_group", return_value=False),
            patch.object(debug_process.ProcessMarkerStore, "remove") as remove,
            self.assertRaisesRegex(TargetConnectionError, "marker retained"),
        ):
            client._invalidate("forced cleanup failure")
        remove.assert_not_called()
        self.assertEqual(client._marker, marker)

    def test_marker_unlink_failure_is_typed_retained_and_retried_by_close(self) -> None:
        from firmware_mcp.adapters import debug_process
        from firmware_mcp.target_errors import TargetConnectionError

        client = cast(Any, object.__new__(debug_process._WorkerClient))
        marker = Path("unlink-failed-worker-marker.json")
        client._guard = RLock()
        client._closed = False
        client._cleanup_confirmed = False
        client._marker = marker
        client._process = SimpleNamespace(stdin=None, stdout=None)
        client._terminate = Mock(return_value=True)

        with patch.object(
            debug_process.ProcessMarkerStore,
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
        from firmware_mcp.adapters import debug_process
        from firmware_mcp.target_errors import TargetConnectionError

        primary = TargetConnectionError("primary worker open protocol failure")
        client = cast(Any, object.__new__(debug_process._WorkerClient))
        client._guard = RLock()
        client._closed = False
        client._cleanup_confirmed = False
        client._marker = Path("open-rollback-marker.json")
        client._process = SimpleNamespace(stdin=None, stdout=None)
        client.call = Mock(side_effect=primary)
        client._terminate = Mock(return_value=True)
        backend = debug_process.ProcessIsolatedDebugInterface()

        with (
            patch.object(debug_process, "_WorkerClient", return_value=client),
            patch.object(
                debug_process.ProcessMarkerStore,
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
        from firmware_mcp.adapters import debug_process
        from firmware_mcp.target_errors import TargetConnectionError

        client = cast(Any, object.__new__(debug_process._WorkerClient))
        marker = Path("unconfirmed-close-marker.json")
        client._guard = RLock()
        client._closed = False
        client._cleanup_confirmed = False
        client._marker = marker
        client._process = SimpleNamespace(stdin=None, stdout=None)
        client.call = Mock(return_value=None)
        with (
            patch.object(debug_process, "terminate_process_group", return_value=False),
            patch.object(debug_process.ProcessMarkerStore, "remove") as remove,
            self.assertRaisesRegex(TargetConnectionError, "marker retained"),
        ):
            client.close()
        remove.assert_not_called()
        self.assertEqual(client._marker, marker)

    def test_close_terminates_when_graceful_close_fails(self) -> None:
        from firmware_mcp.adapters import debug_process

        client = cast(Any, object.__new__(debug_process._WorkerClient))
        client._guard = RLock()
        client._closed = False
        client._cleanup_confirmed = False
        client._marker = Path("close-failure-marker.json")
        client._process = SimpleNamespace(stdin=Mock(), stdout=Mock(), poll=Mock())
        client.call = Mock(side_effect=RuntimeError("close transport failed"))
        with (
            patch.object(debug_process, "terminate_process_group", return_value=True) as terminate,
            patch.object(debug_process.ProcessMarkerStore, "remove") as remove,
        ):
            client.close()

        terminate.assert_called_once_with(client._process)
        remove.assert_called_once_with(Path("close-failure-marker.json"))

    def test_terminate_helper_exception_closes_pipes_and_retains_marker(self) -> None:
        from firmware_mcp.adapters import debug_process
        from firmware_mcp.target_errors import TargetConnectionError

        stdin = Mock()
        stdout = Mock()
        client = cast(Any, object.__new__(debug_process._WorkerClient))
        client._guard = RLock()
        client._closed = False
        client._cleanup_confirmed = False
        client._marker = Path("terminate-exception-marker.json")
        client._process = SimpleNamespace(stdin=stdin, stdout=stdout)
        client.call = Mock(return_value=None)
        with (
            patch.object(
                debug_process,
                "terminate_process_group",
                side_effect=RuntimeError("job cleanup failed"),
            ),
            patch.object(debug_process.ProcessMarkerStore, "remove") as remove,
            self.assertRaisesRegex(TargetConnectionError, "marker retained"),
        ):
            client.close()

        stdin.close.assert_called_once_with()
        stdout.close.assert_called_once_with()
        remove.assert_not_called()

    def test_confirmed_terminate_uses_helper_as_sole_cleanup_authority(self) -> None:
        from firmware_mcp.adapters import debug_process

        process = SimpleNamespace(stdin=Mock(), stdout=Mock(), wait=Mock(), poll=Mock())
        client = cast(Any, object.__new__(debug_process._WorkerClient))
        client._process = process

        with patch.object(debug_process, "terminate_process_group", return_value=True) as terminate:
            self.assertTrue(client._terminate())

        terminate.assert_called_once_with(process)
        process.wait.assert_not_called()
        process.poll.assert_called_once_with()
        process.stdin.close.assert_called_once_with()
        process.stdout.close.assert_called_once_with()

    def test_generic_flash_and_recovery_calls_carry_no_parent_deadline(self) -> None:
        from firmware_mcp.adapters import debug_process
        from firmware_mcp.adapters.debug_interface import (
            FlashVerification,
            RecoveryResult,
            TargetSessionHandle,
        )

        client = cast(Any, object.__new__(debug_process._WorkerClient))
        handle = TargetSessionHandle(None, None, None, "route", None, worker=client)
        seen: list[str] = []

        def call(
            _client: object,
            operation: str,
            _arguments: dict[str, object],
        ) -> object:
            seen.append(operation)
            return {
                "get_state": "HALTED",
                "recovery_capabilities": (),
                "recover": RecoveryResult("backend_mass_erase", True, "unavailable", "unknown"),
                "flash": FlashVerification(
                    "firmware.hex", 1, ((0, 1),), "0" * 64, "0" * 64, "RUNNING"
                ),
            }.get(operation)

        interface = debug_process.ProcessIsolatedDebugInterface()
        with (
            patch.object(debug_process._WorkerClient, "call", new=call),
        ):
            self.assertEqual(interface.get_state(handle), "HALTED")
            interface.flash(handle, Path("firmware.hex"), halt_after_reset=False)
            recovered = interface.recover(handle, "backend_mass_erase")
            self.assertTrue(recovered.accepted)
            self.assertEqual(recovered.observed_session_postcondition, "unknown")
            self.assertEqual(interface.recovery_capabilities(handle), ())

        self.assertEqual(
            seen,
            [
                "get_state",
                "flash",
                "recover",
                "recovery_capabilities",
            ],
        )

    def test_final_reset_worker_results_distinguish_observed_failure_from_uncertainty(self) -> None:
        from firmware_mcp.adapters import debug_process
        from firmware_mcp.adapters.debug_interface import FlashVerification, TargetSessionHandle
        from firmware_mcp.target_errors import (
            FlashFinalResetFailed,
            FlashFinalResetUncertain,
            TargetConnectionError,
        )

        client = cast(Any, object.__new__(debug_process._WorkerClient))
        handle = TargetSessionHandle(None, None, None, "route", None, worker=client)
        for postcondition, error_type, failure_type, failure_message in (
            (
                "failed",
                FlashFinalResetFailed,
                "ObservedResetState",
                "halt_after_reset=true; observed_state=RUNNING; expected_state=HALTED",
            ),
            ("unknown", FlashFinalResetUncertain, "DistinctResetDrop", "reset link disappeared"),
        ):
            with self.subTest(postcondition=postcondition):
                verified = FlashVerification(
                    "firmware.hex",
                    1,
                    ((0, 1),),
                    "0" * 64,
                    "0" * 64,
                    postcondition,
                    failure_type,
                    failure_message,
                )

                def call(*_args: object, **_kwargs: object) -> FlashVerification:
                    return verified

                interface = debug_process.ProcessIsolatedDebugInterface()
                with patch.object(debug_process._WorkerClient, "call", new=call):
                    with self.assertRaises(error_type) as raised:
                        interface.flash(handle, Path("firmware.hex"), halt_after_reset=True)

                self.assertEqual(
                    isinstance(raised.exception, TargetConnectionError), postcondition == "unknown"
                )
                self.assertIs(raised.exception.evidence, verified)
                self.assertIn(failure_type, str(raised.exception))
                self.assertIn(failure_message, str(raised.exception))

    def test_stderr_is_inherited_not_captured_or_sanitized(self) -> None:
        from firmware_mcp.adapters import debug_process

        ready = BytesIO(b'{"version":4,"ready":true}\n')
        process = Mock(stdin=BytesIO(), stdout=ready)
        marker = Path("worker-marker.json")
        with (
            patch.object(debug_process, "popen_owned", return_value=(process, marker)) as popen,
            patch.object(debug_process, "terminate_process_group", return_value=True),
            patch.object(debug_process.ProcessMarkerStore, "remove"),
        ):
            client = debug_process._WorkerClient()
            client.close()

        self.assertIsNone(popen.call_args.kwargs["stderr"])
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.PIPE)
        self.assertFalse(hasattr(client, "_stderr"))

    def test_frozen_metadata_and_uidless_runtime_identity(self) -> None:
        from firmware_mcp.adapters.debug_interface import (
            TargetSessionHandle,
            TargetSessionMetadata,
        )
        from firmware_mcp.services.connections import (
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
        from firmware_mcp.adapters.provider_worker_runtime import _metadata
        from firmware_mcp.adapters.debug_interface import TargetSessionHandle

        session = SimpleNamespace(
            board=SimpleNamespace(name="board"),
            probe=SimpleNamespace(unique_id=None, description="probe"),
            target=SimpleNamespace(part_number=None),
        )
        handle = TargetSessionHandle(session, None, None, "route", None)
        with patch(
            "firmware_mcp.adapters.provider_worker_runtime.probe_family_from_pyocd_probe",
            return_value="jlink",
        ):
            metadata = _metadata(handle)
        self.assertEqual(metadata["probe_family"], "jlink")
        self.assertIsNone(metadata["probe_uid"])
        self.assertIsNone(metadata["live_part_number"])
        self.assertTrue(metadata["runtime_token"])

    def test_child_accepts_large_trusted_values_and_dispatches_complete_surface(self) -> None:
        from firmware_mcp.adapters.provider_worker_runtime import (
            _board,
            _dispatch,
            _validate_arguments,
        )
        from firmware_mcp.adapters.debug_interface import FlashVerification, TargetSessionHandle
        from firmware_mcp.adapters.debug_process import _board_record
        from firmware_mcp.board_config import BoardConfig

        long_text = "x" * 20_000
        board = BoardConfig(
            board_id="fake",
            display_name=long_text,
            mcu_family="fake",
            probe_family="fake",
            target="fake",
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
                    "flash": FlashVerification(
                        long_text,
                        1,
                        ((0, 1),),
                        "0" * 64,
                        "0" * 64,
                        "RUNNING",
                    ),
                    "recover": type(
                        "Recovery",
                        (),
                        {
                            "to_record": lambda self: {
                                "mechanism": "fake",
                                "accepted": True,
                                "verification": "unavailable",
                                "observed_session_postcondition": "unknown",
                            }
                        },
                    )(),
                    "recovery_capabilities": (),
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
            "protocol": "swd",
            "connect_mode": "attach",
            "pack_path": None,
            "pack_sha256": None,
            "pdsc_device": None,
            "frequency_hz": 1_000_000,
        }
        with patch(
            "firmware_mcp.adapters.provider_worker_runtime.probe_family_from_pyocd_probe",
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
            "flash": (
                {"path": long_text, "halt_after_reset": False},
                {
                    "firmware_path": long_text,
                    "byte_count": 1,
                    "verified_ranges": [[0, 1]],
                    "expected_sha256": "0" * 64,
                    "observed_sha256": "0" * 64,
                    "final_reset_postcondition": "RUNNING",
                },
            ),
            "recovery_capabilities": ({}, []),
            "recover": (
                {"mechanism": "fake"},
                {
                    "mechanism": "fake",
                    "accepted": True,
                    "verification": "unavailable",
                    "observed_session_postcondition": "unknown",
                },
            ),
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


if __name__ == "__main__":
    unittest.main()
