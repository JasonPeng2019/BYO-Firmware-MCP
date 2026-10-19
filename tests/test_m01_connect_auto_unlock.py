"""M01-S-1: opening a session must never mass-erase a protected target.

These tests drive the real adapter entry points with only the physical probe
replaced. Each session's options are merged by a real pyOCD ``Session`` (so the
project ``pyocd.yaml`` written here, which turns ``auto_unlock`` back on, really
competes with the adapter's options), and ``open()`` runs pyOCD's own Nordic and
Kinetis protection init tasks against a protected part that records any erase.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from pyocd.core.session import Session  # type: ignore[import-untyped]
from pyocd.coresight.coresight_target import CoreSightTarget  # type: ignore[import-untyped]
from pyocd.target.family.target_kinetis import Kinetis  # type: ignore[import-untyped]
from pyocd.target.family.target_nRF52 import NRF52  # type: ignore[import-untyped]

from pyocd_debug_mcp.adapters import provider_worker_runtime, swd_process, swd_pyocd
from pyocd_debug_mcp.adapters.swd_pyocd import PyOCDSWDInterface
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.firmstore.reports import ReportPaths
from pyocd_debug_mcp.monitor.classify import error_code
from pyocd_debug_mcp.setup_flow.validate import (
    BoardValidator,
    SafetyMapSnapshot,
    ValidationBackend,
    ValidationHooks,
    ValidationInventory,
    ValidationProbe,
    ValidationRequest,
)
from pyocd_debug_mcp.target_errors import LockedTargetError
from pyocd_debug_mcp.timeouts import ServerTimeoutConfig

NRF52_UICR_APPROTECT = 0x10001208


def board(family: str = "cmsisdap") -> BoardConfig:
    return BoardConfig(
        board_id="protected",
        display_name="Protected board",
        mcu_family="fake",
        probe_family=family,
        pyocd_target="fake_target",
        probe_type=family,
        probe_hint_terms=("fake",),
        serial_hint_terms=("fake",),
        test_addr=0,
    )


class RecordingProbe:
    def __init__(self, unique_id: str) -> None:
        self.unique_id = unique_id
        self.description = "Fake probe"
        self.reset_values: list[bool] = []

    def assert_reset(self, asserted: bool) -> None:
        self.reset_values.append(asserted)


class ProtectablePart:
    """The target attributes pyOCD's real family init tasks read and call."""

    def __init__(self, session: Any, probe: RecordingProbe, *, locked: bool) -> None:
        self.session = session
        self.part_number = "nRF52840"
        self.hardened_approtect = True
        self.locked = locked
        self.erases = 0
        self.uicr_writes: list[tuple[int, int]] = []
        self.cores: dict[int, object] = {}
        self.dp = SimpleNamespace(aps={0: object(), 1: object()}, assert_reset=probe.assert_reset)
        self.aps = self.dp.aps
        self.mdm_ap = SimpleNamespace(write_reg=lambda *_args: None)
        self._discoverer = SimpleNamespace(_create_1_ap=lambda _ap: None)
        self._force_halt_on_connect = False

    def is_locked(self) -> bool:
        return self.locked

    def mass_erase(self) -> bool:
        self.erases += 1
        self.locked = False
        return True

    def write_uicr(self, address: int, value: int) -> None:
        self.uicr_writes.append((address, value))

    def halt(self) -> None:
        pass

    @staticmethod
    def get_state() -> object:
        return SimpleNamespace(name="HALTED")


class ProbeSession:
    """Stands in for the probe-bound pyOCD session ConnectHelper returns."""

    def __init__(
        self,
        options: dict[str, object] | None,
        *,
        family: type,
        locked: bool,
        unique_id: str = "probe-1",
        open_error: Exception | None = None,
    ) -> None:
        # Real pyOCD option layering: explicit options, then project pyocd.yaml, then defaults.
        self.pyocd = Session(None, auto_open=False, options=options)
        self.probe = RecordingProbe(unique_id)
        self.target = ProtectablePart(self.pyocd, self.probe, locked=locked)
        self.board = SimpleNamespace(name="Protected board")
        self.family = family
        self.open_error = open_error
        self.close_calls = 0

    @property
    def options(self) -> Any:
        return self.pyocd.options

    def open(self) -> None:
        if self.open_error is not None:
            raise self.open_error
        part = self.target
        # pyOCD order: check_flash_security, create_cores, persist_unlock, check_for_cores.
        self.family.check_flash_security(part)
        if not part.locked:
            part.cores = {0: object()}
        persist_unlock = getattr(self.family, "persist_unlock", None)
        if persist_unlock is not None:
            persist_unlock(part)
        CoreSightTarget.check_for_cores(cast(Any, part))

    def close(self) -> None:
        self.close_calls += 1


class ConnectAutoUnlockTests(unittest.TestCase):
    def setUp(self) -> None:
        project = tempfile.TemporaryDirectory()
        self.addCleanup(project.cleanup)
        # pyOCD's Session switches into its project directory; restore before cleanup.
        self.addCleanup(os.chdir, os.getcwd())
        self.project_config = Path(project.name) / "pyocd.yaml"
        self.project_config.write_text("auto_unlock: true\n", encoding="utf-8")
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ["PYOCD_PROJECT_DIR"] = project.name
        os.environ.pop("PYOCD_TARGET", None)
        os.environ.pop("PYOCD_PROBE_UID", None)
        self.sessions: list[ProbeSession] = []
        self.chosen_options: list[dict[str, object] | None] = []

    def probe_sessions(self, *sessions: dict[str, Any]) -> Any:
        """Patch only the physical probe choice; everything above it is real."""

        pending = list(sessions)

        def choose(**kwargs: Any) -> ProbeSession:
            self.chosen_options.append(kwargs["options"])
            session = ProbeSession(kwargs["options"], **pending.pop(0))
            self.sessions.append(session)
            return session

        return patch.object(
            swd_pyocd.ConnectHelper, "session_with_chosen_probe", side_effect=choose
        )

    def assert_refused_without_erase(self, error: LockedTargetError, session: ProbeSession) -> None:
        self.assertIs(session.options.get("auto_unlock"), False)
        self.assertEqual(session.target.erases, 0)
        self.assertEqual(session.target.uicr_writes, [])
        self.assertTrue(session.target.locked)
        self.assertEqual(session.close_calls, 1)
        message = str(error)
        self.assertIn("not automatically unlocking", message)
        self.assertIn("no session was opened", message)
        self.assertIn("not erased or unlocked", message)
        self.assertIn("target_unlock_raw need a connected session", message)
        self.assertEqual(error_code(error), "target/locked")

    def test_project_config_enables_auto_unlock_without_the_adapter_options(self) -> None:
        self.assertIs(Session(None, auto_open=False, options={}).options.get("auto_unlock"), True)
        options = swd_pyocd.build_session_options(None, None)
        self.assertIs(
            Session(None, auto_open=False, options=options).options.get("auto_unlock"), False
        )

    def test_open_refuses_protected_nordic_target_without_erasing(self) -> None:
        with (
            self.probe_sessions({"family": NRF52, "locked": True}),
            patch.object(swd_pyocd, "verified_pack_for_target", return_value=None),
            self.assertLogs("pyocd", "WARNING") as logs,
            self.assertRaises(LockedTargetError) as raised,
        ):
            PyOCDSWDInterface().open(board=board(), unique_id="probe-1", target=None)

        self.assertIn(
            "nRF52840 APPROTECT enabled: not automatically unlocking", "\n".join(logs.output)
        )
        self.assert_refused_without_erase(raised.exception, self.sessions[0])

    def test_open_refuses_even_when_pyocd_would_finish_opening_after_declining(self) -> None:
        self.project_config.write_text(
            "auto_unlock: true\nallow_no_cores: true\n", encoding="utf-8"
        )
        with (
            self.probe_sessions({"family": NRF52, "locked": True}),
            patch.object(swd_pyocd, "verified_pack_for_target", return_value=None),
            self.assertLogs("pyocd", "WARNING"),
            self.assertRaises(LockedTargetError) as raised,
        ):
            PyOCDSWDInterface().open(board=board(), unique_id="probe-1", target=None)

        self.assertIs(self.sessions[0].options.get("allow_no_cores"), True)
        self.assert_refused_without_erase(raised.exception, self.sessions[0])

    def test_open_with_no_board_refuses_protected_target(self) -> None:
        with (
            self.probe_sessions({"family": NRF52, "locked": True}),
            self.assertLogs("pyocd", "WARNING"),
            self.assertRaises(LockedTargetError) as raised,
        ):
            PyOCDSWDInterface().open(board=None, unique_id=None, target=None)

        self.assert_refused_without_erase(raised.exception, self.sessions[0])

    def test_connect_under_reset_refuses_protected_kinetis_target_and_releases_reset(self) -> None:
        with (
            self.probe_sessions({"family": Kinetis, "locked": True}),
            patch.object(swd_pyocd, "verified_pack_for_target", return_value=None),
            self.assertLogs("pyocd", "WARNING") as logs,
            self.assertRaises(LockedTargetError) as raised,
        ):
            PyOCDSWDInterface().connect_under_reset(board=board(), unique_id="probe-1", target=None)

        session = self.sessions[0]
        self.assertEqual(session.options.get("connect_mode"), "under-reset")
        self.assertIn("in secure state: not automatically unlocking", "\n".join(logs.output))
        self.assertEqual(session.probe.reset_values, [True, False])
        self.assert_refused_without_erase(raised.exception, session)

    def test_jlink_uidless_retry_session_also_refuses(self) -> None:
        serial_miss = RuntimeError("No emulator with serial number 123 found")
        with (
            self.probe_sessions(
                {"family": NRF52, "locked": True, "unique_id": "123", "open_error": serial_miss},
                {"family": NRF52, "locked": True, "unique_id": "123"},
            ),
            patch.object(swd_pyocd, "verified_pack_for_target", return_value=None),
            patch.object(
                swd_pyocd, "_single_matching_probe_visible_for_board_family", return_value=True
            ),
            self.assertLogs("pyocd", "WARNING"),
            self.assertRaises(LockedTargetError) as raised,
        ):
            PyOCDSWDInterface().open(board=board("jlink"), unique_id="123", target=None)

        self.assertEqual(len(self.sessions), 2)
        self.assertEqual(self.sessions[0].close_calls, 1)
        self.assert_refused_without_erase(raised.exception, self.sessions[1])

    def test_worker_open_reports_the_locked_target_kind(self) -> None:
        arguments = {
            "board": None,
            "unique_id": None,
            "target": None,
            "server_timeouts": ServerTimeoutConfig().to_record(),
        }
        with (
            self.probe_sessions({"family": NRF52, "locked": True}),
            self.assertLogs("pyocd", "WARNING"),
            self.assertRaises(LockedTargetError) as raised,
        ):
            provider_worker_runtime._dispatch(PyOCDSWDInterface(), None, "open", arguments)

        kind = provider_worker_runtime._error_kind(raised.exception)
        self.assertEqual(kind, "locked_target")
        self.assertIs(swd_process._ERROR_TYPES[kind], LockedTargetError)
        self.assert_refused_without_erase(raised.exception, self.sessions[0])

    def test_validation_reports_target_locked_without_erasing(self) -> None:
        profile = SimpleNamespace(
            board_id="protected",
            board=board("jlink"),
            mcu_part_number="nRF52840",
            source_path=Path("missing-profile.json"),
            device_support=None,
        )
        probe = ValidationProbe("session:runtime-1", "Fake probe", "jlink", None)
        adapter = PyOCDSWDInterface()
        validator = BoardValidator(
            cast(Any, SimpleNamespace(load=Mock(return_value=profile))),
            Mock(),
            ValidationBackend(
                inventory=lambda: ValidationInventory(probes=(probe,)),
                target_supported=lambda _target: True,
                connect=lambda candidate, chosen, _timeout: adapter.open(
                    board=candidate.board, unique_id=chosen.usb_serial, target=None
                ),
                read_memory=Mock(side_effect=AssertionError("a refused connect cannot read")),
                capture_serial=Mock(),
                close=Mock(),
            ),
            hooks=ValidationHooks(
                load_safety_map=lambda _profile: SafetyMapSnapshot(True, True, "map-digest"),
                stamp_session=Mock(return_value=True),
                record_mismatch=lambda *_args: False,
            ),
        )
        reports = ReportPaths(Path("report.json"), Path("events.jsonl"))

        with (
            self.probe_sessions({"family": NRF52, "locked": True}),
            patch.object(swd_pyocd, "verified_pack_for_target", return_value=None),
            patch.object(validator, "_write_report", return_value=reports),
            self.assertLogs("pyocd", "WARNING"),
        ):
            result = validator.validate(ValidationRequest("protected", "session:runtime-1"))

        self.assertEqual(
            (result.status, result.code), ("validation_blocked", "validation/target-locked")
        )
        self.assertIn("validation did not erase or unlock it", result.agent_prompt)
        self.assertEqual(self.sessions[0].target.erases, 0)
        self.assertEqual(self.sessions[0].close_calls, 1)

    def test_unlocked_target_still_connects_without_any_nonvolatile_write(self) -> None:
        with (
            self.probe_sessions(
                {"family": NRF52, "locked": False},
                {"family": Kinetis, "locked": False},
            ),
            patch.object(swd_pyocd, "verified_pack_for_target", return_value=None),
        ):
            handle = PyOCDSWDInterface().open(board=board(), unique_id="probe-1", target=None)
            reset_handle = PyOCDSWDInterface().connect_under_reset(
                board=board(), unique_id="probe-1", target=None
            )

        self.assertIs(handle.session, self.sessions[0])
        self.assertIs(reset_handle.session, self.sessions[1])
        for session in self.sessions:
            self.assertEqual(session.target.erases, 0)
            self.assertEqual(session.target.uicr_writes, [])
            self.assertEqual(session.close_calls, 0)

    def test_control_without_the_shared_option_the_same_open_would_erase(self) -> None:
        """Proves the doubles detect pyOCD's implicit erase, so the refusals above are not vacuous."""

        real_build = swd_pyocd.build_session_options

        def pre_repair_options(*args: Any, **kwargs: Any) -> dict[str, object]:
            options = dict(real_build(*args, **kwargs))
            del options["auto_unlock"]
            return options

        with (
            self.probe_sessions({"family": NRF52, "locked": True}),
            patch.object(swd_pyocd, "build_session_options", side_effect=pre_repair_options),
            patch.object(swd_pyocd, "verified_pack_for_target", return_value=None),
            self.assertLogs("pyocd", "WARNING") as logs,
        ):
            PyOCDSWDInterface().open(board=board(), unique_id="probe-1", target=None)

        part = self.sessions[0].target
        self.assertIn("will try to unlock via mass erase", "\n".join(logs.output))
        self.assertEqual(part.erases, 1)
        self.assertEqual(part.uicr_writes, [(NRF52_UICR_APPROTECT, 0x5A)])


if __name__ == "__main__":
    unittest.main()
