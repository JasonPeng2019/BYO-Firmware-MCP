"""Host regressions for real pyOCD recovery results and public M14 finalization.

Installed Nordic and default SoC erase implementations run against controlled
register/flash I/O. No probe, serial port, hardware or provider worker is opened.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace
import io
import json
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch

from pyocd.core.exceptions import TransferError
from pyocd.core.soc_target import SoCTarget
from pyocd.target.family import target_nRF52 as nordic

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.adapters.swd_pyocd import PyOCDSWDInterface
from pyocd_debug_mcp.target_errors import (
    LockedTargetError,
    TargetConnectionError,
    TargetControlError,
)

from tests.m14_independent_support import (
    advertised_validation,
    approve,
    attempt,
    connect,
    disclose,
    grant,
    ordinary_full,
    safe_read,
    safe_value,
)


class OnePoll:
    """Take one actual status sample, then expire the installed polling loop."""

    def __init__(self, seconds: float) -> None:
        self.polls = 0

    def __enter__(self) -> OnePoll:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def check(self) -> bool:
        self.polls += 1
        return self.polls == 1


class NordicIO:
    def __init__(self, *, ready: bool = False, error: Exception | None = None) -> None:
        self.ready = ready
        self.error = error
        self.calls: list[tuple[Any, ...]] = []

    def write_reg(self, address: int, value: int) -> None:
        self.calls.append(("write", address, value))

    def read_reg(self, address: int) -> int:
        self.calls.append(("read", address))
        if self.error is not None:
            raise self.error
        return (
            nordic.CTRL_AP_ERASEALLSTATUS_READY
            if self.ready
            else nordic.CTRL_AP_ERASEALLSTATUS_BUSY
        )


class NordicBoundary:
    def __init__(self, **kwargs: Any) -> None:
        self.ctrl_ap = NordicIO(**kwargs)
        self.attempts = 0
        self.results: list[bool] = []

    def mass_erase(self) -> bool:
        self.attempts += 1
        print("controlled backend erase diagnostic")
        # Keep the installed destructive implementation; only its I/O and clock
        # are controlled. This exercises its real False return on timeout.
        with patch.object(nordic, "Timeout", OnePoll), patch.object(nordic, "sleep"):
            result = nordic.NRF52.mass_erase(cast(Any, self))
        self.results.append(result)
        return result


def handle_for(target: Any) -> TargetSessionHandle:
    return TargetSessionHandle(
        session=SimpleNamespace(target=target),
        board=None,
        probe_uid="host-only-recovery",
        route_used="controlled-io",
        target_override=None,
    )


class PyOCDRecoveryResultsTests(unittest.TestCase):
    def assert_one_erase(self, target: NordicBoundary) -> None:
        self.assertEqual(target.attempts, 1)
        self.assertEqual(
            target.ctrl_ap.calls.count(
                ("write", nordic.CTRL_AP_ERASEALL, nordic.CTRL_AP_ERASEALL_ERASE)
            ),
            1,
        )

    def test_installed_nordic_timeout_false_raises_without_retry_and_keeps_stdout_clean(
        self,
    ) -> None:
        target = NordicBoundary()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaisesRegex(TargetControlError, "mass erase reported failure"):
                PyOCDSWDInterface().recover(handle_for(target))
            print("protocol still usable")
        self.assertEqual(target.results, [False])
        self.assert_one_erase(target)
        self.assertFalse(
            any(c[:2] == ("write", nordic.CTRL_AP_RESET) for c in target.ctrl_ap.calls)
        )
        self.assertEqual(stdout.getvalue(), "protocol still usable\n")
        self.assertIn("controlled backend erase diagnostic", stderr.getvalue())

    def test_installed_nordic_true_success_runs_exactly_once(self) -> None:
        target = NordicBoundary(ready=True)
        self.assertIsNone(PyOCDSWDInterface().recover(handle_for(target)))
        self.assertEqual(target.results, [True])
        self.assert_one_erase(target)
        self.assertEqual(
            target.ctrl_ap.calls,
            [
                ("write", nordic.CTRL_AP_ERASEALL, nordic.CTRL_AP_ERASEALL_ERASE),
                ("read", nordic.CTRL_AP_ERASEALLSTATUS),
                ("write", nordic.CTRL_AP_RESET, nordic.CTRL_AP_RESET_RESET),
                ("write", nordic.CTRL_AP_RESET, nordic.CTRL_AP_RESET_NORESET),
                ("write", nordic.CTRL_AP_ERASEALL, nordic.CTRL_AP_ERASEALL_NOOPERATION),
            ],
        )

    def test_installed_default_soc_void_success_keeps_single_chip_erase(self) -> None:
        flash = Mock(is_erase_all_supported=True)
        region = SimpleNamespace(flash=flash)
        target = SimpleNamespace(
            selected_core=SimpleNamespace(node_name="core0"),
            memory_map=SimpleNamespace(iter_matching_regions=Mock(return_value=[region])),
            call_delegate=Mock(return_value=False),
        )
        handle = handle_for(target)
        target.session = handle.session
        # The real default SoC method returns None after the real CHIP wrapper
        # finishes. Only the flash algorithm I/O is mocked, not either method.
        target.mass_erase = Mock(side_effect=lambda: SoCTarget.mass_erase(cast(Any, target)))
        self.assertIsNone(PyOCDSWDInterface().recover(handle))
        target.mass_erase.assert_called_once_with()
        target.call_delegate.assert_called_once_with("mass_erase", target=target)
        flash.init.assert_called_once_with(flash.Operation.ERASE)
        flash.erase_all.assert_called_once_with()
        flash.cleanup.assert_called_once_with()

    def test_backend_exceptions_keep_typed_error_cause_one_attempt_and_output_routing(self) -> None:
        for error, expected in (
            (TransferError("controlled transport loss"), TargetConnectionError),
            (LockedTargetError("controlled locked target"), LockedTargetError),
        ):
            with self.subTest(error=type(error).__name__):
                target = NordicBoundary(error=error)
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with self.assertRaises(expected) as caught:
                        PyOCDSWDInterface().recover(handle_for(target))
                    print("protocol still usable")
                self.assertIs(caught.exception.__cause__, error)
                self.assertIn(str(error), str(caught.exception))
                self.assert_one_erase(target)
                self.assertEqual(stdout.getvalue(), "protocol still usable\n")
                self.assertIn("controlled backend erase diagnostic", stderr.getvalue())

    def assert_read_refused(self, f: Any) -> None:
        reads = [call for call in f.backend.calls if call[0] == "read"]
        try:
            response = safe_read(f)
        except Exception as exc:
            self.assertRegex(str(exc).lower(), "validat|connect|ready|locked")
        else:
            self.assertEqual(response["status"], "batch_failed", response)
        self.assertEqual([call for call in f.backend.calls if call[0] == "read"], reads)

    def test_public_full_false_consumes_grant_and_reports_failed_attempt(self) -> None:
        with ordinary_full(self) as f:
            proposal = disclose(f)
            grant(f, proposal)
            self.assertEqual(approve(f)["status"], "unlock_plan_approved")
            target = NordicBoundary()
            adapter = PyOCDSWDInterface()

            def real_recovery(handle: TargetSessionHandle) -> None:
                manual = f.root / ".agent-workspace/runtime/manual-permissions/mass-erase.json"
                state = json.loads(manual.read_text(encoding="utf-8"))
                self.assertEqual(state["state"], "consumed")
                self.assertTrue(state["claim_id"])
                self.assertIsNone(f.server.gate_manager.snapshot(f.board))
                # Preserve actual public metadata, grant and bindings. Only
                # the synthetic target I/O delegates to the real adapter.
                adapter.recover(replace(handle, session=SimpleNamespace(target=target)))

            f.backend.recover = real_recovery
            writer = f.server._report_writer
            with patch.object(
                writer, "create_target_unlock", wraps=writer.create_target_unlock
            ) as reports:
                with self.assertRaisesRegex(Exception, "mass erase reported failure") as caught:
                    attempt(f)
            self.assertNotIn("Target unlock completed", str(caught.exception))
            self.assertNotIn("performed mass erase", str(caught.exception))
            self.assertEqual(
                [call.args[1]["terminal_status"] for call in reports.call_args_list],
                ["unlock_failed_revalidation_required"],
            )
            self.assert_one_erase(target)
            self.assertEqual(target.results, [False])
            self.assertEqual(f.server._manual_permissions.status("mass-erase").state, "consumed")
            self.assertIsNone(f.server.gate_manager.snapshot(f.board))
            self.assertIsNone(f.server.connection_manager.maybe_connection(f.board))
            with self.assertRaises(Exception):
                attempt(f)
            self.assert_read_refused(f)
            self.assert_one_erase(target)
            connect(f)
            self.assert_read_refused(f)
            self.assertEqual(advertised_validation(f.flow, f.board)["status"], "validation_passed")
            self.assertEqual(safe_value(f), 0x6D143A72)
            disclose(f)
            with self.assertRaises(Exception):
                approve(f)
            self.assert_one_erase(target)


if __name__ == "__main__":
    unittest.main()
