from __future__ import annotations

import inspect
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, call, patch

from firmware_mcp.adapters.debug_interface import TargetSessionHandle
from firmware_mcp.board_config import BoardConfig
from firmware_mcp.firmstore.reports import ReportPaths
from firmware_mcp.kernel.operations import ManagedOperation
from firmware_mcp.services.connections import ConnectionManager
from firmware_mcp.services.live_identity import (
    LiveIdentityContradiction,
    LiveIdentityObservationError,
)
from firmware_mcp.setup_flow.validate import (
    BoardValidator,
    ValidationBackend,
    ValidationInventory,
    ValidationProbe,
    ValidationRequest,
    ValidationResult,
)
from firmware_mcp.target_errors import TargetConnectionError, TargetControlError
from firmware_mcp.tools.setup import SetupToolServices, build_setup_handlers


class ValidationHonestyTests(unittest.TestCase):
    @staticmethod
    def _managed_operation(board_id: str) -> ManagedOperation:
        return ManagedOperation(
            operation_id="operation-test",
            request_id="request-test",
            tool_name="read_memory",
            board_id=board_id,
            timeout_seconds=1.0,
            non_interruptible=False,
            preserve_halt=False,
        )

    def test_payload_describes_observation_limits_and_recovery(self) -> None:
        result = ValidationResult(
            status="validation_passed",
            code="validation/passed",
            validation_id="validation-test",
            agent_prompt="Validated.",
            choices=(),
            observed={},
            steps=(),
            report_paths=ReportPaths(Path("report.json"), Path("events.jsonl")),
        )

        constraints = result.to_payload()["constraints"]

        self.assertFalse(any("deadline" in constraint.casefold() for constraint in constraints))
        self.assertTrue(any("diagnostic output" in constraint for constraint in constraints))
        self.assertFalse(any("best-effort" in constraint for constraint in constraints))

    def test_setup_tools_are_direct_and_preserve_assignment(self) -> None:
        validator_result = SimpleNamespace(
            to_payload=Mock(return_value={"status": "validation_passed"})
        )
        validator = SimpleNamespace(validate=Mock(return_value=validator_result))
        setup_response = SimpleNamespace(
            to_payload=Mock(return_value={"status": "setup_blocked", "setup_run_id": "run"})
        )
        workflow = SimpleNamespace(
            start_setup=Mock(return_value=setup_response),
            repair_setup=Mock(return_value=setup_response),
        )
        require_assignment = Mock()
        assignments = {
            "uidless_board": "session:runtime-uidless",
            "hardware_prefix_board": "probe:session:hardware-uid",
        }
        services = SetupToolServices(
            workflow=cast(Any, workflow),
            validator=cast(Any, validator),
            require_assignment=require_assignment,
            assigned_connection=assignments.get,
        )
        handlers = build_setup_handlers(services)

        setup_payload = json.loads(
            handlers["setup_board"](
                "uidless_board",
                "session:runtime-uidless",
                "Board",
                "MCU",
                False,
                None,
                None,
                "",
            )
        )
        repair_payload = json.loads(
            handlers["repair_board_setup"](
                "uidless_board",
            )
        )
        uidless_validation_payload = json.loads(handlers["validate_board"]("uidless_board"))
        hardware_validation_payload = json.loads(
            handlers["validate_board"](
                "hardware_prefix_board",
            )
        )
        self.assertEqual(
            set(handlers),
            {
                "get_setup_overview",
                "setup_board",
                "repair_board_setup",
                "continue_board_setup",
                "validate_board",
                "get_setup_status",
            },
        )
        self.assertEqual(setup_payload["status"], "setup_blocked")
        self.assertEqual(repair_payload["status"], "setup_blocked")
        self.assertEqual(uidless_validation_payload["status"], "validation_passed")
        self.assertEqual(hardware_validation_payload["status"], "validation_passed")
        self.assertEqual(
            workflow.start_setup.call_args.args[0].connection_id,
            "session:runtime-uidless",
        )
        self.assertEqual(workflow.repair_setup.call_args.args[0], "uidless_board")
        self.assertEqual(
            require_assignment.call_args_list,
            [
                call("uidless_board", "session:runtime-uidless"),
                call("uidless_board", "session:runtime-uidless"),
                call(
                    "hardware_prefix_board",
                    "probe:session:hardware-uid",
                ),
            ],
        )
        self.assertEqual(
            [call.args[0].probe_id for call in validator.validate.call_args_list],
            ["session:runtime-uidless", "session:hardware-uid"],
        )

    def test_validation_reports_map_diagnostics_without_granting_map_authority(self) -> None:
        validator_result = SimpleNamespace(
            to_payload=Mock(return_value={"status": "validation_passed"})
        )
        validator = SimpleNamespace(validate=Mock(return_value=validator_result))
        workflow = SimpleNamespace()
        handlers = build_setup_handlers(
            SetupToolServices(
                workflow=cast(Any, workflow),
                validator=cast(Any, validator),
                safety_map_status=lambda _board_id: {
                    "state": "missing",
                    "digest": None,
                    "remedy": "create a refresh_safety_map plan",
                },
            )
        )

        payload = json.loads(handlers["validate_board"]("board-a"))

        self.assertEqual(payload["status"], "validation_passed")
        self.assertEqual(payload["safety_map"]["state"], "missing")
        self.assertIsNone(payload["safety_map"]["digest"])

    def test_validation_connect_does_not_invent_a_worker_deadline(self) -> None:
        from firmware_mcp import server

        board = BoardConfig(
            board_id="board-1",
            display_name="Board 1",
            mcu_family="family",
            probe_family="jlink",
            target="part",
            probe_type="jlink",
            probe_hint_terms=("probe",),
            serial_hint_terms=("serial",),
            test_addr=0,
        )
        profile = SimpleNamespace(board_id=board.board_id, board=board, device_support=None)
        probe = ValidationProbe("probe-1", "Probe 1", "jlink", "serial-1")
        handle = TargetSessionHandle(None, board, "serial-1", "worker", "part")
        runtime = Mock()

        with (
            patch.object(server.connection_manager, "maybe_connection", return_value=None),
            patch.object(server.target_control, "open_session", return_value=handle) as opened,
            patch.object(server._session_store, "start_session", return_value=runtime),
            patch.object(server.connection_manager, "assign", return_value=Mock()),
        ):
            server._validation_connect(profile, probe)

        self.assertNotIn("operation_timeout_seconds", opened.call_args.kwargs)

    def test_validation_read_does_not_invent_a_worker_deadline(self) -> None:
        from firmware_mcp import server

        handle = TargetSessionHandle(None, None, "serial-1", "worker", None)
        connection = server._ValidationConnection(handle, False, board_id="board-1")
        with patch.object(server.target_control, "read_memory", return_value=0x12) as read:
            self.assertEqual(server._validation_read(connection, 0x1000, 32), 0x12)

        self.assertNotIn("operation_timeout_seconds", read.call_args.kwargs)

    def test_validation_labels_only_verified_identity_mismatch_as_contradiction(self) -> None:
        board = BoardConfig(
            board_id="board-identity",
            display_name="Identity board",
            mcu_family="family",
            probe_family="jlink",
            target="part",
            probe_type="jlink",
            probe_hint_terms=(),
            serial_hint_terms=(),
            test_addr=0,
        )
        profile = SimpleNamespace(
            board_id=board.board_id,
            board=board,
            mcu_part_number="part",
            source_path=Path("missing-profile.json"),
            device_support=None,
        )
        probe = ValidationProbe("probe-identity", "Identity probe", "jlink", "probe-identity")
        reports = ReportPaths(Path("report.json"), Path("events.jsonl"))
        cases = (
            (
                LiveIdentityContradiction("observed mismatch"),
                "validation/live-identity-contradiction",
            ),
            (
                LiveIdentityObservationError("transport read failed"),
                "validation/live-identity-observation-failed",
            ),
        )
        for error, code in cases:
            with self.subTest(code=code):
                validator = BoardValidator(
                    cast(Any, SimpleNamespace(load=Mock(return_value=profile))),
                    Mock(),
                    ValidationBackend(
                        inventory=lambda: ValidationInventory(probes=(probe,)),
                        target_supported=lambda _target: True,
                        connect=lambda *_args: object(),
                        read_memory=lambda *_args: 0,
                        close=Mock(),
                        observe_identity=lambda *_args, error=error: (_ for _ in ()).throw(error),
                    ),
                )
                with patch.object(validator, "_write_report", return_value=reports):
                    result = validator.validate(
                        ValidationRequest("board-identity", "probe-identity")
                    )
                self.assertEqual(result.code, code)
                self.assertIn(str(error), result.agent_prompt)

    def test_replayed_silicon_comparison_rejects_malformed_observation_before_formatting(
        self,
    ) -> None:
        board = BoardConfig(
            board_id="replayed-identity",
            display_name="Replayed identity",
            mcu_family="family",
            probe_family="jlink",
            target="part",
            probe_type="jlink",
            probe_hint_terms=(),
            serial_hint_terms=(),
            silicon_id_addr=0x1000,
            silicon_id_expected=0x1234,
            silicon_id_mask=0xFFFFFFFF,
            silicon_id_width_bits=32,
            silicon_id_label="device id",
            test_addr=0,
        )
        profile = SimpleNamespace(
            board_id=board.board_id,
            board=board,
            mcu_part_number="part",
            source_path=Path("missing-profile.json"),
            device_support=None,
        )
        probe = ValidationProbe("probe-identity", "Identity probe", "jlink", "probe-identity")
        reports = ReportPaths(Path("report.json"), Path("events.jsonl"))
        for observed in (True, None, "0x1234", -1, 1 << 32):
            with self.subTest(observed=repr(observed)):
                validator = BoardValidator(
                    cast(Any, SimpleNamespace(load=Mock(return_value=profile))),
                    Mock(),
                    ValidationBackend(
                        inventory=lambda: ValidationInventory(probes=(probe,)),
                        target_supported=lambda _target: True,
                        connect=lambda *_args: object(),
                        read_memory=lambda *_args, observed=observed: cast(int, observed),
                        close=Mock(),
                    ),
                )
                with patch.object(validator, "_write_report", return_value=reports):
                    result = validator.validate(
                        ValidationRequest("replayed-identity", "probe-identity")
                    )
                self.assertEqual(result.code, "validation/live-identity-observation-failed")
                self.assertIn("malformed", result.agent_prompt)

    def test_uidless_validation_reports_session_local_identity_wording(self) -> None:
        board = BoardConfig(
            board_id="uidless-board",
            display_name="UID-less board",
            mcu_family="family",
            probe_family="jlink",
            target="part",
            probe_type="jlink",
            probe_hint_terms=("probe",),
            serial_hint_terms=(),
            silicon_id_addr=0x1000,
            silicon_id_expected=0x1234,
            silicon_id_mask=0xFFFFFFFF,
            silicon_id_width_bits=32,
            silicon_id_label="device id",
            test_addr=0,
        )
        profile = SimpleNamespace(
            board_id=board.board_id,
            board=board,
            mcu_part_number="part",
            source_path=Path("missing-profile.json"),
            device_support=None,
        )
        probe = ValidationProbe(
            "session:runtime-uidless",
            "UID-less probe",
            "jlink",
            None,
        )
        validator = BoardValidator(
            cast(Any, SimpleNamespace(load=Mock(return_value=profile))),
            Mock(),
            ValidationBackend(
                inventory=lambda: ValidationInventory(probes=(probe,)),
                target_supported=lambda _target: True,
                connect=lambda *_args: object(),
                read_memory=lambda *_args: 0x1234,
                close=Mock(),
            ),
        )
        reports = ReportPaths(Path("report.json"), Path("events.jsonl"))

        with patch.object(validator, "_write_report", return_value=reports):
            result = validator.validate(
                ValidationRequest("uidless-board", "session:runtime-uidless")
            )

        self.assertEqual(result.status, "validation_passed")
        identity_step = next(step for step in result.steps if step.number == 3)
        self.assertIn("session-local", identity_step.name)
        self.assertNotIn("stable probe", identity_step.name)
        self.assertEqual(result.observed["probe_identity"], "session:runtime-uidless")
        self.assertEqual(result.observed["probe_identity_scope"], "session-local")

    def test_validation_holds_stable_board_lock_through_close_and_report(self) -> None:
        manager = ConnectionManager()
        board = BoardConfig(
            board_id="board-1",
            display_name="Board 1",
            mcu_family="family",
            probe_family="jlink",
            target="part",
            probe_type="jlink",
            probe_hint_terms=("probe",),
            serial_hint_terms=(),
            silicon_id_addr=0x1000,
            silicon_id_expected=0x1234,
            silicon_id_mask=0xFFFFFFFF,
            silicon_id_width_bits=32,
            silicon_id_label="device id",
            test_addr=0,
        )
        profile = SimpleNamespace(
            board_id=board.board_id,
            board=board,
            mcu_part_number="part",
            source_path=Path("missing-profile.json"),
            device_support=None,
        )
        connection = object()
        probes = ValidationInventory(
            probes=(ValidationProbe("probe-1", "Probe 1", "jlink", "probe-1"),)
        )
        lock_results: dict[str, bool] = {}
        replacement_started = threading.Event()
        replacement_finished = threading.Event()
        replacement: list[object] = []
        replacement_thread: threading.Thread | None = None

        def inventory() -> ValidationInventory:
            nonlocal replacement_thread

            def install_same_board_replacement() -> None:
                replacement_started.set()
                with manager.lock_for("board-1"):
                    replacement.append(
                        manager.assign(
                            "board-1",
                            TargetSessionHandle(None, None, "replacement-probe", "worker", None),
                            Mock(name="replacement_runtime"),
                        )
                    )
                    replacement_finished.set()

            replacement_thread = threading.Thread(target=install_same_board_replacement)
            replacement_thread.start()
            self.assertTrue(replacement_started.wait(timeout=2))
            self.assertFalse(replacement_finished.wait(timeout=0.05))

            threads = []
            for board_id in ("board-1", "board-2"):

                def try_lock(selected: str = board_id) -> None:
                    lock = manager.lock_for(selected)
                    acquired = lock.acquire(blocking=False)
                    lock_results[selected] = acquired
                    if acquired:
                        lock.release()

                thread = threading.Thread(target=try_lock)
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
            return probes

        def board_lock_is_held() -> bool:
            result: list[bool] = []

            def try_lock() -> None:
                lock = manager.lock_for("board-1")
                acquired = lock.acquire(blocking=False)
                result.append(acquired)
                if acquired:
                    lock.release()

            thread = threading.Thread(target=try_lock)
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            return not result[0]

        close_connection = Mock(
            side_effect=lambda _connection: self.assertTrue(board_lock_is_held())
        )
        backend = ValidationBackend(
            inventory=inventory,
            target_supported=lambda _target: True,
            connect=lambda *_args: connection,
            read_memory=lambda *_args: 0x1234,
            close=close_connection,
        )
        validator = BoardValidator(
            cast(Any, SimpleNamespace(load=Mock(return_value=profile))),
            Mock(),
            backend,
            lock_for_board=manager.lock_for,
        )
        reports = ReportPaths(Path("report.json"), Path("events.jsonl"))

        def write_report(*_args: object) -> ReportPaths:
            self.assertTrue(board_lock_is_held())
            self.assertFalse(replacement_finished.is_set())
            return reports

        with patch.object(
            validator,
            "_write_report",
            side_effect=write_report,
        ):
            result = validator.validate(ValidationRequest("board-1", "probe-1"))

        assert replacement_thread is not None
        replacement_thread.join(timeout=2)
        self.assertFalse(replacement_thread.is_alive())
        self.assertTrue(replacement_finished.is_set())
        self.assertIs(manager.maybe_connection("board-1"), replacement[0])
        self.assertEqual(result.status, "validation_passed")
        self.assertEqual(lock_results, {"board-1": False, "board-2": True})
        close_connection.assert_called_once_with(connection)

    def test_validation_report_failure_preserves_profile_without_authority_rollback(self) -> None:
        manager = ConnectionManager()
        board = BoardConfig(
            board_id="board-1",
            display_name="Board 1",
            mcu_family="family",
            probe_family="jlink",
            target="part",
            probe_type="jlink",
            probe_hint_terms=(),
            serial_hint_terms=(),
            silicon_id_addr=0x1000,
            silicon_id_expected=0x1234,
            silicon_id_mask=0xFFFFFFFF,
            test_addr=0,
        )
        profile = SimpleNamespace(
            board_id=board.board_id,
            board=board,
            mcu_part_number="part",
            source_path=Path("missing-profile.json"),
            device_support=None,
        )
        validator = BoardValidator(
            cast(Any, SimpleNamespace(load=Mock(return_value=profile))),
            Mock(),
            ValidationBackend(
                inventory=lambda: ValidationInventory(
                    probes=(ValidationProbe("probe-1", "Probe 1", "jlink", "probe-1"),)
                ),
                target_supported=lambda _target: True,
                connect=lambda *_args: object(),
                read_memory=lambda *_args: 0x1234,
                close=Mock(),
            ),
            lock_for_board=manager.lock_for,
        )
        with patch.object(
            validator,
            "_write_report",
            side_effect=OSError("report persistence failed"),
        ):
            with self.assertRaisesRegex(OSError, "report persistence failed"):
                validator.validate(ValidationRequest("board-1", "probe-1"))

    def test_transport_loss_evicts_only_the_exact_existing_assignment(self) -> None:
        from firmware_mcp import server

        manager = ConnectionManager()
        first_handle = TargetSessionHandle(None, None, "probe-1", "worker-1", None)
        second_handle = TargetSessionHandle(None, None, "probe-2", "worker-2", None)
        first_runtime = Mock(name="first_runtime")
        second_runtime = Mock(name="second_runtime")
        first_assignment = manager.assign("board-1", first_handle, first_runtime)
        second_assignment = manager.assign("board-2", second_handle, second_runtime)
        profile = SimpleNamespace(board_id="board-1")
        probe = ValidationProbe("probe-1", "Probe 1", "jlink", "probe-1")
        with (
            patch.object(server, "connection_manager", manager),
            patch.object(
                server.target_control,
                "read_memory",
                side_effect=TargetConnectionError("worker transport lost"),
            ),
            patch.object(server.target_control, "close_session") as close_session,
            patch.object(server._session_store, "close_session") as close_runtime,
        ):
            connection = server._validation_connect(profile, probe)
            with self.assertRaisesRegex(TargetConnectionError, "transport lost"):
                server._validation_read(connection, 0, 32)
            server._validation_close(connection)

        self.assertIsNone(manager.maybe_connection("board-1"))
        self.assertIs(manager.maybe_connection("board-2"), second_assignment)
        close_runtime.assert_called_once_with(first_assignment.runtime_session)
        close_session.assert_called_once_with(first_handle)

    def test_transport_loss_does_not_evict_a_concurrent_replacement(self) -> None:
        from firmware_mcp import server

        manager = ConnectionManager()
        stale_handle = TargetSessionHandle(None, None, "probe-1", "worker-1", None)
        replacement_handle = TargetSessionHandle(None, None, "probe-2", "worker-2", None)
        stale_runtime = Mock(name="stale_runtime")
        replacement_runtime = Mock(name="replacement_runtime")
        manager.assign("board-1", stale_handle, stale_runtime)
        profile = SimpleNamespace(board_id="board-1")
        probe = ValidationProbe("probe-1", "Probe 1", "jlink", "probe-1")
        with (
            patch.object(server, "connection_manager", manager),
            patch.object(
                server.target_control,
                "read_memory",
                side_effect=TargetConnectionError("worker transport lost"),
            ),
            patch.object(server.target_control, "close_session") as close_session,
            patch.object(server._session_store, "close_session") as close_runtime,
        ):
            connection = server._validation_connect(profile, probe)
            with self.assertRaises(TargetConnectionError):
                server._validation_read(connection, 0, 32)
            manager.clear("board-1")
            replacement = manager.assign("board-1", replacement_handle, replacement_runtime)
            server._validation_close(connection)

        self.assertIs(manager.maybe_connection("board-1"), replacement)
        close_runtime.assert_not_called()
        close_session.assert_not_called()

    def test_reset_release_transport_loss_evicts_only_the_exact_assignment(self) -> None:
        from firmware_mcp import server

        manager = ConnectionManager()
        failed_handle = TargetSessionHandle(None, None, "probe-1", "worker-1", None)
        other_handle = TargetSessionHandle(None, None, "probe-2", "worker-2", None)
        failed_runtime = Mock(name="failed_runtime")
        other_runtime = Mock(name="other_runtime")
        failed = manager.assign("board-1", failed_handle, failed_runtime)
        other = manager.assign("board-2", other_handle, other_runtime)
        operation = self._managed_operation("board-1")
        with (
            patch.object(server, "connection_manager", manager),
            patch.object(
                server.target_control,
                "release_reset",
                side_effect=TargetConnectionError("reset transport lost"),
            ),
            patch.object(server.target_control, "close_session") as close_session,
            patch.object(server._session_store, "close_session") as close_runtime,
        ):
            server._bind_managed_board_resources(operation)
            operation.resources.cleanup(preserve_halt=False)

        self.assertIsNone(manager.maybe_connection("board-1"))
        self.assertIs(manager.maybe_connection("board-2"), other)
        self.assertTrue(
            any("reset transport lost" in error for error in operation.resources.cleanup_errors)
        )
        close_session.assert_called_once_with(failed.handle)
        close_runtime.assert_called_once_with(failed.runtime_session)

    def test_reset_release_transport_loss_preserves_concurrent_replacement(self) -> None:
        from firmware_mcp import server

        manager = ConnectionManager()
        stale_handle = TargetSessionHandle(None, None, "probe-1", "worker-1", None)
        replacement_handle = TargetSessionHandle(None, None, "probe-2", "worker-2", None)
        manager.assign("board-1", stale_handle, Mock(name="stale_runtime"))
        operation = self._managed_operation("board-1")
        with (
            patch.object(server, "connection_manager", manager),
            patch.object(
                server.target_control,
                "release_reset",
                side_effect=TargetConnectionError("reset transport lost"),
            ),
            patch.object(server.target_control, "close_session") as close_session,
            patch.object(server._session_store, "close_session") as close_runtime,
        ):
            server._bind_managed_board_resources(operation)
            manager.clear("board-1")
            replacement = manager.assign(
                "board-1",
                replacement_handle,
                Mock(name="replacement_runtime"),
            )
            operation.resources.cleanup(preserve_halt=False)

        self.assertIs(manager.maybe_connection("board-1"), replacement)
        close_session.assert_not_called()
        close_runtime.assert_not_called()

    def test_reset_release_control_error_does_not_evict_assignment(self) -> None:
        from firmware_mcp import server

        manager = ConnectionManager()
        handle = TargetSessionHandle(None, None, "probe-1", "worker-1", None)
        assignment = manager.assign("board-1", handle, Mock(name="runtime"))
        operation = self._managed_operation("board-1")
        with (
            patch.object(server, "connection_manager", manager),
            patch.object(
                server.target_control,
                "release_reset",
                side_effect=TargetControlError("reset capability unavailable"),
            ),
            patch.object(server.target_control, "close_session") as close_session,
            patch.object(server._session_store, "close_session") as close_runtime,
        ):
            server._bind_managed_board_resources(operation)
            operation.resources.cleanup(preserve_halt=False)

        self.assertIs(manager.maybe_connection("board-1"), assignment)
        close_session.assert_not_called()
        close_runtime.assert_not_called()

    def test_reset_release_secondary_cleanup_failure_reports_both_errors_portably(self) -> None:
        from firmware_mcp import server

        manager = ConnectionManager()
        handle = TargetSessionHandle(None, None, "probe-1", "worker-1", None)
        runtime = Mock(name="runtime")
        manager.assign("board-1", handle, runtime)
        operation = self._managed_operation("board-1")
        with (
            patch.object(server, "connection_manager", manager),
            patch.object(
                server.target_control,
                "release_reset",
                side_effect=TargetConnectionError("reset transport lost"),
            ),
            patch.object(
                server.target_control,
                "close_session",
                side_effect=TargetConnectionError("dead handle cleanup failed"),
            ),
            patch.object(server._session_store, "close_session") as close_runtime,
        ):
            server._bind_managed_board_resources(operation)
            operation.resources.cleanup(preserve_halt=False)

        self.assertIsNone(manager.maybe_connection("board-1"))
        self.assertEqual(len(operation.resources.cleanup_errors), 1)
        reported = operation.resources.cleanup_errors[0]
        self.assertIn("reset transport lost", reported)
        self.assertIn("dead handle cleanup failed", reported)
        close_runtime.assert_called_once_with(runtime)
        self.assertNotIn(".add_note", inspect.getsource(server._bind_managed_board_resources))

    def test_worker_fault_with_marker_unlink_failure_evicts_only_exact_board(self) -> None:
        from firmware_mcp import server
        from firmware_mcp.adapters import debug_process

        client = cast(Any, object.__new__(debug_process._WorkerClient))
        client._guard = threading.RLock()
        client._closed = False
        client._cleanup_confirmed = False
        client._marker = Path("retained-worker-marker.json")
        client._process = SimpleNamespace(stdin=None, stdout=None)
        client._terminate = Mock(return_value=True)
        manager = ConnectionManager()
        failed_handle = TargetSessionHandle(
            None,
            None,
            "probe-1",
            "worker-1",
            None,
            worker=client,
        )
        failed_runtime = Mock(name="failed_runtime")
        failed = manager.assign("board-1", failed_handle, failed_runtime)
        other = manager.assign(
            "board-2",
            TargetSessionHandle(None, None, "probe-2", "worker-2", None),
            Mock(name="other_runtime"),
        )
        operation = self._managed_operation("board-1")
        with patch.object(
            debug_process.ProcessMarkerStore,
            "remove",
            side_effect=(OSError("marker unlink denied"), None),
        ) as remove:
            with self.assertRaises(TargetConnectionError) as raised:
                client._invalidate("Worker read_memory failed: EOFError: pipe closed.")
            operation.error = raised.exception

            with (
                patch.object(server, "connection_manager", manager),
                patch.object(server._session_store, "close_session") as close_runtime,
                patch.object(server.target_control, "release_reset"),
            ):
                server._bind_managed_board_resources(operation)
                operation.resources.cleanup(preserve_halt=False)

        self.assertIsNone(manager.maybe_connection("board-1"))
        self.assertIs(manager.maybe_connection("board-2"), other)
        close_runtime.assert_called_once_with(failed.runtime_session)
        self.assertIsNone(client._marker)
        self.assertEqual(remove.call_count, 2)
        self.assertEqual(operation.resources.cleanup_errors, [])


if __name__ == "__main__":
    unittest.main()
