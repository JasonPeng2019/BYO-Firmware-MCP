"""Connection safety against actual pyOCD options and Nordic init methods.

Every erase/protection observer below is a mock. No physical target is opened.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pyocd.core.session import Session
from pyocd.target.family.target_nRF52 import NRF52

from pyocd_debug_mcp.adapters import swd_pyocd
from pyocd_debug_mcp.target_errors import LockedTargetError


class ProbeFixture:
    unique_id = "123"
    is_open = False
    wire_protocol = None

    def __init__(self):
        self.assert_reset = Mock()
        self.target = SimpleNamespace(
            halt=Mock(), get_state=Mock(return_value=SimpleNamespace(name="HALTED"))
        )

    def create_associated_board(self):
        return SimpleNamespace(target=self.target)


def nordic_observers(session):
    return SimpleNamespace(
        session=session,
        part_number="fixture-only-nrf52840",
        hardened_approtect=True,
        is_locked=Mock(return_value=True),
        mass_erase=Mock(return_value=True),
        write_uicr=Mock(),
        _discoverer=SimpleNamespace(_create_1_ap=Mock()),
    )


class ConnectSafetyTests(unittest.TestCase):
    def make_session(self, **kwargs):
        session = Session(ProbeFixture(), auto_open=False, options=kwargs["options"])
        self.addCleanup(swd_pyocd._close_session, session)
        return session

    def test_actual_dependency_default_can_erase_and_persist_only_to_mock_observers(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                session = Session(
                    ProbeFixture(),
                    auto_open=False,
                    options={"project_dir": directory, "no_config": True, "user_script": None},
                )
                self.addCleanup(session.close)
                self.assertIs(session.options.get("auto_unlock"), True)
                vendor = nordic_observers(session)
                NRF52.check_flash_security(vendor)
                NRF52.persist_unlock(vendor)
                vendor.mass_erase.assert_called_once_with()
                vendor._discoverer._create_1_ap.assert_called_once()
                vendor.write_uicr.assert_called_once_with(0x10001208, 0x5A)
            finally:
                os.chdir(previous)

    def test_shared_boundary_overrides_default_and_requested_unlock_without_erasing(self):
        for options in (None, {}, {"auto_unlock": True}, {"auto_unlock": False}):
            with self.subTest(options=options):
                original = None if options is None else options.copy()
                with patch.object(
                    swd_pyocd.ConnectHelper,
                    "session_with_chosen_probe",
                    side_effect=self.make_session,
                ):
                    session = swd_pyocd.PyOCDSWDInterface._choose_session(
                        probe_uid="123", options=options
                    )
                self.assertIs(session.options.get("auto_unlock"), False)
                self.assertEqual(options, original)
                vendor = nordic_observers(session)
                NRF52.check_flash_security(vendor)
                NRF52.persist_unlock(vendor)
                vendor.is_locked.assert_called_once_with()
                vendor.mass_erase.assert_not_called()
                vendor._discoverer._create_1_ap.assert_not_called()
                vendor.write_uicr.assert_not_called()
                folder = Path(session.project_dir)
                swd_pyocd._close_session(session)
                self.assertFalse(folder.exists())

    def test_normal_and_uid_retry_and_under_reset_resolve_safe_actual_session_options(self):
        for route in ("normal", "retry", "under-reset"):
            with self.subTest(route=route):
                sessions = []
                requested_uids = []

                def choose(**kwargs):
                    session = self.make_session(**kwargs)
                    sessions.append(session)
                    requested_uids.append(kwargs["unique_id"])
                    session.open = Mock()
                    if route == "retry" and len(sessions) == 1:
                        session.open.side_effect = RuntimeError("no emulator with serial number")
                    return session

                interface = swd_pyocd.PyOCDSWDInterface()
                with (
                    patch.dict("os.environ", {"PYOCD_TARGET": ""}),
                    patch.object(
                        swd_pyocd.ConnectHelper, "session_with_chosen_probe", side_effect=choose
                    ),
                    patch.object(swd_pyocd, "_should_retry_without_uid", return_value=True),
                ):
                    if route == "under-reset":
                        handle = interface.connect_under_reset(
                            board=None, unique_id="000123", target=None
                        )
                        handle.session.target.halt.assert_called_once_with()
                        self.assertEqual(handle.session.options.get("connect_mode"), "under-reset")
                    else:
                        handle = interface.open(board=None, unique_id="000123", target=None)
                    interface.close(handle)
                self.assertEqual(
                    requested_uids, ["000123", None] if route == "retry" else ["000123"]
                )
                for session in sessions:
                    self.assertIs(session.options.get("auto_unlock"), False)
                    session.open.assert_called_once_with()
                    self.assertFalse(Path(session.project_dir).exists())

    def test_locked_open_refuses_with_typed_error_and_closes_isolated_session(self):
        sessions = []

        def choose(**kwargs):
            session = self.make_session(**kwargs)
            session.open = Mock(side_effect=RuntimeError("device locked"))
            sessions.append(session)
            return session

        with (
            patch.dict("os.environ", {"PYOCD_TARGET": ""}),
            patch.object(swd_pyocd.ConnectHelper, "session_with_chosen_probe", side_effect=choose),
        ):
            with self.assertRaisesRegex(LockedTargetError, "device locked"):
                swd_pyocd.PyOCDSWDInterface().open(board=None, unique_id="123", target=None)
        self.assertEqual(len(sessions), 1)
        self.assertIs(sessions[0].options.get("auto_unlock"), False)
        self.assertFalse(Path(sessions[0].project_dir).exists())

    def test_explicit_recovery_keeps_its_existing_backend_primitive(self):
        # Public one-time grant checks are exercised by tiered acceptance tests.
        # This adapter-level check proves auto-unlock=False does not rewrite the
        # explicit recovery operation into a connect-time side effect.
        session = SimpleNamespace(options={"auto_unlock": False})
        handle = SimpleNamespace(session=session)
        with patch.object(swd_pyocd, "FlashEraser") as eraser:
            swd_pyocd.PyOCDSWDInterface().recover(handle)
        eraser.assert_called_once_with(session, eraser.Mode.MASS)
        eraser.return_value.erase.assert_called_once_with()
        self.assertIs(session.options["auto_unlock"], False)


if __name__ == "__main__":
    unittest.main()
