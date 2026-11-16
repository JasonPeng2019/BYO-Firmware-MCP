from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pyocd_debug_mcp import server
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.firmstore.profiles import ProfileError
from pyocd_debug_mcp.probe_inventory import registered_provider_ids
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe

import pytest


FIRST_PROBE = "683710208"
SECOND_PROBE = "683854191"


def setUpModule() -> None:
    required = {"cmsisdap", "picoprobe"}
    actual = set(registered_provider_ids())
    if not required <= actual:
        raise RuntimeError(f"Real pyOCD registry lacks fixture prerequisites: {required - actual}")


def _board(board_id: str) -> BoardConfig:
    return BoardConfig(
        board_id=board_id,
        display_name="Test nRF52840 board",
        mcu_family="nrf52",
        probe_family="picoprobe",
        pyocd_target="nrf52840",
        probe_type="picoprobe",
        probe_hint_terms=("picoprobe", "nrf52840"),
        serial_hint_terms=(),
        test_addr=0x20000000,
    )


def _inventory(*probe_uids: str) -> ValidationInventory:
    return ValidationInventory(
        probes=tuple(
            ValidationProbe(uid, f"Picoprobe {uid}", "picoprobe", uid) for uid in probe_uids
        )
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

    def connection_for(self, board_id: str) -> SimpleNamespace:
        raise AssertionError(f"no live connection was assigned for {board_id}")

    def assigned_board_ids(self) -> tuple[str, ...]:
        # The unified inventory service injects already-open probes, which pyOCD omits.
        # This double never holds a live connection, so there is nothing to inject.
        return ()

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
    ) -> tuple[str, Mock, Mock]:
        board = _board(board_id)
        connection_manager = _ConnectionManager()
        open_session = Mock(
            side_effect=lambda **kwargs: TargetSessionHandle(
                session=SimpleNamespace(board=SimpleNamespace(name="nRF52840")),
                board=kwargs["board"],
                probe_uid=kwargs["unique_id"].removeprefix("picoprobe:"),
                route_used="test-route",
                target_override=kwargs["target"],
            )
        )
        generic_resolution = Mock(
            return_value=SimpleNamespace(
                probe=SimpleNamespace(uid=generic_uid, family="picoprobe"),
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
            patch.object(server, "gate_manager", SimpleNamespace(clear=Mock())),
            patch.object(server, "_record_event", Mock()),
        ):
            result = server._connect_impl(board_id, allow_environment_overrides=False)

        return result, open_session, generic_resolution

    @pytest.mark.mvp
    def test_normal_connect_honors_live_assignment_to_second_probe(self) -> None:
        result, open_session, generic_resolution = self._connect_with(
            board_id="lora_tester_2",
            assigned_connection=f"probe:{SECOND_PROBE}",
            inventory=_inventory(FIRST_PROBE, SECOND_PROBE),
        )

        self.assertEqual(open_session.call_args.kwargs["unique_id"], f"picoprobe:{SECOND_PROBE}")
        self.assertIn(SECOND_PROBE, result)
        generic_resolution.assert_not_called()

    @pytest.mark.mvp
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
                server._connect_impl("lora_tester_2", allow_environment_overrides=False)

        open_session.assert_not_called()
        generic_resolution.assert_not_called()


if __name__ == "__main__":
    unittest.main()
