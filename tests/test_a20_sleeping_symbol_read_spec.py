"""Adversarial contract tests for the coherent scalar-symbol snapshot repair."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp import server
from pyocd_debug_mcp.services import target_control
from pyocd_debug_mcp.services.session_runtime import ToolOutcome
from pyocd_debug_mcp.services.symbols import ResolvedSymbol
from pyocd_debug_mcp.tools.memory import MemoryToolServices, build_memory_handlers


class CoherentSymbolReadSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.artifact = Path(self.temporary_directory.name) / "app.elf"
        self.artifact.write_bytes(b"fixture ELF")
        self.calls: list[str] = []
        self.recorded: list[ToolOutcome] = []
        self.handle = object()

    def _services(
        self,
        state: str = "RUNNING",
        value: int = 0x1234,
        state_error: BaseException | None = None,
        halt_error: BaseException | None = None,
        read_error: BaseException | None = None,
        resume_error: BaseException | None = None,
    ) -> MemoryToolServices:
        def get_state(_handle: object) -> str:
            self.calls.append("state")
            if state_error is not None:
                raise state_error
            return state

        def halt(_handle: object) -> None:
            self.calls.append("halt")
            if halt_error is not None:
                raise halt_error

        def read(_handle: object, address: int, width: int) -> int:
            self.calls.append(f"read:{address:#x}:{width}")
            if read_error is not None:
                raise read_error
            return value

        def resume(_handle: object) -> None:
            self.calls.append("resume")
            if resume_error is not None:
                raise resume_error

        def write(_handle: object, address: int, value: int, width: int) -> None:
            self.calls.append(f"write:{address:#x}:{value:#x}:{width}")

        def record_event(*_args: object, **kwargs: object) -> None:
            self.recorded.append(kwargs["outcome_kind"])  # type: ignore[arg-type]

        return MemoryToolServices(
            runtime_for=lambda _board: None,
            active_session_id=lambda _board: None,
            duration_ms=lambda _started: 0,
            record_event=record_event,
            format_refusal=lambda refusal, **_kwargs: f"Refused [{refusal.code}]: {refusal.message}",
            handle_for=lambda _board: self.handle,
            symbol_artifact_for=lambda _handle: self.artifact,
            find_symbols=lambda _artifact, _query: (),
            resolve_symbol=lambda _artifact, symbol: ResolvedSymbol(
                symbol, 0x20000000, 4, "STT_OBJECT"
            ),
            read_target_memory=read,
            read_target_block=lambda _handle, _address, _length: [0],
            write_target_memory=write,
            check_memory_read=lambda _board, _address, _length: None,
            check_memory_write=lambda _board, _address, _width: None,
            get_state=get_state,
            halt=halt,
            resume=resume,
        )

    def _symbol_read(self, services: MemoryToolServices) -> str:
        handler = build_memory_handlers(services)["read_memory_symbol"]
        with patch("pyocd_debug_mcp.tools.memory.is_elf_artifact", return_value=True):
            return handler("board", "counter", 32, str(self.artifact))

    def test_halted_read_is_direct_and_preserves_a_legitimate_zero(self) -> None:
        result = self._symbol_read(self._services(state="hAlTeD", value=0))

        self.assertIn("value=0x00000000", result)
        self.assertEqual(self.calls, ["state", "read:0x20000000:32"])
        self.assertEqual(self.recorded, [ToolOutcome.SUCCESS])

    def test_every_non_halted_state_halts_reads_and_restores_before_success(self) -> None:
        for state in ("RUNNING", "sleeping", "RESET", "unknown-provider-state"):
            with self.subTest(state=state):
                self.calls.clear()
                self.recorded.clear()
                result = self._symbol_read(self._services(state=state))

                self.assertIn("value=0x00001234", result)
                self.assertEqual(
                    self.calls, ["state", "halt", "read:0x20000000:32", "resume"]
                )
                self.assertEqual(self.recorded, [ToolOutcome.SUCCESS])

    def test_state_or_halt_failure_prevents_read_and_resume(self) -> None:
        for kwargs, message, expected in (
            ({"state_error": RuntimeError("state lost")}, "state lost", ["state"]),
            ({"halt_error": RuntimeError("halt lost")}, "halt lost", ["state", "halt"]),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(RuntimeError, message):
                    self._symbol_read(self._services(**kwargs))
                self.assertEqual(self.calls, expected)
                self.assertEqual(self.recorded, [])
                self.calls.clear()

    def test_read_failure_restores_execution_and_retains_primary_failure(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "read failed"):
            self._symbol_read(self._services(read_error=RuntimeError("read failed")))

        self.assertEqual(self.calls, ["state", "halt", "read:0x20000000:32", "resume"])
        self.assertEqual(self.recorded, [])

    def test_keyboard_interrupt_read_failure_restores_and_reraises_the_same_primary(self) -> None:
        primary = KeyboardInterrupt("read interrupted")

        with self.assertRaises(KeyboardInterrupt) as raised:
            self._symbol_read(self._services(read_error=primary))

        self.assertIs(raised.exception, primary)
        self.assertEqual(self.calls, ["state", "halt", "read:0x20000000:32", "resume"])
        self.assertEqual(self.recorded, [])

    def test_restore_failure_cannot_return_or_record_success(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "resume failed"):
            self._symbol_read(self._services(resume_error=RuntimeError("resume failed")))

        self.assertEqual(self.calls, ["state", "halt", "read:0x20000000:32", "resume"])
        self.assertNotIn(ToolOutcome.SUCCESS, self.recorded)

    def test_dual_read_and_restore_failure_keeps_both_facts(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError, r"ValueError: read failed; execution restoration failed with OSError: resume failed"
        ):
            self._symbol_read(
                self._services(
                    read_error=ValueError("read failed"), resume_error=OSError("resume failed")
                )
            )

        self.assertEqual(self.calls, ["state", "halt", "read:0x20000000:32", "resume"])
        self.assertEqual(self.recorded, [])

    def test_dual_base_exception_failure_keeps_both_facts_and_chains_primary(self) -> None:
        primary = KeyboardInterrupt("read interrupted")
        restoration = SystemExit("resume interrupted")

        with self.assertRaisesRegex(
            RuntimeError,
            r"KeyboardInterrupt: read interrupted; execution restoration failed with "
            r"SystemExit: resume interrupted",
        ) as raised:
            self._symbol_read(self._services(read_error=primary, resume_error=restoration))

        self.assertIs(raised.exception.__cause__, primary)
        self.assertEqual(self.calls, ["state", "halt", "read:0x20000000:32", "resume"])
        self.assertEqual(self.recorded, [])

    def test_pre_io_refusals_and_raw_reads_do_not_acquire_lifecycle_behavior(self) -> None:
        services = self._services()
        handlers = build_memory_handlers(services)
        with patch("pyocd_debug_mcp.tools.memory.is_elf_artifact", return_value=True):
            refused = handlers["read_memory_symbol"]("board", "counter", 64, str(self.artifact))
        self.assertIn("memory/invalid-width", refused)
        self.assertEqual(self.calls, [])

        result = handlers["read_memory_address"]("board", 0x20000000, 32)
        self.assertIn("0x00001234", result)
        self.assertEqual(self.calls, ["read:0x20000000:32"])

    def test_production_wiring_and_published_help_expose_the_exact_contract(self) -> None:
        self.assertIs(server.memory_services.get_state, target_control.get_state)
        self.assertIs(server.memory_services.halt, target_control.halt)
        self.assertIs(server.memory_services.resume, target_control.resume)

        help_text = server.memory_tool_handlers["read_memory_symbol"].__doc__ or ""
        for phrase in ("running or sleeping", "briefly halted", "restored", "already halted"):
            self.assertIn(phrase, help_text)
        self.assertIn("reported honestly", help_text)
        self.assertIn("reconnect", help_text)
        for parameter in ("`board_id`", "`symbol`", "`width`", "`elf_artifact`"):
            self.assertIn(parameter, help_text)
        self.assertIn(
            '`read_memory_symbol("board-a", "scheduler_tick_count", 32, "build/app.elf")`',
            help_text,
        )
