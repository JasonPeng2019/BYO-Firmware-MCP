"""Regression evidence for invocation-scoped provider physical-memory facts."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from firmware_mcp import server
from firmware_mcp.adapters.debug_interface import (
    PhysicalMemoryRegion,
    TargetSessionHandle,
    TargetSessionMetadata,
)
from firmware_mcp.adapters.debug_process import _validate_result
from firmware_mcp.board_config import BoardConfig
from firmware_mcp.services.physical_memory import (
    PhysicalMemoryAccessError,
    PhysicalSpanEvidence,
    PhysicalMemoryFactsUnavailable,
    require_live_physical_access,
)
from firmware_mcp.services.safety_authority import SafetyAuthorityError
from firmware_mcp.target_errors import TargetControlError


def _board() -> BoardConfig:
    return BoardConfig(
        board_id="board",
        display_name="Board",
        mcu_family="exact-part",
        probe_family="provider",
        target="exact-part",
        probe_type="provider",
        probe_hint_terms=(),
        serial_hint_terms=(),
        test_addr=None,
        silicon_id_addr=0x50000000,
        silicon_id_expected=0x1234,
        silicon_id_mask=0xFFFF,
        silicon_id_width_bits=32,
        silicon_id_label="part id",
        silicon_id_capability="exact",
        silicon_id_provenance="datasheet locator",
        silicon_id_bound_part_number="exact-part",
        silicon_id_support_identity="support-digest",
        provider_support_identity="support-digest",
    )


def _handle(*, token: str = "session-a", board: BoardConfig | None = None) -> TargetSessionHandle:
    return TargetSessionHandle(
        session=None,
        board=_board() if board is None else board,
        probe_uid="probe",
        route_used="provider",
        target_override=None,
        metadata=TargetSessionMetadata(
            board_name="Board",
            probe_description="Probe",
            probe_family="provider",
            probe_uid="probe",
            live_part_number="exact-part",
            route_used="provider",
            target_override=None,
            runtime_token=token,
        ),
    )


def _region(
    start: int,
    end: int,
    *,
    token: str = "session-a",
    read: bool = True,
    write: bool = True,
    execute: bool = False,
    name: str = "provider region",
) -> PhysicalMemoryRegion:
    return PhysicalMemoryRegion(
        start,
        end,
        read,
        write,
        execute,
        "provider_kind",
        name,
        "current_live_provider_session",
        token,
    )


class PhysicalMemoryEvidenceTests(unittest.TestCase):
    def _require(
        self,
        regions: tuple[PhysicalMemoryRegion, ...],
        start: int,
        length: int,
        access: str,
        *,
        handle: TargetSessionHandle | None = None,
    ) -> PhysicalSpanEvidence:
        return require_live_physical_access(
            _handle() if handle is None else handle,
            start,
            length,
            access,
            regions_for=lambda _handle: regions,
            read_memory=lambda _handle, address, width: (
                0x1234 if (address, width) == (0x50000000, 32) else 0
            ),
        )

    def test_span_uses_current_exact_identity_and_adjacent_live_regions(self) -> None:
        evidence = self._require(
            (
                _region(0x20000000, 0x20000002, name="boot security named region"),
                _region(0x20000002, 0x20000004, name="option named region"),
            ),
            0x20000000,
            4,
            "write",
        )

        self.assertEqual(evidence.start, 0x20000000)
        self.assertEqual(evidence.end, 0x20000004)
        self.assertEqual(
            [region.name for region in evidence.regions],
            ["boot security named region", "option named region"],
        )

    def test_gap_wrong_access_stale_and_missing_identity_are_not_authority(self) -> None:
        with self.assertRaises(PhysicalMemoryAccessError):
            self._require((_region(0x20000000, 0x20000002),), 0x20000000, 4, "write")
        with self.assertRaises(PhysicalMemoryAccessError):
            self._require((_region(0x20000000, 0x20000004, write=False),), 0x20000000, 4, "write")
        with self.assertRaises(PhysicalMemoryFactsUnavailable):
            self._require(
                (_region(0x20000000, 0x20000004, token="old-session"),), 0x20000000, 4, "read"
            )
        evidence = self._require(
            (_region(0x20000000, 0x20000004),),
            0x20000000,
            4,
            "read",
            handle=_handle(board=replace(_board(), silicon_id_capability="compatible")),
        )
        self.assertEqual(evidence.start, 0x20000000)

    def test_malformed_provider_records_are_rejected_at_worker_boundary(self) -> None:
        record = _region(0x20000000, 0x20000004).to_record()
        result = _validate_result("physical_memory_regions", [record])
        self.assertEqual(result, (_region(0x20000000, 0x20000004),))

        malformed = dict(record)
        malformed["writable"] = "true"
        with self.assertRaisesRegex(ValueError, "physical-memory"):
            _validate_result("physical_memory_regions", [malformed])
        with self.assertRaisesRegex(ValueError, "overlap"):
            _validate_result(
                "physical_memory_regions",
                [record, _region(0x20000002, 0x20000006).to_record()],
            )

    def test_registered_memory_and_breakpoint_handlers_receive_live_checks(self) -> None:
        memory_checks: list[tuple[object, int, int]] = []
        breakpoint_checks: list[tuple[object, int, str | None]] = []
        memory_tool = server.mcp._tool_manager.get_tool("read_memory")
        breakpoint_tool = server.mcp._tool_manager.get_tool("set_breakpoint")
        self.assertIsNotNone(memory_tool)
        self.assertIsNotNone(breakpoint_tool)
        assert memory_tool is not None and breakpoint_tool is not None
        memory_services = server.memory_services
        breakpoint_services = server.breakpoint_services
        old_memory = {
            name: getattr(memory_services, name)
            for name in (
                "runtime_for",
                "active_session_id",
                "duration_ms",
                "record_event",
                "handle_for",
                "check_memory_read",
                "read_target_memory",
            )
        }
        old_breakpoint = {
            name: getattr(breakpoint_services, name)
            for name in (
                "runtime_for",
                "active_session_id",
                "duration_ms",
                "record_event",
                "handle_for",
                "check_breakpoint",
                "set_target_breakpoint",
            )
        }
        handle = object()
        try:
            for name, value in {
                "runtime_for": lambda _board: None,
                "active_session_id": lambda _board: None,
                "duration_ms": lambda _started: 0,
                "record_event": lambda *args, **kwargs: None,
                "handle_for": lambda _board: handle,
                "check_memory_read": lambda current, address, width: (
                    memory_checks.append((current, address, width)) or {"unknown": True}
                ),
                "read_target_memory": lambda *_args: 0x12,
            }.items():
                object.__setattr__(memory_services, name, value)
            for name, value in {
                "runtime_for": lambda _board: None,
                "active_session_id": lambda _board: None,
                "duration_ms": lambda _started: 0,
                "record_event": lambda *args, **kwargs: None,
                "handle_for": lambda _board: handle,
                "check_breakpoint": lambda current, address, elf_path: breakpoint_checks.append(
                    (current, address, elf_path)
                ),
                "set_target_breakpoint": lambda *_args: None,
            }.items():
                object.__setattr__(breakpoint_services, name, value)
            memory_handler = getattr(memory_tool.fn, "_guarded_raw_handler", memory_tool.fn)
            breakpoint_handler = getattr(
                breakpoint_tool.fn, "_guarded_raw_handler", breakpoint_tool.fn
            )
            read = memory_handler("board", "0x20000000", 32)
            self.assertIn("0x00000012", read)
            self.assertIn("semantic_role=unknown", read)
            self.assertIn(
                "Breakpoint set",
                breakpoint_handler("board", "0x08000001", "build/application.elf"),
            )
        finally:
            for name, value in old_memory.items():
                object.__setattr__(memory_services, name, value)
            for name, value in old_breakpoint.items():
                object.__setattr__(breakpoint_services, name, value)

        self.assertEqual(memory_checks, [(handle, 0x20000000, 4)])
        self.assertEqual(breakpoint_checks, [(handle, 0x08000000, "build/application.elf")])

    def test_breakpoint_authority_rejects_load_bytes_beyond_checked_elf_snapshot(self) -> None:
        header = struct.pack(
            "<16sHHIIIIIHHHHHH",
            b"\x7fELF\x01\x01\x01" + b"\0" * 9,
            2,
            40,
            1,
            0,
            52,
            0,
            0,
            52,
            32,
            1,
            0,
            0,
            0,
        )
        payload = header + struct.pack("<IIIIIIII", 1, 0x100, 0, 0, 4, 4, 5, 4)
        with (
            patch.object(server, "_require_safety_access", return_value={}),
            patch.object(server._guard_core, "execution_file", return_value=payload),
        ):
            with self.assertRaisesRegex(SafetyAuthorityError, "immutable snapshot"):
                server._check_breakpoint_safety(_handle(), 0, "checked.elf")

    def test_full_mask_no_verify_write_is_write_only_capable_and_performs_no_read(self) -> None:
        writes: list[tuple[int, int, int]] = []
        with (
            patch.object(server, "_handle", return_value=SimpleNamespace()),
            patch.object(server.connection_manager, "lock_for", return_value=nullcontext()),
            patch.object(
                server,
                "_run_logged_tool",
                side_effect=lambda _board, _name, _args, operation: operation(),
            ),
            patch.object(server, "_require_physical_access") as require_access,
            patch.object(server, "_require_safety_access"),
            patch.object(
                server.target_control, "read_memory", side_effect=AssertionError("must not read")
            ),
            patch.object(
                server.target_control,
                "write_memory",
                side_effect=lambda _handle, address, value, width: writes.append(
                    (address, value, width)
                ),
            ),
        ):
            result = server._masked_register_write(
                "board", 0x40000000, 0xFFFFFFFF, 0x12345678, False
            )

        self.assertIn("provider accepted", result)
        self.assertIn("verification=not_requested", result)
        self.assertEqual(writes, [(0x40000000, 0x12345678, 32)])
        self.assertEqual(require_access.call_args_list[0].args[1:], (0x40000000, 4, "write"))

    def test_partial_or_verified_write_only_request_refuses_before_mutation(self) -> None:
        with (
            patch.object(server, "_handle", return_value=SimpleNamespace()),
            patch.object(server.connection_manager, "lock_for", return_value=nullcontext()),
            patch.object(
                server,
                "_run_logged_tool",
                side_effect=lambda _board, _name, _args, operation: operation(),
            ),
            patch.object(server, "_require_physical_access"),
            patch.object(
                server,
                "_require_safety_access",
                side_effect=(None, PhysicalMemoryAccessError("not readable")),
            ),
            patch.object(server.target_control, "write_memory") as write_memory,
        ):
            with self.assertRaisesRegex(TargetControlError, "Partial peripheral-register writes"):
                server._masked_register_write("board", 0x40000000, 0xFF, 0x12, False)

        write_memory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
