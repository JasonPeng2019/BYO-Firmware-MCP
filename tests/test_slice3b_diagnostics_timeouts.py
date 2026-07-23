from __future__ import annotations

import inspect
import io
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import anyio
from anyio.to_thread import run_sync as run_sync_in_thread
from firmware_mcp.adapters.uart_interface import UARTInterface, UARTPortHandle
from firmware_mcp.kernel.hygiene import cleanup_stale_owned_processes
from firmware_mcp.kernel.operations import (
    OperationCancelledError,
    OperationCleanupError,
    OperationManager,
    OperationResources,
    cancellation_checkpoint,
    dispatch,
)
from firmware_mcp.kernel.processes import ProcessMarkerStore, run_owned
from firmware_mcp.native_build import build_firmware
from firmware_mcp.setup_flow.device_support import _svd_peripheral_regions


class _RestoreFailingTransport:
    def __init__(self) -> None:
        self._timeout = 1.0

    @property
    def timeout(self) -> float:
        return self._timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        if value == 1.0:
            raise OSError("restore denied")
        self._timeout = value


class _FailingReadAdapter(UARTInterface):
    def open(self, device: str, *, baudrate: int, timeout_seconds: float) -> UARTPortHandle:
        raise AssertionError("not used")

    def close(self, handle: UARTPortHandle) -> None:
        del handle

    def reset_input_buffer(self, handle: UARTPortHandle) -> None:
        del handle

    def read(self, handle: UARTPortHandle, size: int) -> bytes:
        del handle, size
        raise RuntimeError("read dropped")

    def write(self, handle: UARTPortHandle, data: bytes) -> int:
        del handle, data
        return 0


class Slice3BDiagnosticsAndTimeoutTests(unittest.TestCase):
    def test_no_default_server_deadline_or_hygiene_cap(self) -> None:
        self.assertIsNone(inspect.signature(dispatch).parameters["timeout"].default)
        self.assertIsNone(inspect.signature(build_firmware).parameters["timeout_seconds"].default)
        self.assertNotIn(
            "timeout_seconds", inspect.signature(cleanup_stale_owned_processes).parameters
        )

    def test_malformed_optional_svd_is_an_explicit_capability_error(self) -> None:
        capability = _svd_peripheral_regions(SimpleNamespace(svd=io.BytesIO(b"<device")))

        self.assertEqual(capability.status, "error")
        self.assertEqual(capability.error_type, "ParseError")
        self.assertTrue(capability.error_message)
        self.assertEqual(capability.regions, ())

    def test_uart_read_and_timeout_restoration_failures_are_both_visible(self) -> None:
        adapter = _FailingReadAdapter()
        handle = UARTPortHandle(_RestoreFailingTransport(), "COM_TEST", 115200, 1.0)

        with self.assertRaisesRegex(RuntimeError, "read dropped") as raised:
            adapter.read_with_timeout(handle, 16, timeout_seconds=0.25)

        self.assertTrue(
            any(
                "timeout restoration failed: OSError: restore denied" in note
                for note in raised.exception.__notes__
            )
        )

    def test_build_honors_only_the_explicit_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            unrestricted = build_firmware(
                str(project),
                str(root / "unrestricted"),
                [sys.executable, "-c", "import time; time.sleep(0.05)"],
            )
            timed = build_firmware(
                str(project),
                str(root / "timed"),
                [sys.executable, "-c", "import time; time.sleep(1)"],
                timeout_seconds=0.01,
            )

        self.assertEqual(unrestricted["status"], "build_succeeded")
        self.assertIsNone(unrestricted["timeout_seconds"])
        self.assertEqual(timed["status"], "build_timeout")
        self.assertEqual(timed["timeout_seconds"], 0.01)

    def test_cancellation_terminates_exact_owned_process_and_preserves_other_board(self) -> None:
        async def scenario() -> None:
            manager = OperationManager()
            with tempfile.TemporaryDirectory() as temporary:
                store = ProcessMarkerStore(Path(temporary))
                started = threading.Event()

                def hanging_build() -> None:
                    started.set()
                    run_owned(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        marker_store=store,
                    )

                async def invoke_hanging_build() -> None:
                    with self.assertRaises(OperationCancelledError):
                        await dispatch(
                            "build_firmware",
                            "board-a",
                            hanging_build,
                            request_id="cancelled-build",
                            manager=manager,
                        )

                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(invoke_hanging_build)
                    await run_sync_in_thread(started.wait)
                    self.assertEqual(manager.cancel_request("cancelled-build"), 1)

                other_board = await dispatch(
                    "get_target_state",
                    "board-b",
                    lambda: "still-live",
                    manager=manager,
                )
                self.assertEqual(other_board, "still-live")
                self.assertEqual(list(Path(temporary).glob("*.json")), [])

        anyio.run(scenario)

    def test_unconfirmed_debug_cleanup_is_fatal_but_keeps_primary_cancellation(self) -> None:
        async def scenario() -> None:
            manager = OperationManager()
            released = threading.Event()
            bound = threading.Event()
            marker_evidence = "worker cleanup unconfirmed; marker retained: board-a-worker.json"

            def bind_resources(operation: object) -> None:
                managed = cast(Any, operation)
                managed.resources.close_debug.append(
                    lambda: {
                        "closed": False,
                        "diagnostic": marker_evidence,
                    }
                )
                managed.add_cancellation_callback(released.set)
                bound.set()

            def wait_for_cancellation() -> None:
                released.wait()
                cancellation_checkpoint()

            async def invoke() -> None:
                with self.assertRaises(OperationCleanupError) as raised:
                    await dispatch(
                        "connect_board",
                        "board-a",
                        wait_for_cancellation,
                        request_id="unconfirmed-debug-cleanup",
                        manager=manager,
                        resource_binder=bind_resources,
                    )
                self.assertIn("OperationCancelledError", str(raised.exception))
                self.assertIn(marker_evidence, str(raised.exception))

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(invoke)
                await run_sync_in_thread(bound.wait)
                await run_sync_in_thread(
                    lambda: manager.cancel_request("unconfirmed-debug-cleanup")
                )

        anyio.run(scenario)

    def test_proven_forced_debug_close_is_diagnostic_not_fatal(self) -> None:
        resources = OperationResources()
        resources.close_debug.append(
            lambda: {
                "closed": True,
                "graceful": False,
                "diagnostic": "EOFError: graceful worker close dropped",
            }
        )

        resources.cleanup(preserve_halt=False)

        self.assertEqual(resources.fatal_cleanup_errors, [])
        self.assertEqual(
            resources.cleanup_errors,
            [
                "debug session was force-closed after graceful close failed: "
                "EOFError: graceful worker close dropped"
            ],
        )

    def test_uart_contract_requires_a_caller_duration_in_docs_and_live_schemas(self) -> None:
        from firmware_mcp import server

        contract = (Path(__file__).parents[1] / "docs" / "client-contract.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("caller-supplied positive finite `timeout_seconds`", contract)
        self.assertIn("caller-chosen", contract)
        self.assertNotIn("default `timeout_seconds=3.0`", contract)
        self.assertNotIn("default `timeout_seconds=1.0`", contract)
        for name in ("read_serial", "write_serial", "exchange_serial"):
            with self.subTest(name=name):
                schema = server.mcp._tool_manager._tools[name].parameters
                self.assertIn("timeout_seconds", schema["required"])
                self.assertIn(f"{name}(", contract)

    def test_fifty_explicitly_timed_hanging_owned_processes_leave_no_markers(self) -> None:
        # The timeout is the test's explicit semantic duration.  This exercises
        # the former fallback-leak shape repeatedly without adding a production
        # cleanup grace interval or sharing marker ownership across processes.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ProcessMarkerStore(root)
            for iteration in range(50):
                with (
                    self.subTest(iteration=iteration),
                    self.assertRaises(subprocess.TimeoutExpired),
                ):
                    run_owned(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        marker_store=store,
                        timeout_seconds=0.001,
                    )

            self.assertEqual(list(root.glob("*.json")), [])

    def test_main_reports_every_disconnect_failure_before_nonzero_exit(self) -> None:
        from firmware_mcp import server

        stream = io.StringIO()
        with (
            patch.object(server, "require_clean_startup"),
            patch.object(server.mcp, "run"),
            patch.object(
                server.connection_manager, "assigned_board_ids", return_value=("alpha", "beta")
            ),
            patch.object(
                server,
                "disconnect",
                side_effect=(OSError("alpha lost"), RuntimeError("beta stuck")),
            ) as disconnect,
            patch.object(server.sys, "stderr", stream),
        ):
            with self.assertRaisesRegex(RuntimeError, "alpha.*beta"):
                server.main()

        self.assertEqual([call.args[0] for call in disconnect.call_args_list], ["alpha", "beta"])
        self.assertIn("disconnect_board(alpha) failed: OSError: alpha lost", stream.getvalue())
        self.assertIn("disconnect_board(beta) failed: RuntimeError: beta stuck", stream.getvalue())

    def test_source_audit_locks_removed_policy_deadline_terms(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "firmware_mcp"
        source = "\n".join(path.read_text(encoding="utf-8") for path in source_root.rglob("*.py"))
        for removed in (
            "DEFAULT_OPERATION_TIMEOUT_SECONDS",
            "DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS",
            "ServerTimeoutConfig",
            "server_timeouts",
            "NO_INTERNALS_RELAY_INSTRUCTION",
            "SAFE_EXIT_REMINDER",
            "PolicyRefusal",
            "ToolOutcome.REFUSED",
            "_offline_environment",
            "reopen_count",
            "retry budget",
        ):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, source)

    def test_remaining_buffer_constants_are_documented_as_non_limits(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "firmware_mcp"
        classified = {
            source_root / "adapters" / "swd_pyocd.py": "Transport buffering only",
            source_root / "services" / "uart_capture.py": "transport buffering only",
            source_root / "pack_index_repair.py": "Stream buffering only",
            source_root / "pack_provision.py": "Stream buffering only",
        }
        for path, classification in classified.items():
            with self.subTest(path=path.name):
                self.assertIn(classification, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
