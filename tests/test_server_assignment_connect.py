from __future__ import annotations

import subprocess
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from pyocd_debug_mcp import server
from pyocd_debug_mcp.adapters.swd_interface import (
    TargetSessionHandle,
    TargetSessionMetadata,
)
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.firmstore.profiles import ProfileError
from pyocd_debug_mcp.services.connections import ConnectionManager, probe_connection_id
from pyocd_debug_mcp.setup_flow.setup import RunAssignmentStore
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe
from pyocd_debug_mcp.target_errors import TargetConnectionError


FIRST_PROBE = "683710208"
SECOND_PROBE = "683854191"


def _board(board_id: str) -> BoardConfig:
    return BoardConfig(
        board_id=board_id,
        display_name="Test nRF52840 board",
        mcu_family="nrf52",
        probe_family="jlink",
        pyocd_target="nrf52840",
        probe_type="jlink",
        probe_hint_terms=("j-link", "nrf52840"),
        serial_hint_terms=(),
        test_addr=0x20000000,
    )


def _inventory(*probe_uids: str) -> ValidationInventory:
    return ValidationInventory(
        probes=tuple(
            ValidationProbe(uid, f"J-Link {uid}", "jlink", uid) for uid in probe_uids
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
        assignment_store = SimpleNamespace(
            connection_for=Mock(return_value=assigned_connection)
        )

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

    def test_normal_connect_honors_live_assignment_to_second_probe(self) -> None:
        result, open_session, generic_resolution = self._connect_with(
            board_id="lora_tester_2",
            assigned_connection=f"probe:{SECOND_PROBE}",
            inventory=_inventory(FIRST_PROBE, SECOND_PROBE),
        )

        self.assertEqual(open_session.call_args.kwargs["unique_id"], SECOND_PROBE)
        self.assertIn(SECOND_PROBE, result)
        generic_resolution.assert_not_called()

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
                server._connect_impl("ambiguous_board", allow_environment_overrides=False)

        open_session.assert_not_called()
        generic_resolution.assert_called_once()

    def test_parent_validation_and_connect_inventory_never_call_native_pyocd_discovery(self) -> None:
        native = Mock(side_effect=AssertionError("parent invoked native discovery"))
        cli_listing = f"0  J-Link Probe  jlink:{FIRST_PROBE}\n"
        with (
            patch(
                "pyocd.core.helpers.ConnectHelper.get_all_connected_probes",
                native,
            ),
            patch.object(server, "_run_cmd", return_value=(0, cli_listing, "")) as run_cmd,
            patch.object(server, "list_serial_ports", return_value=[]),
            patch.object(server.connection_manager, "assigned_board_ids", return_value=()),
        ):
            inventory = server._validation_inventory()
            resolved = server._resolve_probe_uid_for_connect(
                _board("board"),
                None,
                allow_environment_override=False,
            )

        self.assertEqual(inventory.probes[0].usb_serial, f"jlink:{FIRST_PROBE}")
        self.assertEqual(resolved, f"jlink:{FIRST_PROBE}")
        native.assert_not_called()
        self.assertGreaterEqual(run_cmd.call_count, 2)

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
            patch.object(server, "_run_cmd", return_value=(0, "", "")),
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
                    1.0,
                ),
            )
            with self.assertRaisesRegex(TargetConnectionError, "does not match"):
                server._validation_connect(
                    profile,
                    ValidationProbe("session:another-worker", "Other", "jlink", None),
                    1.0,
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

    def test_uidless_validation_stamps_exact_session_token_without_probe_prefix(self) -> None:
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
        assignment_store = SimpleNamespace(
            run_if_current=Mock(side_effect=lambda _connection, _board, action: action())
        )
        gate = SimpleNamespace(stamp_validation=Mock())
        profile = SimpleNamespace(device_support=None, mcu_part_number="nRF52840")

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "assignment_store", assignment_store),
            patch.object(server, "gate_manager", gate),
            patch.object(server._profile_repository, "load", return_value=profile),
        ):
            stamped = server._stamp_validation_session(
                "uidless-board",
                "validation-run",
                assignment.connection_id,
                None,
                "nRF52840 0x1234",
                "map-digest",
            )

        self.assertTrue(stamped)
        self.assertEqual(
            assignment_store.run_if_current.call_args.args[:2],
            (assignment.connection_id, "uidless-board"),
        )
        self.assertFalse(assignment.connection_id.startswith("probe:"))
        self.assertEqual(
            gate.stamp_validation.call_args.kwargs["connection_id"],
            assignment.connection_id,
        )
        self.assertEqual(
            gate.stamp_validation.call_args.kwargs["probe_identity"],
            assignment.connection_id,
        )

    def test_a_validation_mismatch_clears_the_assignment_and_records_the_gate_mismatch(
        self,
    ) -> None:
        """M3 (FIX 8 addendum): the raw `f"probe:{probe_uid}"` mint site is gone.

        `_record_validation_mismatch`'s `provisional_connection_id` must reproduce the
        exact canonical (provider-qualified) key `_setup_overview` would have stored,
        or `assignment_store.run_if_current`'s exact-string match would report every
        real mismatch as "assignment changed" and silently drop both the assignment
        clear and the gate mismatch record. Uses the real `RunAssignmentStore`, not a
        stub, so this actually exercises that exact-match boundary end to end.
        """

        manager = ConnectionManager()
        handle = TargetSessionHandle(
            session=None,
            board=_board("board-1"),
            probe_uid=FIRST_PROBE,
            route_used="worker",
            target_override=None,
            metadata=TargetSessionMetadata(
                board_name="Test board",
                probe_description="J-Link probe",
                probe_family="jlink",
                probe_uid=FIRST_PROBE,
                live_part_number=None,
                route_used="worker",
                target_override=None,
                runtime_token="runtime-1",
            ),
        )
        connection = manager.assign("board-1", handle, Mock(name="runtime"))
        # The assignment store is seeded exactly the way `_setup_overview` would seed
        # it: a provider-qualified canonical key, which happens to be the same key
        # `ConnectionManager.assign` above derived for the live connection.
        canonical_connection_id = probe_connection_id("jlink", FIRST_PROBE)
        self.assertEqual(connection.connection_id, canonical_connection_id)
        real_store = RunAssignmentStore({})
        real_store.assign(canonical_connection_id, "board-1")
        gate = SimpleNamespace(record_mismatch=Mock())

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "assignment_store", real_store),
            patch.object(server, "gate_manager", gate),
        ):
            recorded = server._record_validation_mismatch(
                "board-1",
                "validation-run",
                FIRST_PROBE,
                FIRST_PROBE,
                "nRF52840",
                "unexpected 0xDEAD",
            )

        self.assertTrue(recorded, "the mismatch must be recorded, not silently dropped")
        self.assertIsNone(
            real_store.connection_for("board-1"), "the assignment must be cleared"
        )
        gate.record_mismatch.assert_called_once()
        recorded_kwargs = gate.record_mismatch.call_args.kwargs
        self.assertEqual(recorded_kwargs["board_id"], "board-1")
        self.assertEqual(recorded_kwargs["connection_id"], canonical_connection_id)
        self.assertEqual(recorded_kwargs["expected_mcu"], "nRF52840")
        self.assertEqual(recorded_kwargs["observed_mcu"], "unexpected 0xDEAD")

    def test_a_mismatch_after_the_connection_already_vanished_still_matches_via_the_profile(
        self,
    ) -> None:
        """`_known_provider_for_board`'s second source: the board's own profile.

        The live connection is already gone by the time the provisional key is
        rebuilt (a real, if rare, race between the live read and this bookkeeping).
        The board's profile still names the same provider, so the match should still
        succeed via that fallback rather than conservatively reporting "assignment
        changed" when the provider is, in fact, still knowable.
        """

        canonical_connection_id = probe_connection_id("jlink", FIRST_PROBE)
        real_store = RunAssignmentStore({})
        real_store.assign(canonical_connection_id, "board-1")
        gate = SimpleNamespace(record_mismatch=Mock())
        manager = ConnectionManager()  # no live connection: it already vanished
        profile = SimpleNamespace(board=_board("board-1"))

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "assignment_store", real_store),
            patch.object(server, "gate_manager", gate),
            patch.object(server._profile_repository, "load", return_value=profile),
        ):
            recorded = server._record_validation_mismatch(
                "board-1",
                "validation-run",
                FIRST_PROBE,
                FIRST_PROBE,
                "nRF52840",
                "unexpected 0xDEAD",
            )

        self.assertTrue(recorded)
        self.assertIsNone(real_store.connection_for("board-1"))
        gate.record_mismatch.assert_called_once()

    def test_a_stale_divergent_profile_provider_fails_closed_not_wrong(self) -> None:
        """C13/D12: the profile fallback and the stored assignment are independent.

        The sibling test above cannot distinguish safe from unsafe because both sides
        of its comparison come from the same `_board()` fixture. This constructs a
        genuine divergence: the assignment was minted for one provider ("cmsisdap" --
        what was actually connected/assigned), but the board's profile independently
        declares a DIFFERENT provider ("jlink"), built from its own standalone
        `BoardConfig`, not derived from the stored key at all.

        Answer, confirmed empirically by this test rather than assumed: it fails
        closed. `_known_provider_for_board`'s profile fallback mints
        `probe_connection_id("jlink", ...)`, which is simply not the stored key
        (`probe_connection_id("cmsisdap", ...)`) for this board_id --
        `run_if_current`'s two-way exact-match dictionary lookup requires the minted
        key to equal the value stored for *this exact board_id*, so a wrong provider
        can only ever produce "no match" (`SetupWorkflowError` -> `False`, nothing
        recorded, assignment untouched), never a match against a stored key that names
        a different real probe. Wrong attribution would require the assignment store
        to already hold the diverged key under this same board_id, which only
        `_setup_overview` -- itself always minting from the real discovered row's
        actual provider, never from a profile declaration -- ever writes.
        """

        real_provider = "cmsisdap"
        stale_profile_provider = "jlink"
        canonical_connection_id = probe_connection_id(real_provider, FIRST_PROBE)
        real_store = RunAssignmentStore({})
        real_store.assign(canonical_connection_id, "board-1")
        gate = SimpleNamespace(record_mismatch=Mock())
        manager = ConnectionManager()  # no live connection: it already vanished
        # A standalone BoardConfig, independent of the stored key's provider -- not
        # the same fixture the sibling test above shares between both sides.
        diverged_board = BoardConfig(
            board_id="board-1",
            display_name="Diverged board",
            mcu_family="nrf52",
            probe_family=stale_profile_provider,
            pyocd_target="nrf52840",
            probe_type=stale_profile_provider,
            probe_hint_terms=(),
            serial_hint_terms=(),
            test_addr=0x20000000,
        )
        profile = SimpleNamespace(board=diverged_board)

        with (
            patch.object(server, "connection_manager", manager),
            patch.object(server, "assignment_store", real_store),
            patch.object(server, "gate_manager", gate),
            patch.object(server._profile_repository, "load", return_value=profile),
        ):
            recorded = server._record_validation_mismatch(
                "board-1",
                "validation-run",
                FIRST_PROBE,
                FIRST_PROBE,
                "nRF52840",
                "unexpected 0xDEAD",
            )

        self.assertFalse(
            recorded, "a diverged profile provider must fail closed, not silently succeed"
        )
        gate.record_mismatch.assert_not_called()
        # The assignment must be left exactly as it was -- not cleared, not
        # reattributed to the wrong provider.
        self.assertEqual(real_store.connection_for("board-1"), canonical_connection_id)

    def test_cli_command_uses_null_stdin_and_preserves_owned_runner_contract(self) -> None:
        completed = SimpleNamespace(returncode=7, stdout=b"listed", stderr=b"diagnostic")
        with patch.object(server, "run_owned", return_value=completed) as run_owned:
            result = server._run_cmd(["pyocd", "list", "--probes"], timeout_seconds=4.25)

        self.assertEqual(result, (7, "listed", "diagnostic"))
        kwargs = run_owned.call_args.kwargs
        self.assertEqual(run_owned.call_args.args, (["pyocd", "list", "--probes"],))
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(kwargs["capture_output"])
        self.assertFalse(kwargs["text"])
        self.assertEqual(kwargs["timeout"], 4.25)
        self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")

    def test_cli_command_preserves_unicode_output_on_legacy_windows_hosts(self) -> None:
        result = server._run_cmd(
            [sys.executable, "-c", "print('probe status: \\u2716\\ufe0e')"],
            timeout_seconds=4.25,
        )

        self.assertEqual(result[0], 0)
        self.assertEqual(result[1].splitlines(), ["probe status: \u2716\ufe0e"])
        self.assertEqual(result[2], "")

    def test_cli_inventory_hanging_child_is_bounded_and_terminated(self) -> None:
        import sys
        import time

        from pyocd_debug_mcp.probe_inventory import list_connected_probes_cli

        marker_root = Path.home() / ".pyocd-debug-mcp" / "runs" / "owned-processes"
        before = set(marker_root.glob("*.json")) if marker_root.exists() else set()
        started = time.monotonic()

        probes = list_connected_probes_cli(
            lambda _command: server._run_cmd(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                timeout_seconds=0.05,
            )
        )

        self.assertEqual(probes, [])
        self.assertLess(time.monotonic() - started, 2.0)
        after = set(marker_root.glob("*.json")) if marker_root.exists() else set()
        self.assertEqual(after - before, set())

    def test_inventory_reachable_tool_timeouts_compose_every_cli_fallback(self) -> None:
        from pyocd_debug_mcp.kernel import operations
        from pyocd_debug_mcp.kernel.processes import MAX_OWNED_PROCESS_CLEANUP_SECONDS

        existing = {
            "setup_overview": 30.0,
            "connect": 30.0,
            "connect_override": 30.0,
            "get_setup_status": 30.0,
            "connect_under_reset": 30.0,
            "board_validate": 120.0,
            "board_setup": 300.0,
            "board_fix_setup": 300.0,
        }
        for fallback_count in (1, 4, 10):
            commands = tuple(("pyocd", f"fallback-{index}") for index in range(fallback_count))
            derived = (
                operations.DEFAULT_OPERATION_TIMEOUT_SECONDS
                + fallback_count
                * (
                    operations.DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS
                    + MAX_OWNED_PROCESS_CLEANUP_SECONDS
                )
                + operations.CANCELLATION_CLEANUP_GRACE_SECONDS
            )
            with self.subTest(fallback_count=fallback_count), patch.object(
                operations, "configured_probe_cli_commands", return_value=commands
            ):
                for tool_name, prior_timeout in existing.items():
                    with self.subTest(tool_name=tool_name):
                        self.assertEqual(
                            operations.operation_timeout_seconds(
                                tool_name,
                                {"probe_uid": None}
                                if tool_name == "connect_override"
                                else None,
                            ),
                            max(prior_timeout, derived),
                        )
                self.assertEqual(
                    operations.operation_timeout_seconds("get_state"),
                    operations.DEFAULT_OPERATION_TIMEOUT_SECONDS,
                )

    def test_real_sequential_hanging_fallbacks_fit_derived_budget_without_markers(self) -> None:
        import sys
        import time

        from pyocd_debug_mcp import probe_inventory
        from pyocd_debug_mcp.kernel import operations
        from pyocd_debug_mcp.kernel.processes import MAX_OWNED_PROCESS_CLEANUP_SECONDS
        from pyocd_debug_mcp.probe_inventory import list_connected_probes_cli

        execution_timeout = 0.05
        commands = tuple(
            (sys.executable, "-c", "import time; time.sleep(60)") for _ in range(2)
        )
        marker_root = Path.home() / ".pyocd-debug-mcp" / "runs" / "owned-processes"
        before = set(marker_root.glob("*.json")) if marker_root.exists() else set()
        derived_budget = (
            operations.DEFAULT_OPERATION_TIMEOUT_SECONDS
            + len(commands) * (execution_timeout + MAX_OWNED_PROCESS_CLEANUP_SECONDS)
            + operations.CANCELLATION_CLEANUP_GRACE_SECONDS
        )

        started = time.monotonic()
        with patch.object(
            probe_inventory,
            "configured_probe_cli_commands",
            return_value=commands,
        ):
            probes = list_connected_probes_cli(
                lambda command: server._run_cmd(
                    command,
                    timeout_seconds=execution_timeout,
                )
            )
        elapsed = time.monotonic() - started

        self.assertEqual(probes, [])
        self.assertLess(elapsed, derived_budget)
        after = set(marker_root.glob("*.json")) if marker_root.exists() else set()
        self.assertEqual(after - before, set())


class ActiveConnectionRowsToctouTests(unittest.TestCase):
    """FIX 3c (C3/K1): a board disconnecting mid-scan must not raise.

    `assigned_board_ids()` snapshots the key set under lock and releases it before
    `_active_connection_rows` re-acquires the lock per board. The pre-fix code used
    `connection_for`, which re-raises `BoardNotConnectedError` for a board that
    vanished in that gap; the fix uses `maybe_connection` and skips `None` instead,
    which is correct since a board that disconnected between the two calls is, in
    fact, no longer an active connection to report.
    """

    def test_a_board_that_disappears_between_the_two_calls_is_skipped_not_raised(self) -> None:
        board = _board("nrf-1")
        handle = TargetSessionHandle(
            session=None,
            board=board,
            probe_uid="683710208",
            route_used="test",
            target_override=None,
        )
        connection = SimpleNamespace(handle=handle, connection_id="probe:683710208")

        class _RacingConnectionManager:
            def assigned_board_ids(self) -> tuple[str, ...]:
                # The snapshot taken under lock: both boards were assigned at that
                # instant.
                return ("nrf-1", "nrf-vanished")

            def maybe_connection(self, board_id: str) -> SimpleNamespace | None:
                # "nrf-vanished" disconnected in the gap between the two calls --
                # exactly the race `connection_for` used to turn into an unhandled
                # `BoardNotConnectedError` mid-iteration.
                if board_id == "nrf-vanished":
                    return None
                return connection

        with patch.object(server, "connection_manager", _RacingConnectionManager()):
            rows = server._active_connection_rows()

        self.assertEqual(len(rows), 1, "the vanished board should be skipped, not raised on")
        self.assertEqual(rows[0].probe_uid, "683710208")


if __name__ == "__main__":
    unittest.main()
