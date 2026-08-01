from __future__ import annotations

import inspect
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, call, patch

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.firmstore.reports import ReportPaths
from pyocd_debug_mcp.kernel.operations import ManagedOperation
from pyocd_debug_mcp.services.connections import ConnectionManager
from pyocd_debug_mcp.setup_flow.validate import (
    BoardValidator,
    SafetyMapSnapshot,
    ValidationBackend,
    ValidationHooks,
    ValidationInventory,
    ValidationProbe,
    ValidationRequest,
    ValidationResult,
)
from pyocd_debug_mcp.target_errors import TargetConnectionError, TargetControlError
from pyocd_debug_mcp.tools.setup import _load_guidance, build_setup_handlers


class ValidationHonestyTests(unittest.TestCase):
    @staticmethod
    def _jlink_board(board_id: str = "board-1") -> BoardConfig:
        """A board whose probe_family matches the "jlink" ValidationProbe fixtures below.

        FIX 8 (C7/D8): `_connection_matches_probe` now checks provider as well as UID,
        so a handle built with `board=None` (which defaults `probe_family` to
        "unknown") no longer matches a `ValidationProbe(..., "jlink", ...)` -- this
        gives the transport-loss tests a handle whose provider is consistent with the
        probe they compare it against, without changing what any of them assert.
        """

        return BoardConfig(
            board_id=board_id,
            display_name="Board 1",
            mcu_family="family",
            probe_family="jlink",
            pyocd_target="part",
            probe_type="jlink",
            probe_hint_terms=(),
            serial_hint_terms=(),
            test_addr=0,
        )

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

    def test_payload_describes_hard_worker_deadline_and_recovery(self) -> None:
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

        self.assertTrue(any("enforced by the parent process" in constraint for constraint in constraints))
        self.assertTrue(any("reconnect and revalidate" in constraint for constraint in constraints))
        self.assertFalse(any("best-effort" in constraint for constraint in constraints))

    def test_validation_guidance_repeats_provider_recovery(self) -> None:
        guidance = _load_guidance("board-1", "board_validate")["guidance"]
        remedies = guidance["common_remedies"]

        self.assertTrue(any("terminates only that worker" in remedy for remedy in remedies))
        self.assertTrue(any("reconnect and revalidate" in remedy for remedy in remedies))

    def test_setup_tools_preserve_exact_session_local_validation_assignment(self) -> None:
        loader = SimpleNamespace(
            load=Mock(return_value={"status": "setup_tool_loaded"}),
            is_loaded=Mock(return_value=True),
        )
        validator_result = SimpleNamespace(
            to_payload=Mock(return_value={"status": "validation_passed"})
        )
        validator = SimpleNamespace(validate=Mock(return_value=validator_result))
        require_assignment = Mock()
        assignments = {
            "uidless-board": "session:runtime-uidless",
            "hardware-prefix-board": "probe:session:hardware-uid",
        }
        services = cast(
            Any,
            SimpleNamespace(
                loader=loader,
                validator=validator,
                require_assignment=require_assignment,
                assigned_connection=assignments.get,
            ),
        )
        handlers = build_setup_handlers(services)

        uidless_load_payload = json.loads(
            handlers["load_setup_tool"]("uidless-board", "board_validate")
        )
        uidless_validation_payload = json.loads(
            handlers["board_validate"](
                "uidless-board",
                "session:runtime-uidless",
            )
        )
        hardware_load_payload = json.loads(
            handlers["load_setup_tool"]("hardware-prefix-board", "board_validate")
        )
        hardware_validation_payload = json.loads(
            handlers["board_validate"](
                "hardware-prefix-board",
                # Step 5: the opaque connection token, passed back verbatim. The server
                # resolves it against a fresh snapshot; it is never a pyOCD UID.
                "probe:session:hardware-uid",
            )
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            handlers["board_validate"](
                "hardware-prefix-board",
                "session:runtime-uidless",
            )

        self.assertEqual(uidless_load_payload["status"], "setup_tool_loaded")
        self.assertEqual(uidless_validation_payload["status"], "validation_passed")
        self.assertEqual(hardware_load_payload["status"], "setup_tool_loaded")
        self.assertEqual(hardware_validation_payload["status"], "validation_passed")
        self.assertEqual(
            [call.kwargs["validation_probe_id"] for call in loader.load.call_args_list],
            ["session:runtime-uidless", "probe:session:hardware-uid"],
        )
        self.assertEqual(
            require_assignment.call_args_list,
            [
                call("uidless-board", "session:runtime-uidless"),
                call(
                    "hardware-prefix-board",
                    "probe:session:hardware-uid",
                ),
            ],
        )
        self.assertEqual(
            [call.args[0].probe_id for call in validator.validate.call_args_list],
            ["session:runtime-uidless", "probe:session:hardware-uid"],
        )

    def test_validation_connect_passes_its_step_deadline_to_the_worker_open(self) -> None:
        from pyocd_debug_mcp import server

        board = BoardConfig(
            board_id="board-1",
            display_name="Board 1",
            mcu_family="family",
            probe_family="jlink",
            pyocd_target="part",
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
            patch.object(server.gate_manager, "clear"),
        ):
            server._validation_connect(profile, probe, 2.75)

        self.assertEqual(opened.call_args.kwargs["operation_timeout_seconds"], 2.75)

    def test_validation_read_passes_its_step_deadline_to_the_worker_call(self) -> None:
        from pyocd_debug_mcp import server

        handle = TargetSessionHandle(None, None, "serial-1", "worker", None)
        connection = server._ValidationConnection(handle, False, board_id="board-1")
        with patch.object(server.target_control, "read_memory", return_value=0x12) as read:
            self.assertEqual(server._validation_read(connection, 0x1000, 32, 3.25), 0x12)

        self.assertEqual(read.call_args.kwargs["operation_timeout_seconds"], 3.25)

    def test_uidless_validation_reports_session_local_identity_wording(self) -> None:
        board = BoardConfig(
            board_id="uidless-board",
            display_name="UID-less board",
            mcu_family="family",
            probe_family="jlink",
            pyocd_target="part",
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
        stamp = Mock(return_value=True)
        validator = BoardValidator(
            cast(Any, SimpleNamespace(load=Mock(return_value=profile))),
            Mock(),
            ValidationBackend(
                inventory=lambda: ValidationInventory(probes=(probe,)),
                target_supported=lambda _target: True,
                connect=lambda *_args: object(),
                read_memory=lambda *_args: 0x1234,
                capture_serial=Mock(),
                close=Mock(),
            ),
            hooks=ValidationHooks(
                load_safety_map=lambda _profile: SafetyMapSnapshot(
                    True, True, "map-digest"
                ),
                stamp_session=stamp,
                record_mismatch=lambda *_args: False,
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
        self.assertEqual(
            stamp.call_args.args[2:4],
            ("session:runtime-uidless", None),
        )

    def test_validation_holds_stable_board_lock_through_close_and_report(self) -> None:
        manager = ConnectionManager()
        board = BoardConfig(
            board_id="board-1",
            display_name="Board 1",
            mcu_family="family",
            probe_family="jlink",
            pyocd_target="part",
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
                            TargetSessionHandle(
                                None, None, "replacement-probe", "worker", None
                            ),
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
            capture_serial=Mock(),
            close=close_connection,
        )
        hooks = ValidationHooks(
            load_safety_map=lambda _profile: SafetyMapSnapshot(True, True, "map-digest"),
            stamp_session=lambda *_args: board_lock_is_held(),
            record_mismatch=lambda *_args: False,
        )
        validator = BoardValidator(
            cast(Any, SimpleNamespace(load=Mock(return_value=profile))),
            Mock(),
            backend,
            hooks=hooks,
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

    def test_validation_report_failure_rolls_back_while_board_lock_is_held(self) -> None:
        manager = ConnectionManager()
        board = BoardConfig(
            board_id="board-1",
            display_name="Board 1",
            mcu_family="family",
            probe_family="jlink",
            pyocd_target="part",
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
        rollback_lock_observed: list[bool] = []

        def lock_is_held() -> bool:
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
            return not result[0]

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
                capture_serial=Mock(),
                close=Mock(),
            ),
            hooks=ValidationHooks(
                load_safety_map=lambda _profile: SafetyMapSnapshot(True, True, "map-digest"),
                stamp_session=lambda *_args: True,
                record_mismatch=lambda *_args: False,
                rollback_session=lambda *_args: rollback_lock_observed.append(lock_is_held()),
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

        self.assertEqual(rollback_lock_observed, [True])

    def test_transport_loss_evicts_only_the_exact_existing_assignment(self) -> None:
        from pyocd_debug_mcp import server

        manager = ConnectionManager()
        first_handle = TargetSessionHandle(None, self._jlink_board(), "probe-1", "worker-1", None)
        second_handle = TargetSessionHandle(None, None, "probe-2", "worker-2", None)
        first_runtime = Mock(name="first_runtime")
        second_runtime = Mock(name="second_runtime")
        first_assignment = manager.assign("board-1", first_handle, first_runtime)
        second_assignment = manager.assign("board-2", second_handle, second_runtime)
        profile = SimpleNamespace(board_id="board-1")
        probe = ValidationProbe("probe-1", "Probe 1", "jlink", "probe-1")
        gate = SimpleNamespace(clear=Mock())

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "gate_manager", gate),
            patch.object(
                server.target_control,
                "read_memory",
                side_effect=TargetConnectionError("worker transport lost"),
            ),
            patch.object(server.target_control, "close_session") as close_session,
            patch.object(server._session_store, "close_session") as close_runtime,
        ):
            connection = server._validation_connect(profile, probe, 1.0)
            with self.assertRaisesRegex(TargetConnectionError, "transport lost"):
                server._validation_read(connection, 0, 32, 1.0)
            server._validation_close(connection)

        self.assertIsNone(manager.maybe_connection("board-1"))
        self.assertIs(manager.maybe_connection("board-2"), second_assignment)
        gate.clear.assert_called_once_with("board-1", "validation transport lost")
        close_runtime.assert_called_once_with(first_assignment.runtime_session)
        close_session.assert_called_once_with(first_handle)

    def test_transport_loss_does_not_evict_a_concurrent_replacement(self) -> None:
        from pyocd_debug_mcp import server

        manager = ConnectionManager()
        stale_handle = TargetSessionHandle(None, self._jlink_board(), "probe-1", "worker-1", None)
        replacement_handle = TargetSessionHandle(None, None, "probe-2", "worker-2", None)
        stale_runtime = Mock(name="stale_runtime")
        replacement_runtime = Mock(name="replacement_runtime")
        manager.assign("board-1", stale_handle, stale_runtime)
        profile = SimpleNamespace(board_id="board-1")
        probe = ValidationProbe("probe-1", "Probe 1", "jlink", "probe-1")
        gate = SimpleNamespace(clear=Mock())

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "gate_manager", gate),
            patch.object(
                server.target_control,
                "read_memory",
                side_effect=TargetConnectionError("worker transport lost"),
            ),
            patch.object(server.target_control, "close_session") as close_session,
            patch.object(server._session_store, "close_session") as close_runtime,
        ):
            connection = server._validation_connect(profile, probe, 1.0)
            with self.assertRaises(TargetConnectionError):
                server._validation_read(connection, 0, 32, 1.0)
            manager.clear("board-1")
            replacement = manager.assign("board-1", replacement_handle, replacement_runtime)
            server._validation_close(connection)

        self.assertIs(manager.maybe_connection("board-1"), replacement)
        gate.clear.assert_not_called()
        close_runtime.assert_not_called()
        close_session.assert_not_called()

    def test_transport_loss_gate_failure_still_closes_runtime_and_worker(self) -> None:
        from pyocd_debug_mcp import server

        manager = ConnectionManager()
        handle = TargetSessionHandle(None, self._jlink_board(), "probe-1", "worker-1", None)
        runtime = Mock(name="runtime")
        assignment = manager.assign("board-1", handle, runtime)
        profile = SimpleNamespace(board_id="board-1")
        probe = ValidationProbe("probe-1", "Probe 1", "jlink", "probe-1")
        gate_failure = OSError("gate persistence failed")
        gate = SimpleNamespace(clear=Mock(side_effect=gate_failure))

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "gate_manager", gate),
            patch.object(
                server.target_control,
                "read_memory",
                side_effect=TargetConnectionError("worker transport lost"),
            ),
            patch.object(server.target_control, "close_session") as close_session,
            patch.object(server._session_store, "close_session") as close_runtime,
        ):
            connection = server._validation_connect(profile, probe, 1.0)
            with self.assertRaises(TargetConnectionError):
                server._validation_read(connection, 0, 32, 1.0)
            with self.assertRaises(OSError) as raised:
                server._validation_close(connection)

        self.assertIs(raised.exception, gate_failure)
        self.assertIsNone(manager.maybe_connection("board-1"))
        gate.clear.assert_called_once_with("board-1", "validation transport lost")
        close_runtime.assert_called_once_with(assignment.runtime_session)
        close_session.assert_called_once_with(handle)

    def test_promoted_validation_rollback_gate_failure_still_closes_everything(self) -> None:
        from pyocd_debug_mcp import server

        manager = ConnectionManager()
        handle = TargetSessionHandle(None, None, "probe-1", "worker-1", None)
        runtime = Mock(name="runtime")
        assignment = manager.assign("board-1", handle, runtime)
        connection = server._ValidationConnection(
            handle,
            False,
            board_id="board-1",
            promoted=True,
            assignment=assignment,
        )
        gate_failure = OSError("gate persistence failed")
        gate = SimpleNamespace(
            snapshot=Mock(return_value=None),
            current_mismatch=Mock(return_value=None),
            clear=Mock(side_effect=gate_failure),
        )

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "gate_manager", gate),
            patch.object(server.target_control, "close_session") as close_session,
            patch.object(server._session_store, "close_session") as close_runtime,
        ):
            with self.assertRaises(OSError) as raised:
                server._validation_close(connection)

        self.assertIs(raised.exception, gate_failure)
        self.assertIsNone(manager.maybe_connection("board-1"))
        gate.clear.assert_called_once_with(
            "board-1", "validation connection was not stamped"
        )
        close_runtime.assert_called_once_with(runtime)
        close_session.assert_called_once_with(handle)

    def test_reset_release_transport_loss_evicts_only_the_exact_assignment(self) -> None:
        from pyocd_debug_mcp import server

        manager = ConnectionManager()
        failed_handle = TargetSessionHandle(None, None, "probe-1", "worker-1", None)
        other_handle = TargetSessionHandle(None, None, "probe-2", "worker-2", None)
        failed_runtime = Mock(name="failed_runtime")
        other_runtime = Mock(name="other_runtime")
        failed = manager.assign("board-1", failed_handle, failed_runtime)
        other = manager.assign("board-2", other_handle, other_runtime)
        operation = self._managed_operation("board-1")
        gate = SimpleNamespace(clear=Mock())

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "gate_manager", gate),
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
        self.assertTrue(any("reset transport lost" in error for error in operation.resources.cleanup_errors))
        gate.clear.assert_called_once_with(
            "board-1",
            "target connection failed while releasing reset",
        )
        close_session.assert_called_once_with(failed.handle)
        close_runtime.assert_called_once_with(failed.runtime_session)

    def test_reset_release_transport_loss_preserves_concurrent_replacement(self) -> None:
        from pyocd_debug_mcp import server

        manager = ConnectionManager()
        stale_handle = TargetSessionHandle(None, None, "probe-1", "worker-1", None)
        replacement_handle = TargetSessionHandle(None, None, "probe-2", "worker-2", None)
        manager.assign("board-1", stale_handle, Mock(name="stale_runtime"))
        operation = self._managed_operation("board-1")
        gate = SimpleNamespace(clear=Mock())

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "gate_manager", gate),
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
        gate.clear.assert_not_called()
        close_session.assert_not_called()
        close_runtime.assert_not_called()

    def test_reset_release_control_error_does_not_evict_assignment(self) -> None:
        from pyocd_debug_mcp import server

        manager = ConnectionManager()
        handle = TargetSessionHandle(None, None, "probe-1", "worker-1", None)
        assignment = manager.assign("board-1", handle, Mock(name="runtime"))
        operation = self._managed_operation("board-1")
        gate = SimpleNamespace(clear=Mock())

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "gate_manager", gate),
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
        gate.clear.assert_not_called()
        close_session.assert_not_called()
        close_runtime.assert_not_called()

    def test_reset_release_secondary_cleanup_failure_reports_both_errors_portably(self) -> None:
        from pyocd_debug_mcp import server

        manager = ConnectionManager()
        handle = TargetSessionHandle(None, None, "probe-1", "worker-1", None)
        runtime = Mock(name="runtime")
        manager.assign("board-1", handle, runtime)
        operation = self._managed_operation("board-1")
        gate = SimpleNamespace(clear=Mock())

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "gate_manager", gate),
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
        from pyocd_debug_mcp import server
        from pyocd_debug_mcp.adapters import swd_process

        client = cast(Any, object.__new__(swd_process._WorkerClient))
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
        gate = SimpleNamespace(clear=Mock())

        with patch.object(
            swd_process.ProcessMarkerStore,
            "remove",
            side_effect=(OSError("marker unlink denied"), None),
        ) as remove:
            with self.assertRaises(TargetConnectionError) as raised:
                client._invalidate("Worker read_memory failed: EOFError: pipe closed.")
            operation.error = raised.exception

            with (
                patch.object(server, "connection_manager", manager),
                patch.object(server, "gate_manager", gate),
                patch.object(server._session_store, "close_session") as close_runtime,
                patch.object(server.target_control, "release_reset"),
            ):
                server._bind_managed_board_resources(operation)
                operation.resources.cleanup(preserve_halt=False)

        self.assertIsNone(manager.maybe_connection("board-1"))
        self.assertIs(manager.maybe_connection("board-2"), other)
        gate.clear.assert_called_once_with("board-1", "target connection failed during operation")
        close_runtime.assert_called_once_with(failed.runtime_session)
        self.assertIsNone(client._marker)
        self.assertEqual(remove.call_count, 2)
        self.assertEqual(operation.resources.cleanup_errors, [])


if __name__ == "__main__":
    unittest.main()
