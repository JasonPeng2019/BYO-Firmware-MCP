from __future__ import annotations

import contextlib
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from pyocd_debug_mcp.adapters import swd_pyocd
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.adapters.swd_pyocd import PyOCDSWDInterface
from pyocd_debug_mcp.target_errors import (
    TargetConnectionError,
    TargetControlError,
    TargetStateError,
)


def hardware_breakpoint(
    *,
    enabled: bool = True,
    comp_register_addr: int = 0xE0002008,
) -> SimpleNamespace:
    return SimpleNamespace(
        type=SimpleNamespace(name="HW"),
        enabled=enabled,
        comp_register_addr=comp_register_addr,
    )


def software_breakpoint(
    *,
    enabled: bool = True,
    original_instr: int = 0x1234,
) -> SimpleNamespace:
    return SimpleNamespace(
        type=SimpleNamespace(name="SW"),
        enabled=enabled,
        original_instr=original_instr,
    )


class FakeAccessPort:
    def __init__(self, target: FakeTarget) -> None:
        self.target = target

    def read_memory(self, address: int, width: int, *, now: bool) -> object:
        self.target.calls.append(("ap.read_memory", address))
        self.target.raw_read_widths.append((width, now))
        error = self.target.raw_read_errors.pop(0) if self.target.raw_read_errors else None
        if error is not None:
            raise error
        return self.target.raw_memory_values.get(address, self.target.raw_instruction)


class FakeTarget:
    def __init__(
        self,
        *,
        accepted: bool = True,
        initially_realized: object | None = None,
        realized_after_flush: object | None = None,
        manager_flush_errors: tuple[Exception | None, ...] = (),
        target_flush_errors: tuple[Exception | None, ...] = (),
        remove_errors: tuple[Exception | None, ...] = (),
        find_errors: tuple[Exception | None, ...] = (),
        remove_effective: bool = True,
        sw_restore_effective: bool = True,
        disable_hw_on_remove: bool = True,
        clear_hw_comparator_on_remove: bool = True,
        hw_disable_before_flush_error: bool = False,
        raw_instruction: object = 0xBE00,
        comparator_value: object = 1,
        raw_read_errors: tuple[Exception | None, ...] = (),
    ) -> None:
        self.accepted = accepted
        self.realized = initially_realized
        self.realized_after_flush = realized_after_flush
        self.manager_flush_errors = list(manager_flush_errors)
        self.target_flush_errors = list(target_flush_errors)
        self.remove_errors = list(remove_errors)
        self.find_errors = list(find_errors)
        self.remove_effective = remove_effective
        self.sw_restore_effective = sw_restore_effective
        self.disable_hw_on_remove = disable_hw_on_remove
        self.clear_hw_comparator_on_remove = clear_hw_comparator_on_remove
        self.hw_disable_before_flush_error = hw_disable_before_flush_error
        self.raw_instruction = raw_instruction
        self.raw_memory_values: dict[int, object] = {0xE0002008: comparator_value}
        self.raw_read_errors = list(raw_read_errors)
        self.raw_read_widths: list[tuple[int, bool]] = []
        self.pending_mutation: str | None = None
        self.calls: list[tuple[str, int | None]] = []
        self.selected_core_or_raise = SimpleNamespace(
            bp_manager=FakeBreakpointManager(self),
            ap=FakeAccessPort(self),
        )

    def set_breakpoint(self, address: int) -> bool:
        self.calls.append(("set_breakpoint", address))
        if self.accepted:
            self.pending_mutation = "install"
        return self.accepted

    def flush(self) -> None:
        self.calls.append(("flush", None))
        error = self.target_flush_errors.pop(0) if self.target_flush_errors else None
        if error is not None:
            raise error

    def find_breakpoint(self, address: int) -> object | None:
        self.calls.append(("find_breakpoint", address))
        error = self.find_errors.pop(0) if self.find_errors else None
        if error is not None:
            raise error
        return self.realized

    def remove_breakpoint(self, address: int) -> None:
        self.calls.append(("remove_breakpoint", address))
        error = self.remove_errors.pop(0) if self.remove_errors else None
        if error is not None:
            raise error
        self.pending_mutation = "remove"


class FakeBreakpointManager:
    def __init__(self, target: FakeTarget) -> None:
        self.target = target

    def flush(self) -> None:
        self.target.calls.append(("breakpoint_manager.flush", None))
        error = (
            self.target.manager_flush_errors.pop(0) if self.target.manager_flush_errors else None
        )
        if error is not None:
            if (
                self.target.pending_mutation == "remove"
                and self.target.hw_disable_before_flush_error
            ):
                breakpoint = cast(Any, self.target.realized)
                if breakpoint is not None and swd_pyocd._breakpoint_kind(breakpoint) == "hw":
                    breakpoint.enabled = False
            raise error
        if self.target.pending_mutation == "install":
            self.target.realized = self.target.realized_after_flush
        elif self.target.pending_mutation == "remove" and self.target.remove_effective:
            if self.target.realized is not None:
                breakpoint = cast(Any, self.target.realized)
                kind = swd_pyocd._breakpoint_kind(breakpoint)
                if kind == "sw" and self.target.sw_restore_effective:
                    self.target.raw_instruction = breakpoint.original_instr
                elif kind == "hw" and self.target.disable_hw_on_remove:
                    breakpoint.enabled = False
                    if self.target.clear_hw_comparator_on_remove:
                        self.target.raw_memory_values[breakpoint.comp_register_addr] = 0
            self.target.realized = None
        self.target.pending_mutation = None


def handle_for(target: Any) -> TargetSessionHandle:
    return TargetSessionHandle(
        session=SimpleNamespace(target=target),
        board=None,
        probe_uid=None,
        route_used=swd_pyocd.ROUTE_PYOCD_NATIVE,
        target_override=None,
    )


class BreakpointInstallationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.quiet_patch = patch.object(
            swd_pyocd,
            "_backend_stdout_to_stderr",
            contextlib.nullcontext,
        )
        self.quiet_patch.start()

    def tearDown(self) -> None:
        self.quiet_patch.stop()

    def test_false_backend_result_is_reported_and_not_flushed(self) -> None:
        target = FakeTarget(accepted=False)

        with self.assertRaisesRegex(TargetControlError, "could not allocate.*0x00001234"):
            PyOCDSWDInterface().set_breakpoint(handle_for(target), 0x1234)

        self.assertEqual(
            target.calls,
            [("find_breakpoint", 0x1234), ("set_breakpoint", 0x1234)],
        )

    def test_success_is_reported_only_after_flush_and_realization(self) -> None:
        target = FakeTarget(realized_after_flush=hardware_breakpoint())

        PyOCDSWDInterface().set_breakpoint(handle_for(target), 0x5679)

        self.assertEqual(
            target.calls,
            [
                ("find_breakpoint", 0x5678),
                ("set_breakpoint", 0x5678),
                ("breakpoint_manager.flush", None),
                ("find_breakpoint", 0x5678),
                ("flush", None),
            ],
        )

    def test_preexisting_breakpoint_is_idempotent_and_never_owned(self) -> None:
        existing = hardware_breakpoint()
        target = FakeTarget(initially_realized=existing)

        PyOCDSWDInterface().set_breakpoint(handle_for(target), 0x5679)

        self.assertIs(target.realized, existing)
        self.assertEqual(target.calls, [("find_breakpoint", 0x5678)])

    def test_preexisting_unrealized_request_is_flushed_but_never_owned(self) -> None:
        queued = SimpleNamespace(enabled=False)
        realized = hardware_breakpoint()
        target = FakeTarget(initially_realized=queued, realized_after_flush=realized)
        target.pending_mutation = "install"

        PyOCDSWDInterface().set_breakpoint(handle_for(target), 0x5679)

        self.assertIs(target.realized, realized)
        self.assertEqual(
            target.calls,
            [
                ("find_breakpoint", 0x5678),
                ("breakpoint_manager.flush", None),
                ("flush", None),
                ("find_breakpoint", 0x5678),
            ],
        )

    def test_preexisting_unrealized_request_that_stays_unverified_requires_recovery(self) -> None:
        queued = SimpleNamespace(enabled=False)
        target = FakeTarget(initially_realized=queued, realized_after_flush=None)
        target.pending_mutation = "install"

        with self.assertRaisesRegex(
            TargetStateError,
            "remains unrealized or unverifiable.*Disconnect, power-cycle.*reflash/restore",
        ):
            PyOCDSWDInterface().set_breakpoint(handle_for(target), 0x5679)

        self.assertNotIn(("remove_breakpoint", 0x5678), target.calls)

    def test_manager_flush_failure_rolls_back_but_reports_uncertain_provider_state(self) -> None:
        backend_error = RuntimeError("provider failed")
        target = FakeTarget(
            realized_after_flush=hardware_breakpoint(),
            manager_flush_errors=(backend_error, None),
        )

        with self.assertRaisesRegex(
            TargetStateError,
            "uncertain.*manager/provider flush failed.*Disconnect, power-cycle.*reflash/restore",
        ) as caught:
            PyOCDSWDInterface().set_breakpoint(handle_for(target), 0x1234)

        self.assertIs(caught.exception.__cause__, backend_error)
        self.assertEqual(
            target.calls,
            [
                ("find_breakpoint", 0x1234),
                ("set_breakpoint", 0x1234),
                ("breakpoint_manager.flush", None),
                ("remove_breakpoint", 0x1234),
                ("breakpoint_manager.flush", None),
                ("flush", None),
                ("find_breakpoint", 0x1234),
            ],
        )

    def test_target_flush_failure_rolls_back_and_preserves_primary_error(self) -> None:
        backend_error = RuntimeError("target transfer flush failed")
        target = FakeTarget(
            realized_after_flush=hardware_breakpoint(),
            target_flush_errors=(backend_error, None),
        )

        with self.assertRaisesRegex(
            TargetConnectionError, "RuntimeError: target transfer flush failed"
        ) as caught:
            PyOCDSWDInterface().set_breakpoint(handle_for(target), 0x1234)

        self.assertIs(caught.exception.__cause__, backend_error)
        self.assertIsNone(target.realized)

    def test_successful_software_install_requires_raw_bkpt_instruction(self) -> None:
        target = FakeTarget(
            realized_after_flush=software_breakpoint(),
            raw_instruction=0xBE00,
        )

        PyOCDSWDInterface().set_breakpoint(handle_for(target), 0x1234)

        self.assertIn(("ap.read_memory", 0x1234), target.calls)
        self.assertEqual(target.raw_read_widths, [(16, True)])

    def test_software_install_raw_mismatch_requires_recovery_even_after_rollback(self) -> None:
        target = FakeTarget(
            realized_after_flush=software_breakpoint(),
            raw_instruction=0x1234,
        )

        with self.assertRaisesRegex(
            TargetStateError,
            "could not be proven in physical code memory.*reflash",
        ):
            PyOCDSWDInterface().set_breakpoint(handle_for(target), 0x1234)

        self.assertEqual(target.calls.count(("ap.read_memory", 0x1234)), 2)

    def test_target_flush_failure_with_silent_sw_restore_failure_is_uncertain(self) -> None:
        primary = RuntimeError("target transfer flush failed")
        target = FakeTarget(
            realized_after_flush=software_breakpoint(),
            target_flush_errors=(primary, None),
            sw_restore_effective=False,
            raw_instruction=0xBE00,
        )

        with self.assertRaisesRegex(
            TargetStateError,
            "uncertain.*provider-level rollback.*raw instruction.*Disconnect, restore/reflash",
        ) as caught:
            PyOCDSWDInterface().set_breakpoint(handle_for(target), 0x1234)

        self.assertIs(caught.exception.__cause__, primary)

    def test_missing_realization_is_uncertain_despite_manager_cleanup(self) -> None:
        target = FakeTarget(realized_after_flush=None)

        with self.assertRaisesRegex(
            TargetStateError,
            "did not return a realized breakpoint.*suppressed software-provider.*reflash",
        ):
            PyOCDSWDInterface().set_breakpoint(handle_for(target), 0x9ABC)

        self.assertEqual(target.calls.count(("remove_breakpoint", 0x9ABC)), 1)
        self.assertEqual(target.calls[-1], ("find_breakpoint", 0x9ABC))

    def test_cleanup_failure_reports_uncertain_state_and_recovery(self) -> None:
        primary = RuntimeError("initial provider flush failed")
        cleanup = RuntimeError("rollback provider flush failed")
        target = FakeTarget(manager_flush_errors=(primary, cleanup))

        with self.assertRaisesRegex(
            TargetStateError,
            "uncertain.*rollback could not be proven.*Disconnect, power-cycle.*reflash/restore",
        ) as caught:
            PyOCDSWDInterface().set_breakpoint(handle_for(target), 0x1234)

        self.assertIs(caught.exception.__cause__, primary)

    def test_unverifiable_cleanup_reports_uncertain_state_and_recovery(self) -> None:
        primary = RuntimeError("initial provider flush failed")
        target = FakeTarget(
            manager_flush_errors=(primary, None),
            find_errors=(None, RuntimeError("provider query failed")),
        )

        with self.assertRaisesRegex(
            TargetStateError,
            "uncertain.*absence verification.*Disconnect, power-cycle.*reflash/restore",
        ):
            PyOCDSWDInterface().set_breakpoint(handle_for(target), 0x1234)

    def test_successful_removal_canonicalizes_flushes_and_verifies_absence(self) -> None:
        target = FakeTarget(initially_realized=hardware_breakpoint())

        PyOCDSWDInterface().remove_breakpoint(handle_for(target), 0x5679)

        self.assertEqual(
            target.calls,
            [
                ("find_breakpoint", 0x5678),
                ("remove_breakpoint", 0x5678),
                ("breakpoint_manager.flush", None),
                ("flush", None),
                ("find_breakpoint", 0x5678),
                ("ap.read_memory", 0xE0002008),
            ],
        )

    def test_successful_sw_removal_raw_reads_restored_original(self) -> None:
        target = FakeTarget(
            initially_realized=software_breakpoint(original_instr=0x1234),
            raw_instruction=0xBE00,
        )

        PyOCDSWDInterface().remove_breakpoint(handle_for(target), 0x1234)

        self.assertEqual(target.raw_instruction, 0x1234)
        self.assertEqual(target.raw_read_widths, [(16, True)])

    def test_silent_sw_removal_restore_mismatch_requires_recovery(self) -> None:
        target = FakeTarget(
            initially_realized=software_breakpoint(original_instr=0x1234),
            sw_restore_effective=False,
            raw_instruction=0xBE00,
        )

        with self.assertRaisesRegex(
            TargetStateError,
            "raw instruction is 0xBE00.*Disconnect, restore/reflash",
        ):
            PyOCDSWDInterface().remove_breakpoint(handle_for(target), 0x1234)

    def test_sw_removal_raw_read_failure_requires_recovery(self) -> None:
        read_error = RuntimeError("AP read failed")
        target = FakeTarget(
            initially_realized=software_breakpoint(original_instr=0x1234),
            raw_read_errors=(read_error,),
        )

        with self.assertRaisesRegex(
            TargetStateError,
            "raw instruction read failed.*Disconnect, restore/reflash",
        ):
            PyOCDSWDInterface().remove_breakpoint(handle_for(target), 0x1234)

    def test_hw_remove_retry_cannot_hide_uncleared_comparator(self) -> None:
        primary = RuntimeError("comparator clear write failed")
        target = FakeTarget(
            initially_realized=hardware_breakpoint(),
            manager_flush_errors=(primary, None),
            hw_disable_before_flush_error=True,
            clear_hw_comparator_on_remove=False,
            comparator_value=0x12345679,
        )

        with self.assertRaisesRegex(
            TargetStateError,
            "hardware comparator remains programmed.*Disconnect, power-cycle.*revalidate",
        ) as caught:
            PyOCDSWDInterface().remove_breakpoint(handle_for(target), 0x1234)

        self.assertIs(caught.exception.__cause__, primary)

    def test_hw_removal_comparator_read_failure_requires_recovery(self) -> None:
        target = FakeTarget(
            initially_realized=hardware_breakpoint(),
            raw_read_errors=(RuntimeError("AP comparator read failed"),),
        )

        with self.assertRaisesRegex(
            TargetStateError,
            "raw hardware comparator read failed.*Disconnect, power-cycle.*revalidate",
        ):
            PyOCDSWDInterface().remove_breakpoint(handle_for(target), 0x1234)

    def test_removal_flush_failure_with_no_proof_reports_uncertain_state(self) -> None:
        first = RuntimeError("provider flush failed")
        retry = RuntimeError("provider flush still failed")
        target = FakeTarget(
            initially_realized=hardware_breakpoint(),
            manager_flush_errors=(first, retry),
        )

        with self.assertRaisesRegex(
            TargetStateError,
            "uncertain.*provider state could not be verified.*Disconnect, power-cycle.*revalidate",
        ) as caught:
            PyOCDSWDInterface().remove_breakpoint(handle_for(target), 0x5678)

        self.assertIs(caught.exception.__cause__, first)

    def test_confirmed_remaining_breakpoint_is_a_control_failure(self) -> None:
        target = FakeTarget(initially_realized=hardware_breakpoint(), remove_effective=False)

        with self.assertRaisesRegex(TargetControlError, "remains installed"):
            PyOCDSWDInterface().remove_breakpoint(handle_for(target), 0x5678)

    def test_unverifiable_removal_reports_uncertain_state_and_recovery(self) -> None:
        target = FakeTarget(
            initially_realized=hardware_breakpoint(),
            find_errors=(
                None,
                RuntimeError("query failed"),
                RuntimeError("query still failed"),
            ),
        )

        with self.assertRaisesRegex(
            TargetStateError,
            "uncertain.*absence verification.*Disconnect, power-cycle.*revalidate",
        ):
            PyOCDSWDInterface().remove_breakpoint(handle_for(target), 0x5678)


if __name__ == "__main__":
    unittest.main()
