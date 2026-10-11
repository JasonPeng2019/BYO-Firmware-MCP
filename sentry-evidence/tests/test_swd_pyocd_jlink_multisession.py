from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pyocd_debug_mcp.adapters import swd_pyocd
from pyocd_debug_mcp.adapters.swd_pyocd import PyOCDSWDInterface
from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.target_errors import ResetLineUnavailableError, TargetConnectionError


def board(family: str = "jlink") -> BoardConfig:
    return BoardConfig(
        board_id="fake",
        display_name="Fake board",
        mcu_family="fake",
        probe_family=family,
        pyocd_target="fake_target",
        probe_type=family,
        probe_hint_terms=("fake",),
        serial_hint_terms=("fake",),
        test_addr=0,
    )


class FakeTarget:
    def __init__(self) -> None:
        self.part_number = "fake_target"
        self.halt_calls = 0

    def halt(self) -> None:
        self.halt_calls += 1

    @staticmethod
    def get_state() -> object:
        return SimpleNamespace(name="HALTED")


class FakeProbe:
    def __init__(self, *, reset_supported: bool = True) -> None:
        self.unique_id = "probe-1"
        self.description = "Fake J-Link"
        self._link = object()
        self.reset_values: list[bool] = []
        if not reset_supported:
            self.assert_reset = None  # type: ignore[assignment]

    def assert_reset(self, asserted: bool) -> None:
        self.reset_values.append(asserted)


class FakeSession:
    def __init__(
        self, *, open_error: Exception | None = None, reset_supported: bool = True
    ) -> None:
        self.probe = FakeProbe(reset_supported=reset_supported)
        self.target = FakeTarget()
        self.board = SimpleNamespace(name="Fake target board")
        self.open_error = open_error
        self.open_calls = 0
        self.close_calls = 0

    def open(self) -> None:
        self.open_calls += 1
        if self.open_error is not None:
            raise self.open_error

    def close(self) -> None:
        self.close_calls += 1


class JLinkOneSessionWorkerTests(unittest.TestCase):
    def test_cli_command_uses_null_stdin_and_preserves_owned_runner_contract(self) -> None:
        completed = SimpleNamespace(returncode=3, stdout="listed", stderr="diagnostic")
        with patch.object(swd_pyocd, "run_owned", return_value=completed) as run_owned:
            result = swd_pyocd._run_cmd(
                ["pyocd", "list", "--probes"],
                timeout_seconds=6.5,
            )

        self.assertEqual(result, (3, "listed", "diagnostic"))
        run_owned.assert_called_once_with(
            ["pyocd", "list", "--probes"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=6.5,
        )

    def test_session_options_are_headless_only_for_jlink(self) -> None:
        jlink_options = swd_pyocd.build_session_options(board("jlink"), target=None)
        cmsis_options = swd_pyocd.build_session_options(board("cmsisdap"), target=None)

        self.assertIsNotNone(jlink_options)
        self.assertIsNotNone(cmsis_options)
        assert jlink_options is not None and cmsis_options is not None
        self.assertIs(jlink_options["jlink.non_interactive"], True)
        self.assertNotIn("jlink.non_interactive", cmsis_options)

    def test_open_uses_pyocd_selected_provider_without_temporary_dll_replacement(self) -> None:
        session = FakeSession()
        original_link = session.probe._link
        interface = PyOCDSWDInterface()

        with (
            patch.object(interface, "_choose_session", return_value=session),
            patch.object(interface, "_verify_session_pack_source", return_value=None),
            patch.object(swd_pyocd, "verified_pack_for_target", return_value=None),
        ):
            handle = interface.open(
                board=board(),
                unique_id=session.probe.unique_id,
                target="fake_target",
                operation_timeout_seconds=1.0,
            )

        self.assertIs(session.probe._link, original_link)
        self.assertEqual(session.open_calls, 1)
        interface.close(handle)
        self.assertEqual(session.close_calls, 1)

    def test_failed_open_asks_pyocd_to_close_without_manual_provider_finalization(self) -> None:
        session = FakeSession(open_error=RuntimeError("open failed"))
        interface = PyOCDSWDInterface()

        with (
            patch.object(interface, "_choose_session", return_value=session),
            patch.object(interface, "_verify_session_pack_source", return_value=None),
            patch.object(swd_pyocd, "verified_pack_for_target", return_value=None),
            self.assertRaisesRegex(TargetConnectionError, "open failed"),
        ):
            interface.open(
                board=board(),
                unique_id=session.probe.unique_id,
                target="fake_target",
            )

        self.assertEqual(session.close_calls, 1)

    def test_connect_under_reset_and_release_reset_use_the_selected_probe(self) -> None:
        session = FakeSession()
        interface = PyOCDSWDInterface()

        with (
            patch.object(interface, "_choose_session", return_value=session),
            patch.object(interface, "_verify_session_pack_source", return_value=None),
            patch.object(swd_pyocd, "verified_pack_for_target", return_value=None),
        ):
            handle = interface.connect_under_reset(
                board=board(),
                unique_id=session.probe.unique_id,
                target="fake_target",
                operation_timeout_seconds=1.0,
            )
            interface.release_reset(handle)

        self.assertEqual(session.target.halt_calls, 1)
        self.assertEqual(session.probe.reset_values, [False])
        interface.close(handle)

    def test_release_reset_fails_honestly_when_probe_has_no_control(self) -> None:
        session = FakeSession(reset_supported=False)
        handle = swd_pyocd.TargetSessionHandle(
            session=session,
            board=board(),
            probe_uid=session.probe.unique_id,
            route_used=swd_pyocd.ROUTE_PYOCD_NATIVE,
            target_override="fake_target",
        )

        with self.assertRaises(ResetLineUnavailableError):
            PyOCDSWDInterface().release_reset(handle)

    def test_obsolete_jlink_allocator_is_absent(self) -> None:
        source = Path(swd_pyocd.__file__).read_text(encoding="utf-8")
        for obsolete in ("use_tmpcpy", "_dispose_jlink_provider", "_finalize", "Library.unload"):
            self.assertNotIn(obsolete, source)


if __name__ == "__main__":
    unittest.main()
