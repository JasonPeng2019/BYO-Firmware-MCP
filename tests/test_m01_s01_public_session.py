"""Independent R01 connect proof at the *real* adapter / simulated wire boundary.

Public dispatch and target_control are real; the installed PyOCDSWDInterface is
installed in-process in place of its production process proxy. Session option
loading and NRF52 security/erase/persist decisions are real. Only
probe selection, session lifecycle and register/core discovery I/O are simulated.
This is neither a live worker transport test nor evidence about a physical board.
"""

from __future__ import annotations

import contextlib
from dataclasses import asdict
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import patch

from pyocd.core.session import Session
from pyocd.coresight.coresight_target import CoreSightTarget
from pyocd.probe.jlink_probe import JLinkProbe
from pyocd.target.family import target_nRF52 as nordic

from pyocd_debug_mcp.adapters import provider_worker_runtime as worker
from pyocd_debug_mcp.adapters import swd_pyocd as adapter
from pyocd_debug_mcp.adapters.swd_process import _ERROR_TYPES
from pyocd_debug_mcp.services import target_control
from pyocd_debug_mcp.capabilities.manual_permissions import ManualPermissionRepository
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp import probe_inventory
from pyocd_debug_mcp.target_errors import LockedTargetError
from pyocd_debug_mcp.timeouts import ServerTimeoutConfig

from tests.tiered_acceptance_support import (
    confirmed_lite_policy,
    isolated_project,
    known_good_full_policy,
    public_call,
    public_text,
    tiered_test_server,
)


BOARD = "legacy_full"
UID = "701234567"
RAM = 0x20000000
VALUE = 0xC0DEC0DE


class Wire:
    """Register transport: records attempts before changing simulated protection."""

    def __init__(self, journal: list, locked: bool) -> None:
        self.journal = journal
        self.locked = locked
        self.before_erase = lambda: None

    def read_reg(self, address: int) -> int:
        if address == nordic.CTRL_AP_APPROTECTSTATUS:
            value = (
                nordic.CTRL_AP_APPROTECTSTATUS_ENABLED
                if self.locked
                else nordic.CTRL_AP_APPROTECTSTATUS_DISABLED
            )
        elif address == nordic.CTRL_AP_ERASEALLSTATUS:
            value = nordic.CTRL_AP_ERASEALLSTATUS_READY
        else:
            raise AssertionError(f"unexpected CTRL-AP read {address:#x}")
        self.journal.append(("ctrl-read", address, value))
        return value

    def write_reg(self, address: int, value: int) -> None:
        self.journal.append(("ctrl-write", address, value))
        if address == nordic.CTRL_AP_ERASEALL and value == nordic.CTRL_AP_ERASEALL_ERASE:
            self.before_erase()
            self.locked = False


class BoundaryTarget(nordic.NRF52):
    """Real NRF52 methods with only register/core I/O and construction replaced."""

    def __init__(self, session: Any, wire: Wire) -> None:
        # Avoid constructing a DebugPort on the host: all wire I/O is owned here.
        self._session = session
        self.ctrl_ap = wire
        self.part_number = "nRF52840"
        self.hardened_approtect = True
        self._cores = {}
        self._discoverer = cast(
            Any,
            SimpleNamespace(
                _create_1_ap=lambda number: wire.journal.append(("recreate-ap", number))
            ),
        )
        self.state = "HALTED"

    def write_uicr(self, addr: int, value: int) -> None:
        self.ctrl_ap.journal.append(("uicr-write", addr, value))

    def read_memory(self, addr: int, transfer_size: int = 32, now: bool = True) -> Any:
        self.ctrl_ap.journal.append(("memory-read", addr, transfer_size))
        if self.ctrl_ap.locked:
            raise AssertionError("RAM access attempted through a locked AHB-AP")
        if (addr, transfer_size) != (RAM, 32):
            raise AssertionError("unexpected read")
        return VALUE if now else lambda: VALUE

    def write_memory(self, addr: int, data: int, transfer_size: int = 32) -> None:
        self.ctrl_ap.journal.append(("memory-write", addr, data, transfer_size))

    def halt(self) -> None:
        self.ctrl_ap.journal.append(("halt",))
        self.state = "HALTED"

    def get_state(self) -> Any:
        return SimpleNamespace(name=self.state)


class BoundaryProbe(JLinkProbe):
    """Recognizable registered provider type without constructing a native driver."""

    def __init__(self, journal: list) -> None:
        self.journal = journal
        self.boundary_board = SimpleNamespace(name="unrecognized board metadata", target=None)

    @property
    def unique_id(self) -> str:
        return UID

    @property
    def description(self) -> str:
        return "J-Link register boundary"

    @property
    def associated_board_info(self) -> None:
        return None

    def create_associated_board(self) -> Any:
        return self.boundary_board

    def assert_reset(self, asserted: bool) -> None:
        self.journal.append(("reset", asserted))


class BoundarySession:
    """Real Session configuration; selected init tasks with core I/O simulated.

    A locked AHB-AP yields no cores at the discovery boundary. The subsequent
    exception (or allow_no_cores return) is produced by real check_for_cores,
    not an invented pyOCD exception. Full DP discovery is not executed.
    """

    def __init__(self, options: dict, *, locked: bool, open_error: bool = False) -> None:
        self.journal: list = []
        self.probe = BoundaryProbe(self.journal)
        self.native = Session(self.probe, auto_open=False, options=options)
        self.options = self.native.options
        self.wire = Wire(self.journal, locked)
        self.target = BoundaryTarget(self, self.wire)
        self.board = self.probe.boundary_board
        self.board.target = self.target
        self.open_error = open_error
        self.closed = False

    def open(self) -> None:
        self.journal.append(("open", self.options.get("auto_unlock")))
        if self.open_error:
            raise RuntimeError("No emulator with serial number is connected")
        under_reset = self.options.get("connect_mode") == "under-reset"
        if under_reset:
            self.probe.assert_reset(True)
        # These three decisions and the register erase algorithm are installed pyOCD.
        self.target.check_flash_security()
        self.target._cores = {} if self.wire.locked else {0: cast(Any, object())}
        self.journal.append(("discovered-cores", len(self.target.cores)))
        self.target.persist_unlock()
        self.target.check_for_cores()
        if under_reset:
            self.probe.assert_reset(False)
        self.journal.append(("open-returned",))

    def close(self) -> None:
        self.journal.append(("close",))
        self.closed = True


def writes(session: BoundarySession) -> list:
    return [
        event
        for event in session.journal
        if event[0] in {"ctrl-write", "uicr-write", "memory-write"}
    ]


class PublicSessionProof(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(
        self,
        *,
        configured: bool = False,
        locked: bool = True,
        allow_no_cores: bool = False,
        per_probe: bool = False,
        retry: bool = False,
    ):
        previous_cwd = Path.cwd()
        logger = logging.getLogger("pyocd")
        handlers = tuple(logger.handlers)
        with isolated_project() as root, patch.dict(os.environ, {"PYOCD_PROJECT_DIR": str(root)}):
            config = (
                "auto_unlock: false\nprobes:\n  '701234':\n    auto_unlock: true\n"
                if per_probe
                else "auto_unlock: true\n"
            )
            (root / "pyocd.yaml").write_text(
                config + f"allow_no_cores: {str(allow_no_cores).lower()}\n", encoding="utf-8"
            )
            sessions: list[BoundarySession] = []
            choices: list = []

            def choose(**kwargs: Any) -> BoundarySession:
                choices.append(kwargs)
                result = BoundarySession(
                    kwargs["options"], locked=locked, open_error=retry and not sessions
                )
                sessions.append(result)
                return result

            try:
                with tiered_test_server(root) as server:
                    policies = CapabilityPolicyRepository(root)
                    if configured:
                        profile, _ = known_good_full_policy()
                        profile["mcu_family"] = "unrecognized-family"
                        policies.commit(
                            BOARD,
                            Tier.SETUP_LITE,
                            profile_snapshot=profile,
                            map_snapshot=confirmed_lite_policy(BOARD),
                            evidence={"confirmed": "independent R01 host fixture"},
                        )
                    else:
                        policies.resolve(BOARD)
                    manual = ManualPermissionRepository(root, server.server_run.run_id)
                    manual.reset_startup()
                    server.configure_tiered_test_seams(
                        backend=adapter.PyOCDSWDInterface(),
                        policy_repository=policies,
                        manual_permission_repository=manual,
                    )
                    with (
                        # M11 resolves explicit UIDs against unified inventory
                        # before reaching the same real Session/security tasks.
                        patch.object(
                            server,
                            "list_connected_probes_detailed",
                            return_value=probe_inventory.NativeProbeListing(
                                probes=(probe_inventory.ProbeInfo(
                                    UID, "J-Link register boundary", "fixture", family="jlink"
                                ),),
                                command=(),
                                exit_code=0,
                                timed_out=False,
                                stdout_summary="fixture probe inventory",
                                stderr_summary="",
                            ),
                        ),
                        patch.object(
                            probe_inventory,
                            "list_connected_probes_cli",
                            return_value=[
                                probe_inventory.ProbeInfo(
                                    UID, "J-Link register boundary", "fixture", family="jlink"
                                ),
                            ],
                        ),
                        patch.object(
                            adapter.ConnectHelper, "session_with_chosen_probe", side_effect=choose
                        ),
                        patch.object(
                            adapter.ConnectHelper,
                            "get_all_connected_probes",
                            side_effect=lambda **_: [BoundaryProbe([])],
                        ),
                    ):
                        try:
                            yield SimpleNamespace(
                                server=server,
                                manual=manual,
                                sessions=sessions,
                                choices=choices,
                                root=root,
                                configured=configured,
                            )
                        finally:
                            if server.connection_manager.maybe_connection(BOARD) is not None:
                                public_text(server, "disconnect", {"board_id": BOARD})
            finally:
                os.chdir(previous_cwd)
                self.assertEqual(tuple(logger.handlers), handlers, "decline handler leaked")
                if journal_path := os.environ.get("M01_S01_JOURNAL"):
                    with Path(journal_path).open("a", encoding="utf-8") as stream:
                        stream.write(
                            json.dumps(
                                {
                                    "test": self.id(),
                                    "configured": configured,
                                    "locked": locked,
                                    "allow_no_cores": allow_no_cores,
                                    "per_probe": per_probe,
                                    "config": config,
                                    "choices": choices,
                                    "sessions": [session.journal for session in sessions],
                                }
                            )
                            + "\n"
                        )

    def connect(self, f: Any, route: str) -> str:
        arguments: dict[str, Any] = {"board_id": BOARD, "probe_uid": UID}
        if route == "connect" and f.configured:
            arguments.pop("probe_uid")
        elif route == "connect":
            arguments["target"] = "nrf52840"
        else:
            arguments["target_override"] = "nrf52840"
            fields = {
                "board_id": BOARD,
                "hypothesis": "Early firmware prevents ordinary attach.",
                "strategy": "Use physical reset attach without changing debug protection.",
                "hypothesis_made": True,
                "strategy_evaluated": True,
                "expected_fail_return": "A protected target refuses without erase.",
                "expected_success_return": "A usable halted session.",
                "max_calls": 1,
                "max_calls_buffer": 0,
                "action_parameters": {
                    key: arguments[key] for key in ("probe_uid", "target_override")
                },
                "user_permission": None,
            }
            public_text(f.server, "connect_under_reset-plan", dict.fromkeys(fields))
            fields.pop("user_permission")
            accepted = json.loads(public_text(f.server, "connect_under_reset-plan", fields))
            self.assertEqual(accepted["status"], "plan_accepted", accepted)
        return public_text(f.server, route, arguments)

    def refuse(self, f: Any, route: str) -> None:
        with self.assertRaises(Exception) as caught:
            self.connect(f, route)
        self.assertIn("debug access is protected", str(caught.exception))
        self.assertIn("no session was opened", str(caught.exception))
        self.assertIn("cannot recover it", str(caught.exception))
        causes: list[BaseException] = []
        current: BaseException | None = caught.exception
        while current is not None and current not in causes:
            causes.append(current)
            current = current.__cause__ or current.__context__
        self.assertTrue(any(isinstance(exc, LockedTargetError) for exc in causes), causes)
        self.assertIsNone(f.server.connection_manager.maybe_connection(BOARD))
        self.assertIsNone(f.server.gate_manager.snapshot(BOARD))
        self.assertEqual(f.manual.status("mass-erase").state, "locked")
        for session in f.sessions:
            self.assertEqual(writes(session), [], session.journal)
            self.assertTrue(session.wire.locked)
            self.assertEqual(session.journal.count(("close",)), 1)
        self.assertIn("Not connected.", public_text(f.server, "disconnect", {"board_id": BOARD}))

    def test_public_locked_connect_and_reset_refuse_without_any_write(self) -> None:
        for route, configured in (
            ("connect", False),
            ("connect", True),
            ("connect_under_reset", True),
        ):
            for per_probe in (False, True):
                for allow_no_cores in (False, True):
                    with (
                        self.subTest(
                            route=route,
                            configured=configured,
                            per_probe=per_probe,
                            allow_no_cores=allow_no_cores,
                        ),
                        self.fixture(
                            configured=configured,
                            per_probe=per_probe,
                            allow_no_cores=allow_no_cores,
                        ) as f,
                    ):
                        self.refuse(f, route)
                        self.assertEqual(len(f.sessions), 1)
                        session = f.sessions[0]
                        self.assertIn(("discovered-cores", 0), session.journal)
                        self.assertEqual(("open-returned",) in session.journal, allow_no_cores)
                        if route == "connect_under_reset":
                            self.assertIn(("reset", True), session.journal)
                            self.assertIn(("reset", False), session.journal)
                            self.assertLess(
                                session.journal.index(("reset", False)),
                                session.journal.index(("close",)),
                            )

    def test_unlocked_public_connect_read_disconnect_control(self) -> None:
        for route, configured in (
            ("connect", False),
            ("connect", True),
            ("connect_under_reset", True),
        ):
            with (
                self.subTest(route=route, configured=configured),
                self.fixture(
                    configured=configured,
                    locked=False,
                    per_probe=True,
                ) as f,
            ):
                self.assertIn("Connected", self.connect(f, route))
                session = f.sessions[0]
                self.assertFalse(session.closed)
                read = public_call(f.server, "read_memory_raw", {"board_id": BOARD, "address": RAM})
                self.assertEqual(read["status"], "ok", read)
                self.assertEqual(read["result"].lower(), f"0x{VALUE:08x}")
                public_text(f.server, "disconnect", {"board_id": BOARD})
                self.assertIsNone(f.server.connection_manager.maybe_connection(BOARD))
                self.assertEqual(session.journal.count(("close",)), 1)
                # ERASEALL is forbidden even in the sensitivity control. With the
                # historical default, unlocked hardened parts may persist UICR.
                self.assertFalse(
                    any(e[:2] == ("ctrl-write", nordic.CTRL_AP_ERASEALL) for e in session.journal)
                )

    def test_native_project_global_and_probe_layers_are_really_loaded(self) -> None:
        for global_value, probe_value in ((False, True), (True, False)):
            with self.subTest(global_value=global_value), isolated_project() as root:
                previous_cwd = Path.cwd()
                try:
                    (root / "pyocd.yaml").write_text(
                        f"auto_unlock: {str(global_value).lower()}\nprobes:\n"
                        f"  '701234':\n    auto_unlock: {str(probe_value).lower()}\n",
                        encoding="utf-8",
                    )
                    with patch.dict(os.environ, {"PYOCD_PROJECT_DIR": str(root)}):
                        without_probe = Session(None, auto_open=False)
                        with_probe = Session(BoundaryProbe([]), auto_open=False)
                    self.assertIs(without_probe.options.get("auto_unlock"), global_value)
                    self.assertIs(with_probe.options.get("auto_unlock"), probe_value)
                    self.assertEqual(Path(with_probe.project_dir), root)
                finally:
                    os.chdir(previous_cwd)

    def test_unlocked_connect_also_makes_no_security_or_memory_writes(self) -> None:
        for route, configured in (
            ("connect", False),
            ("connect", True),
            ("connect_under_reset", True),
        ):
            with (
                self.subTest(route=route, configured=configured),
                self.fixture(
                    configured=configured,
                    locked=False,
                    per_probe=True,
                ) as f,
            ):
                self.connect(f, route)
                self.assertEqual(writes(f.sessions[0]), [], f.sessions[0].journal)
                public_text(f.server, "disconnect", {"board_id": BOARD})

    def test_public_uid_retry_closes_first_and_refuses_protected_retry(self) -> None:
        with self.fixture(configured=True, per_probe=True, retry=True) as f:
            self.refuse(f, "connect")
            self.assertEqual(len(f.sessions), 2)
            self.assertEqual([choice["unique_id"] for choice in f.choices], [UID, None])
            self.assertEqual(f.sessions[0].journal, [("open", False), ("close",)])
            self.assertIn(("discovered-cores", 0), f.sessions[1].journal)

    def test_production_worker_dispatch_uses_same_real_adapter_boundary(self) -> None:
        # Real worker deserialization/forwarding, in-process: no worker PID or
        # JSON pipe transport is claimed by this test.
        for operation in ("open", "connect_under_reset"):
            with self.subTest(operation=operation), self.fixture(configured=True) as f:
                board = f.server._profile_repository.from_snapshot(
                    BOARD, f.server._tier_router.state_for(BOARD).profile_snapshot
                ).board
                arguments: dict[str, Any] = dict.fromkeys(
                    (
                        "protocol",
                        "connect_mode",
                        "pack_path",
                        "pack_sha256",
                        "pdsc_device",
                        "frequency_hz",
                    )
                )
                arguments.update(
                    {
                        "board": json.loads(json.dumps(asdict(board), default=str)),
                        "unique_id": UID,
                        "target": "nrf52840",
                        "server_timeouts": ServerTimeoutConfig().to_record(),
                    }
                )
                worker._validate_arguments(operation, arguments)
                with self.assertRaises(LockedTargetError) as caught:
                    worker._dispatch(
                        adapter.PyOCDSWDInterface(),
                        None,
                        operation,
                        arguments,
                    )
                kind = worker._error_kind(caught.exception)
                self.assertIs(_ERROR_TYPES[kind], LockedTargetError)
                self.assertEqual(writes(f.sessions[0]), [])
                self.assertEqual(f.sessions[0].journal.count(("close",)), 1)

    def test_public_exact_grant_consumed_once_before_real_erase_then_finalized(self) -> None:
        with self.fixture(locked=False) as f:
            self.connect(f, "connect")
            session = f.sessions[0]
            arguments = {"board_id": BOARD, "recovery_mechanism": "backend_mass_erase"}
            refused = public_call(f.server, "target_unlock_raw", arguments)
            self.assertEqual(refused["status"], "refused", refused)
            self.assertFalse(refused["attempted"])
            self.assertEqual(writes(session), [])
            connection = f.server.connection_manager.connection_for(BOARD)
            f.server.gate_manager.stamp_validation(
                board_id=BOARD,
                connection_id=connection.connection_id,
                probe_identity=UID,
                observed_mcu="nRF52840",
                validation_run="independent-start-state",
                map_digest="fixture-only-no-physical-validation",
            )
            binding = refused["manual_grant"]
            f.manual.write_unlocked_grant(
                action="mass-erase",
                grant_id="host-exact-one-use",
                board_id=BOARD,
                policy_digest=binding["policy_digest"],
                binding_digest=binding["binding_digest"],
            )

            def before_erase() -> None:
                self.assertEqual(f.manual.status("mass-erase").state, "consumed")
                session.journal.append(("grant-observed-consumed",))

            session.wire.before_erase = before_erase
            attempted = public_call(f.server, "target_unlock_raw", arguments)
            self.assertEqual(attempted["status"], "ok", attempted)
            self.assertEqual(
                session.journal.count(
                    ("ctrl-write", nordic.CTRL_AP_ERASEALL, nordic.CTRL_AP_ERASEALL_ERASE)
                ),
                1,
            )
            self.assertEqual(f.manual.status("mass-erase").state, "consumed")
            self.assertIsNone(f.server.connection_manager.maybe_connection(BOARD))
            self.assertIsNone(f.server.gate_manager.snapshot(BOARD))
            self.assertEqual(session.journal.count(("close",)), 1)
            self.assertLess(
                session.journal.index(("grant-observed-consumed",)),
                session.journal.index(("close",)),
            )
            self.connect(f, "connect")
            replay = public_call(f.server, "target_unlock_raw", arguments)
            self.assertEqual(replay["status"], "refused", replay)
            self.assertFalse(replay["attempted"])
            self.assertEqual(writes(f.sessions[1]), [])
            self.assertFalse(f.sessions[1].closed)
            read = public_call(f.server, "read_memory_raw", {"board_id": BOARD, "address": RAM})
            self.assertEqual(read["status"], "ok")
            public_text(f.server, "disconnect", {"board_id": BOARD})


def import_identities() -> dict[str, Any]:
    objects: tuple[Any, ...] = (
        Session,
        nordic.NRF52,
        CoreSightTarget,
        adapter,
        worker,
        target_control,
        ManualPermissionRepository,
        nordic.NRF52.mass_erase,
        PublicSessionProof,
        tiered_test_server,
    )
    return {
        getattr(obj, "__name__", str(obj)): {
            "path": inspect.getfile(inspect.unwrap(obj)),
            "sha256": hashlib.sha256(
                Path(inspect.getfile(inspect.unwrap(obj))).read_bytes()
            ).hexdigest(),
        }
        for obj in objects
    }


if __name__ == "__main__":
    print(json.dumps(import_identities(), indent=2))
    unittest.main()
