from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from pyocd_debug_mcp.services.symbols import ResolvedSymbol
from pyocd_debug_mcp.tools import breakpoints
from pyocd_debug_mcp.tools.breakpoints import BreakpointToolServices, build_breakpoint_handlers


class BreakpointToolCanonicalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.safety_addresses: list[int] = []
        self.set_addresses: list[int] = []
        self.remove_addresses: list[int] = []
        self.elf = tempfile.NamedTemporaryFile(suffix=".elf", delete=False)
        self.elf.close()
        services = BreakpointToolServices(
            runtime_for=lambda _board_id: None,
            active_session_id=lambda _board_id: "session",
            duration_ms=lambda _started: 0,
            record_event=lambda tool_name, args, **kwargs: self.events.append(
                {"tool_name": tool_name, "args": args, **kwargs}
            ),
            format_refusal=lambda refusal, **_kwargs: refusal.message,
            handle_for=lambda _board_id: SimpleNamespace(),
            resolve_symbol=lambda _artifact, _symbol: ResolvedSymbol(
                name="main",
                address=0x1C1,
                size=4,
                type="STT_FUNC",
            ),
            set_target_breakpoint=lambda _handle, address: self.set_addresses.append(address),
            remove_target_breakpoint=lambda _handle, address: self.remove_addresses.append(address),
            check_breakpoint=lambda _board_id, address, _artifact: (
                self.safety_addresses.append(address)
            ),
        )
        self.handlers = build_breakpoint_handlers(services)
        self.elf_patch = patch.object(breakpoints, "is_elf_artifact", return_value=True)
        self.elf_patch.start()

    def tearDown(self) -> None:
        self.elf_patch.stop()
        Path(self.elf.name).unlink(missing_ok=True)

    def test_explicit_thumb_address_is_canonical_everywhere(self) -> None:
        result = self.handlers["set_breakpoint"]("board", "0x1c1", self.elf.name)

        self.assertEqual(self.safety_addresses, [0x1C0])
        self.assertEqual(self.set_addresses, [0x1C0])
        self.assertEqual(self.events[-1]["details"]["resolved_address"], 0x1C0)
        self.assertIn("0x000001C0", result)

    def test_thumb_symbol_is_canonical_everywhere(self) -> None:
        result = self.handlers["set_breakpoint"]("board", "main", self.elf.name)

        self.assertEqual(self.safety_addresses, [0x1C0])
        self.assertEqual(self.set_addresses, [0x1C0])
        self.assertEqual(self.events[-1]["details"]["resolved_address"], 0x1C0)
        self.assertIn("0x000001C0", result)

    def test_remove_thumb_address_is_canonical_everywhere(self) -> None:
        result = self.handlers["remove_breakpoint"]("board", "0x1c1")

        self.assertEqual(self.remove_addresses, [0x1C0])
        self.assertEqual(self.events[-1]["details"]["resolved_address"], 0x1C0)
        self.assertIn("0x000001C0", result)


if __name__ == "__main__":
    unittest.main()
