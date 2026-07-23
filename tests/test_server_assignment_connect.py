from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from firmware_mcp import server
from firmware_mcp.adapters.debug_interface import (
    TargetSessionHandle,
    TargetSessionMetadata,
)
from firmware_mcp.board_config import BoardConfig
from firmware_mcp.firmstore.profiles import ProfileError
from firmware_mcp.services.connections import ConnectionManager
from firmware_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from firmware_mcp.target_errors import TargetConnectionError


FIRST_PROBE = "683710208"
SECOND_PROBE = "683854191"


def _board(board_id: str) -> BoardConfig:
    return BoardConfig(
        board_id=board_id,
        display_name="Test nRF52840 board",
        mcu_family="nrf52",
        probe_family="jlink",
        target="nrf52840",
        probe_type="jlink",
        probe_hint_terms=("j-link", "nrf52840"),
        serial_hint_terms=(),
        test_addr=0x20000000,
    )


def _inventory(*probe_uids: str) -> ValidationInventory:
    return ValidationInventory(
        probes=tuple(ValidationProbe(uid, f"J-Link {uid}", "jlink", uid) for uid in probe_uids)
    )


class _ConnectionManager:
    def __init__(self) -> None:
        self.assigned: list[tuple[str, TargetSessionHandle, str]] = []
        self._lock = threading.RLock()

    def lock_for(self, board_id: str) -> threading.RLock:
        del board_id
        return self._lock

    def maybe_connection(self, board_id: str) -> None:
        del board_id
        return None

    def assign(
        self,
        board_id: str,
        handle: TargetSessionHandle,
        runtime_session: object,
        *,
        connection_id: str,
    ) -> SimpleNamespace:
        self.assigned.append((board_id, handle, connection_id))
        return SimpleNamespace(
            board_id=board_id,
            handle=handle,
            connection_id=connection_id,
            runtime_session=runtime_session,
        )


class _SessionStore:
    def start_session(
        self,
        *,
        board_id: str,
        connection_id: str,
        probe_uid: str | None,
        route_used: str | None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            session_id="session-test",
            board_id=board_id,
            connection_id=connection_id,
            probe_uid=probe_uid,
            route_used=route_used,
        )

    def close_session(self, runtime_session: object) -> None:
        del runtime_session


class AssignmentAwareConnectTests(unittest.TestCase):
    def _connect_with(
        self,
        *,
        board_id: str,
        assigned_connection: str | None,
        inventory: ValidationInventory,
        generic_uid: str = FIRST_PROBE,
        requested_connection: str | None = None,
    ) -> tuple[str, Mock, Mock]:
        board = _board(board_id)
        connection_manager = _ConnectionManager()
        open_session = Mock(
            side_effect=lambda **kwargs: TargetSessionHandle(
                session=SimpleNamespace(board=SimpleNamespace(name="nRF52840")),
                board=kwargs["board"],
                probe_uid=kwargs["unique_id"],
                route_used="test-route",
                target_override=kwargs["target"],
            )
        )
        generic_resolution = Mock(
            return_value=SimpleNamespace(
                probe=SimpleNamespace(uid=generic_uid),
                note="generic discovery selected a probe",
            )
        )
        profile_repository = SimpleNamespace(
            load=Mock(side_effect=ProfileError("no stored profile")),
            store=SimpleNamespace(
                layout=SimpleNamespace(board_profile=lambda unused: Path("missing-profile.json"))
            ),
        )
        assignment_store = SimpleNamespace(connection_for=Mock(return_value=assigned_connection))

        with (
            patch.object(server, "assignment_store", assignment_store),
            patch.object(server, "connection_manager", connection_manager),
            patch.object(server, "_validation_inventory", return_value=inventory),
            patch.object(server, "resolve_board_config", return_value=board),
            patch.object(server, "resolve_probe_for_board_cli", generic_resolution),
            patch.object(server, "_profile_repository", profile_repository),
            patch.object(server.target_control, "open_session", open_session),
            patch.object(server, "_session_store", _SessionStore()),
            patch.object(server, "_record_event", Mock()),
        ):
            result = server._connect_impl(board_id, requested_connection)

        return result, open_session, generic_resolution

    def test_normal_connect_honors_live_assignment_to_second_probe(self) -> None:
        result, open_session, generic_resolution = self._connect_with(
            board_id="lora_tester_2",
            assigned_connection=f"probe:{SECOND_PROBE}",
            inventory=_inventory(FIRST_PROBE, SECOND_PROBE),
        )

        self.assertEqual(open_session.call_args.kwargs["unique_id"], SECOND_PROBE)
        self.assertIn(SECOND_PROBE, result)
        generic_resolution.assert_not_called()

    def test_canonical_pyocd_plan_assignment_is_reresolved_to_raw_backend_uid(self) -> None:
        _, open_session, generic_resolution = self._connect_with(
            board_id="lora_tester_2",
            assigned_connection="probe:000123",
            inventory=_inventory("000123"),
            requested_connection="probe:000123",
        )

        self.assertEqual(open_session.call_args.kwargs["unique_id"], "000123")
        generic_resolution.assert_not_called()

    def test_canonical_pyocd_plan_route_change_or_removal_blocks_before_backend(self) -> None:
        profile_repository = SimpleNamespace(
            load=Mock(side_effect=ProfileError("no stored profile")),
            store=SimpleNamespace(
                layout=SimpleNamespace(board_profile=lambda unused: Path("missing-profile.json"))
            ),
        )
        for assignment, inventory in (
            ("probe:000456", _inventory("000123", "000456")),
            ("probe:123", _inventory("000123")),
            (None, _inventory("000123")),
            ("probe:000123", _inventory("000456")),
        ):
            with self.subTest(assignment=assignment, inventory=inventory):
                open_session = Mock()
                with (
                    patch.object(
                        server,
                        "assignment_store",
                        SimpleNamespace(connection_for=Mock(return_value=assignment)),
                    ),
                    patch.object(server, "connection_manager", _ConnectionManager()),
                    patch.object(server, "_validation_inventory", return_value=inventory),
                    patch.object(
                        server, "resolve_board_config", return_value=_board("lora_tester_2")
                    ),
                    patch.object(server, "_profile_repository", profile_repository),
                    patch.object(server.target_control, "open_session", open_session),
                    patch.object(server, "_record_event", Mock()),
                ):
                    with self.assertRaises((TargetConnectionError, RuntimeError)):
                        server._connect_impl("lora_tester_2", "probe:000123")
                open_session.assert_not_called()

    def test_under_reset_canonical_assignment_reaches_backend_as_raw_uid(self) -> None:
        board = _board("lora_tester_2")
        connection_manager = _ConnectionManager()
        connect_under_reset = Mock(
            side_effect=lambda **kwargs: TargetSessionHandle(
                session=SimpleNamespace(board=SimpleNamespace(name="nRF52840")),
                board=kwargs["board"],
                probe_uid=kwargs["unique_id"],
                route_used="under-reset",
                target_override=kwargs["target"],
            )
        )
        profile_repository = SimpleNamespace(
            load=Mock(side_effect=ProfileError("no stored profile")),
            store=SimpleNamespace(
                layout=SimpleNamespace(board_profile=lambda unused: Path("missing-profile.json"))
            ),
        )
        with (
            patch.object(
                server,
                "assignment_store",
                SimpleNamespace(connection_for=Mock(return_value="probe:000123")),
            ),
            patch.object(server, "connection_manager", connection_manager),
            patch.object(server, "_validation_inventory", return_value=_inventory("000123")),
            patch.object(server, "resolve_board_config", return_value=board),
            patch.object(server, "_profile_repository", profile_repository),
            patch.object(server.target_control, "connect_under_reset", connect_under_reset),
            patch.object(server, "_session_store", _SessionStore()),
            patch.object(server, "_record_event", Mock()),
        ):
            server._connect_with_wired_reset_impl("lora_tester_2", "probe:000123", None, None)

        self.assertEqual(connect_under_reset.call_args.kwargs["unique_id"], "000123")

    def test_under_reset_canonical_assignment_change_blocks_before_backend(self) -> None:
        board = _board("lora_tester_2")
        profile_repository = SimpleNamespace(
            load=Mock(side_effect=ProfileError("no stored profile")),
            store=SimpleNamespace(
                layout=SimpleNamespace(board_profile=lambda unused: Path("missing-profile.json"))
            ),
        )
        for assignment, inventory in (
            ("probe:123", _inventory("000123")),
            (None, _inventory("000123")),
            ("probe:000123", _inventory("000456")),
        ):
            with self.subTest(assignment=assignment, inventory=inventory):
                connect_under_reset = Mock()
                with (
                    patch.object(
                        server,
                        "assignment_store",
                        SimpleNamespace(connection_for=Mock(return_value=assignment)),
                    ),
                    patch.object(server, "connection_manager", _ConnectionManager()),
                    patch.object(server, "_validation_inventory", return_value=inventory),
                    patch.object(server, "resolve_board_config", return_value=board),
                    patch.object(server, "_profile_repository", profile_repository),
                    patch.object(server.target_control, "connect_under_reset", connect_under_reset),
                    patch.object(server, "_record_event", Mock()),
                ):
                    with self.assertRaises((TargetConnectionError, RuntimeError)):
                        server._connect_with_wired_reset_impl(
                            "lora_tester_2", "probe:000123", None, None
                        )
                connect_under_reset.assert_not_called()

    def test_normal_connect_rejects_missing_assigned_probe_without_fallback(self) -> None:
        open_session = Mock()
        generic_resolution = Mock()
        assignment_store = SimpleNamespace(
            connection_for=Mock(return_value=f"probe:{SECOND_PROBE}")
        )

        with (
            patch.object(server, "assignment_store", assignment_store),
            patch.object(server, "connection_manager", _ConnectionManager()),
            patch.object(server, "_validation_inventory", return_value=_inventory(FIRST_PROBE)),
            patch.object(server, "resolve_board_config", return_value=_board("lora_tester_2")),
            patch.object(server, "resolve_probe_for_board_cli", generic_resolution),
            patch.object(server.target_control, "open_session", open_session),
            patch.object(server, "_record_event", Mock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "assigned probe.*no longer present"):
                server._connect_impl("lora_tester_2")

        open_session.assert_not_called()
        generic_resolution.assert_not_called()

    def test_unassigned_profile_retains_generic_probe_discovery(self) -> None:
        result, open_session, generic_resolution = self._connect_with(
            board_id="unassigned_board",
            assigned_connection=None,
            inventory=_inventory(FIRST_PROBE, SECOND_PROBE),
            generic_uid=FIRST_PROBE,
        )

        self.assertEqual(open_session.call_args.kwargs["unique_id"], FIRST_PROBE)
        self.assertIn(FIRST_PROBE, result)
        generic_resolution.assert_called_once()

    def test_unassigned_ambiguous_windows_inventory_fails_without_backend_open(self) -> None:
        open_session = Mock()
        generic_resolution = Mock(
            return_value=SimpleNamespace(
                probe=None,
                note="multiple matching probes found",
                probes=(
                    SimpleNamespace(uid=FIRST_PROBE),
                    SimpleNamespace(uid=SECOND_PROBE),
                ),
            )
        )
        assignment_store = SimpleNamespace(connection_for=Mock(return_value=None))

        with (
            patch.object(server, "assignment_store", assignment_store),
            patch.object(server, "connection_manager", _ConnectionManager()),
            patch.object(server, "resolve_board_config", return_value=_board("ambiguous_board")),
            patch.object(server, "resolve_probe_for_board_cli", generic_resolution),
            patch.object(server.target_control, "open_session", open_session),
            patch.object(server, "_record_event", Mock()),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "multiple matching probes found.*Rerun setup routing",
            ):
                server._connect_impl("ambiguous_board")

        open_session.assert_not_called()
        generic_resolution.assert_called_once()

    def test_parent_validation_and_connect_inventory_use_runtime_probe_evidence(self) -> None:
        from firmware_mcp.probe_inventory import ProbeInfo

        with (
            patch.object(
                server,
                "list_connected_probes",
                return_value=[ProbeInfo(FIRST_PROBE, "runtime probe", FIRST_PROBE)],
            ),
            patch.object(server, "list_serial_ports", return_value=[]),
            patch.object(server.connection_manager, "assigned_board_ids", return_value=()),
            patch.object(
                server,
                "resolve_probe_for_board_cli",
                return_value=SimpleNamespace(probe=SimpleNamespace(uid=FIRST_PROBE), note=""),
            ),
        ):
            inventory = server._validation_inventory()
            resolved = server._resolve_probe_uid_for_connect(
                _board("board"),
                None,
            )

        self.assertEqual(inventory.probes[0].usb_serial, FIRST_PROBE)
        self.assertEqual(resolved, FIRST_PROBE)

    def test_empty_cli_inventory_includes_active_uidless_connection_as_session_local(self) -> None:
        manager = ConnectionManager()
        board = _board("uidless-board")
        metadata = TargetSessionMetadata(
            board_name=board.display_name,
            probe_description="Frozen UID-less J-Link",
            probe_family="jlink",
            probe_uid=None,
            live_part_number="nRF52840",
            route_used="worker",
            target_override="nrf52840",
            runtime_token="runtime-uidless",
        )
        handle = TargetSessionHandle(
            session=None,
            board=board,
            probe_uid=None,
            route_used="worker",
            target_override="nrf52840",
            metadata=metadata,
        )
        assignment = manager.assign("uidless-board", handle, Mock(name="runtime"))

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "list_connected_probes", return_value=[]),
            patch.object(server, "list_serial_ports", return_value=[]),
        ):
            inventory = server._validation_inventory()

        self.assertEqual(len(inventory.probes), 1)
        probe = inventory.probes[0]
        self.assertEqual(probe.probe_id, assignment.connection_id)
        self.assertEqual(probe.description, "Frozen UID-less J-Link")
        self.assertEqual(probe.probe_family, "jlink")
        self.assertIsNone(probe.usb_serial)
        choice = probe.choice()
        self.assertIn("session-local", choice.label)
        self.assertIn("not stable across reconnects", choice.description)
        self.assertNotIn("unknown serial", choice.label)

    def test_uidless_validation_reuses_only_the_exact_active_assignment(self) -> None:
        manager = ConnectionManager()
        handle = TargetSessionHandle(
            session=None,
            board=_board("uidless-board"),
            probe_uid=None,
            route_used="worker",
            target_override="nrf52840",
            metadata=TargetSessionMetadata(
                board_name="Test board",
                probe_description="UID-less probe",
                probe_family="jlink",
                probe_uid=None,
                live_part_number="nRF52840",
                route_used="worker",
                target_override="nrf52840",
                runtime_token="runtime-uidless",
            ),
        )
        assignment = manager.assign("uidless-board", handle, Mock(name="runtime"))
        profile = SimpleNamespace(board_id="uidless-board")

        with patch.object(server, "connection_manager", manager):
            reused = cast(
                Any,
                server._validation_connect(
                    profile,
                    ValidationProbe(
                        assignment.connection_id,
                        "UID-less probe",
                        "jlink",
                        None,
                    ),
                ),
            )
            with self.assertRaisesRegex(TargetConnectionError, "does not match"):
                server._validation_connect(
                    profile,
                    ValidationProbe("session:another-worker", "Other", "jlink", None),
                )

        self.assertIs(reused.handle, handle)
        self.assertIs(reused.assignment, assignment)

    def test_setup_overview_does_not_prefix_session_local_connection_as_probe(self) -> None:
        uidless = ValidationProbe(
            "session:runtime-uidless",
            "UID-less probe",
            "jlink",
            None,
        )
        with (
            patch.object(server._profile_repository, "load_all", return_value=[]),
            patch.object(
                server,
                "_validation_inventory",
                return_value=ValidationInventory(probes=(uidless,)),
            ),
        ):
            overview = server._setup_overview(None)

        self.assertEqual(overview["status"], "setup_names_required")
        connections = cast(list[dict[str, object]], overview["connections"])
        self.assertEqual(connections[0]["connection_id"], "session:runtime-uidless")
        self.assertIn("session-local", str(connections[0]["friendly_name"]))
        self.assertNotIn("unknown serial", str(connections[0]["friendly_name"]))
        self.assertNotEqual(
            connections[0]["connection_id"],
            "probe:session:runtime-uidless",
        )


if __name__ == "__main__":
    unittest.main()
