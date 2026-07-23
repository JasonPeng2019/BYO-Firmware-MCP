from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from firmware_mcp.adapters.debug_interface import TargetSessionHandle
from firmware_mcp.adapters.swd_pyocd import PyOCDSWDInterface
from firmware_mcp.board_config import BoardConfig
from firmware_mcp.target_errors import TargetConnectionError, TargetStateError


def _record(address: int, record_type: int, data: bytes) -> str:
    body = bytes((len(data), (address >> 8) & 0xFF, address & 0xFF, record_type)) + data
    return ":" + (body + bytes(((-sum(body)) & 0xFF,))).hex().upper()


class _Region:
    is_flash = True
    is_writable = True
    end = 0xFF

    def contains_range(self, start: int, end: int) -> bool:
        return 0 <= start <= end < 0x100


class _Target:
    def __init__(
        self,
        data: bytes,
        *,
        reset_error: Exception | None = None,
        observed_state: str = "RUNNING",
    ) -> None:
        self.data = bytearray(data)
        self.reset_error = reset_error
        self.memory_map = SimpleNamespace(get_region_for_address=lambda _address: _Region())
        self.reset_and_halt_calls = 0
        self.observed_state = observed_state

    def reset_and_halt(self) -> None:
        self.reset_and_halt_calls += 1

    def reset(self) -> None:
        if self.reset_error is not None:
            raise self.reset_error

    def get_state(self) -> SimpleNamespace:
        return SimpleNamespace(name=self.observed_state)

    def read_memory(self, address: int, width: int) -> int:
        del address, width
        return 0x1234

    def read_memory_block8(self, address: int, length: int) -> list[int]:
        return list(self.data[address : address + length])


class SWDFlashVerificationTests(unittest.TestCase):
    def _image(self, data: bytes) -> Path:
        temporary = tempfile.NamedTemporaryFile(
            suffix=".hex", delete=False, mode="w", encoding="ascii"
        )
        self.addCleanup(lambda: Path(temporary.name).unlink(missing_ok=True))
        temporary.write(_record(0, 0, data) + "\n" + _record(0, 1, b"") + "\n")
        temporary.close()
        return Path(temporary.name)

    def _flash(self, target: _Target, path: Path, *, halt_after_reset: bool = False):
        board = SimpleNamespace(
            display_name="test",
            probe_family="test",
            silicon_id_addr=0x4000,
            silicon_id_expected=0x1234,
            silicon_id_mask=0xFFFFFFFF,
            silicon_id_width_bits=32,
            silicon_id_capability="exact",
            silicon_id_provenance="datasheet section 1",
            silicon_id_bound_part_number="test-part",
            silicon_id_support_identity="test-support",
            provider_support_identity="test-support",
        )
        handle = TargetSessionHandle(
            SimpleNamespace(target=target), cast(BoardConfig, board), None, "test", None
        )
        programmer = SimpleNamespace(program=lambda _path: None)
        with patch("firmware_mcp.adapters.swd_pyocd.FileProgrammer", return_value=programmer):
            return PyOCDSWDInterface().flash(handle, path, halt_after_reset=halt_after_reset)

    def test_reports_exact_byte_readback_evidence(self) -> None:
        result = self._flash(_Target(b"\x12\x34"), self._image(b"\x12\x34"))

        self.assertEqual(result.byte_count, 2)
        self.assertEqual(result.verified_ranges, ((0, 2),))
        self.assertEqual(result.expected_sha256, result.observed_sha256)
        self.assertEqual(result.final_reset_postcondition, "RUNNING")

    def test_provider_regions_are_live_session_evidence_not_catalog_roles(self) -> None:
        target = SimpleNamespace(
            memory_map=SimpleNamespace(
                regions=[
                    SimpleNamespace(
                        start=0x40000000,
                        end=0x40000003,
                        is_readable=False,
                        is_writable=True,
                        is_executable=False,
                        type="vendor_special",
                        name="security option register",
                    )
                ]
            )
        )
        handle = TargetSessionHandle(SimpleNamespace(target=target), None, None, "test", None)

        regions = PyOCDSWDInterface().physical_memory_regions(handle)

        self.assertEqual(
            [(item.start, item.end, item.writable) for item in regions],
            [(0x40000000, 0x40000004, True)],
        )
        self.assertEqual(regions[0].name, "security option register")
        assert handle.metadata is not None
        self.assertEqual(regions[0].session_token, handle.metadata.runtime_token)
        self.assertEqual(regions[0].provenance, "current_live_pyocd_provider_session")

    def test_one_byte_mismatch_fails_before_final_reset(self) -> None:
        target = _Target(b"\x12\x99")

        with self.assertRaisesRegex(
            TargetStateError, r"0x0000000000000001.*expected 0x34, observed 0x99"
        ):
            self._flash(target, self._image(b"\x12\x34"))

        self.assertEqual(target.reset_and_halt_calls, 1)

    def test_final_reset_and_halt_mismatch_preserves_verified_write_evidence(self) -> None:
        result = self._flash(
            _Target(b"\x12\x34", observed_state="RUNNING"),
            self._image(b"\x12\x34"),
            halt_after_reset=True,
        )

        self.assertEqual(result.final_reset_postcondition, "failed")
        self.assertEqual(result.final_reset_error_type, "TargetStateError")
        self.assertIn("halt_after_reset=true", result.final_reset_error_message or "")
        self.assertIn("observed_state=RUNNING", result.final_reset_error_message or "")
        self.assertIn("expected_state=HALTED", result.final_reset_error_message or "")
        self.assertEqual(result.byte_count, 2)
        self.assertEqual(result.verified_ranges, ((0, 2),))
        self.assertEqual(result.expected_sha256, result.observed_sha256)

    def test_final_reset_transport_failure_is_explicitly_unknown_with_real_cause(self) -> None:
        class DistinctResetDrop(Exception):
            pass

        result = self._flash(
            _Target(b"\x12", reset_error=DistinctResetDrop("reset link disappeared")),
            self._image(b"\x12"),
        )

        self.assertEqual(result.final_reset_postcondition, "unknown")
        self.assertEqual(result.final_reset_error_type, "DistinctResetDrop")
        self.assertEqual(result.final_reset_error_message, "reset link disappeared")
        self.assertEqual(result.byte_count, 1)
        self.assertEqual(result.expected_sha256, result.observed_sha256)

    def test_readback_failure_never_returns_flash_success(self) -> None:
        target = _Target(b"\x12")
        target.read_memory_block8 = lambda address, length: (_ for _ in ()).throw(
            OSError("link lost")
        )

        with self.assertRaisesRegex(TargetConnectionError, "link lost"):
            self._flash(target, self._image(b"\x12"))

    def test_no_writable_flash_authority_rejects_before_programming(self) -> None:
        target = _Target(b"\x12")
        target.memory_map = SimpleNamespace(get_region_for_address=lambda _address: None)

        with self.assertRaisesRegex(TargetStateError, "writable-flash"):
            self._flash(target, self._image(b"\x12"))

    def test_same_core_with_wrong_silicon_id_cannot_program(self) -> None:
        target = _Target(b"\x12")
        target.read_memory = lambda address, width: 0x4321

        with self.assertRaisesRegex(TargetStateError, "silicon-ID evidence does not match"):
            self._flash(target, self._image(b"\x12"))

    def test_adjacent_writable_flash_banks_cover_one_continuous_image(self) -> None:
        target = _Target(b"\x12\x34")
        first = _Region()
        first.end = 0
        second = _Region()
        second.end = 0xFF
        target.memory_map = SimpleNamespace(
            get_region_for_address=lambda address: (
                first if address == 0 else second if address == 1 else None
            )
        )

        result = self._flash(target, self._image(b"\x12\x34"))

        self.assertEqual(result.byte_count, 2)

    def test_gap_or_nonwritable_boundary_fails_before_programmer_runs(self) -> None:
        target = _Target(b"\x12\x34")
        first = _Region()
        first.end = 0
        readonly = _Region()
        readonly.is_writable = False
        readonly.end = 1
        target.memory_map = SimpleNamespace(
            get_region_for_address=lambda address: (
                first if address == 0 else readonly if address == 1 else None
            )
        )
        board = SimpleNamespace(
            display_name="test",
            probe_family="test",
            silicon_id_addr=0x4000,
            silicon_id_expected=0x1234,
            silicon_id_mask=0xFFFFFFFF,
            silicon_id_width_bits=32,
            silicon_id_capability="exact",
            silicon_id_provenance="datasheet section 1",
            silicon_id_bound_part_number="test-part",
            silicon_id_support_identity="test-support",
            provider_support_identity="test-support",
        )
        handle = TargetSessionHandle(
            SimpleNamespace(target=target), cast(BoardConfig, board), None, "test", None
        )
        programmer = SimpleNamespace(program=lambda _path: None)
        with patch(
            "firmware_mcp.adapters.swd_pyocd.FileProgrammer", return_value=programmer
        ) as factory:
            with self.assertRaisesRegex(TargetStateError, "writable-flash authority"):
                PyOCDSWDInterface().flash(handle, self._image(b"\x12\x34"), halt_after_reset=False)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
