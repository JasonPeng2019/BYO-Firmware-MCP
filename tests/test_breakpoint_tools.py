from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from firmware_mcp.tools.breakpoints import BreakpointToolServices, build_breakpoint_handlers


class BreakpointToolCanonicalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.safety_addresses: list[int] = []
        self.set_addresses: list[int] = []
        self.remove_addresses: list[int] = []
        services = BreakpointToolServices(
            runtime_for=lambda _board_id: None,
            active_session_id=lambda _board_id: "session",
            duration_ms=lambda _started: 0,
            record_event=lambda tool_name, args, **kwargs: self.events.append(
                {"tool_name": tool_name, "args": args, **kwargs}
            ),
            format_invalid=lambda invalid, **_kwargs: invalid.message,
            handle_for=lambda _board_id: SimpleNamespace(),
            set_target_breakpoint=lambda _handle, address: self.set_addresses.append(address),
            remove_target_breakpoint=lambda _handle, address: self.remove_addresses.append(address),
            check_breakpoint=lambda _handle, address, _elf_path: self.safety_addresses.append(
                address
            ),
        )
        self.handlers = build_breakpoint_handlers(services)

    def test_explicit_thumb_address_with_elf_is_canonical(self) -> None:
        result = self.handlers["set_breakpoint"]("board", "0x1c1", "firmware.elf")

        self.assertEqual(self.safety_addresses, [0x1C0])
        self.assertEqual(self.set_addresses, [0x1C0])
        self.assertEqual(self.events[-1]["details"]["resolved_address"], 0x1C0)
        self.assertIn("0x000001C0", result)

    def test_symbol_text_is_not_a_numeric_breakpoint(self) -> None:
        result = self.handlers["set_breakpoint"]("board", "main", "firmware.elf")

        self.assertIn("decimal or hexadecimal", result)
        self.assertEqual(self.set_addresses, [])

    def test_set_requires_elf_evidence(self) -> None:
        result = self.handlers["set_breakpoint"]("board", "0x1c1", "")

        self.assertIn("elf_path is required", result)
        self.assertEqual(self.set_addresses, [])

    def test_remove_thumb_address_is_canonical_everywhere(self) -> None:
        result = self.handlers["remove_breakpoint"]("board", "0x1c1")

        self.assertEqual(self.remove_addresses, [0x1C0])
        self.assertEqual(self.safety_addresses, [0x1C0])
        self.assertEqual(self.events[-1]["details"]["resolved_address"], 0x1C0)
        self.assertIn("0x000001C0", result)


if __name__ == "__main__":
    unittest.main()
