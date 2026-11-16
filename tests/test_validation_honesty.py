from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from pyocd_debug_mcp.board_config import BoardConfig
from pyocd_debug_mcp.firmstore.reports import ReportPaths
from pyocd_debug_mcp.services.connections import ConnectionManager
from pyocd_debug_mcp.setup_flow.validate import BoardValidator, SafetyMapSnapshot, ValidationBackend, ValidationHooks, ValidationInventory, ValidationProbe, ValidationRequest


class ValidationHonestyTests(unittest.TestCase):
    @staticmethod

    def test_candidate_validation_uses_staged_profile_and_map_before_policy_commit(self) -> None:
        """Fresh setup validates its exact staged bytes without an active policy pointer."""

        board = BoardConfig(
            board_id="fresh_board",
            display_name="Fresh board",
            mcu_family="stm32",
            probe_family="jlink",
            pyocd_target="stm32f4",
            probe_type="jlink",
            probe_hint_terms=(),
            serial_hint_terms=(),
            silicon_id_addr=0xE0042000,
            silicon_id_expected=0x1234,
            silicon_id_mask=0xFFFFFFFF,
            silicon_id_width_bits=32,
            silicon_id_label="device id",
            test_addr=0x20000000,
        )
        candidate = SimpleNamespace(
            board_id=board.board_id,
            board=board,
            mcu_part_number="STM32F4",
            source_path=Path("staged-profile.yaml"),
            device_support=None,
        )
        stamp = Mock(return_value=True)
        profiles = SimpleNamespace(
            load=Mock(side_effect=AssertionError("candidate validation must not load a profile"))
        )
        validator = BoardValidator(
            cast(Any, profiles),
            Mock(),
            ValidationBackend(
                inventory=lambda: ValidationInventory(
                    probes=(ValidationProbe("probe-1", "Probe", "jlink", "serial-1"),)
                ),
                target_supported=lambda _target: True,
                connect=lambda *_args: object(),
                read_memory=lambda *_args: 0x1234,
                capture_serial=Mock(),
                close=Mock(),
            ),
            hooks=ValidationHooks(
                load_safety_map=Mock(
                    side_effect=AssertionError("candidate validation must not load active safety")
                ),
                stamp_session=stamp,
                record_mismatch=lambda *_args: False,
            ),
        )
        reports = ReportPaths(Path("report.json"), Path("events.jsonl"))

        with patch.object(validator, "_write_report", return_value=reports):
            result = validator.validate(
                ValidationRequest(
                    "fresh_board",
                    "probe-1",
                    candidate_profile=candidate,
                    candidate_safety_map=SafetyMapSnapshot(True, True, "candidate-map-digest"),
                )
            )

        self.assertEqual(result.status, "validation_passed")
        profiles.load.assert_not_called()
        self.assertEqual(stamp.call_args.args[-1], candidate)


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


if __name__ == "__main__":
    unittest.main()
