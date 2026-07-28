"""Adversarial contract tests for A21 lifecycle-coherent scalar writes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp import server
from pyocd_debug_mcp.guardrails import plan_defs
from pyocd_debug_mcp.services import target_control
from pyocd_debug_mcp.services.session_runtime import ToolOutcome
from pyocd_debug_mcp.services.symbols import ResolvedSymbol
from pyocd_debug_mcp.tools.memory import (
    MemoryToolServices,
    build_memory_handlers,
)


class WriteMemoryCoherenceSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []
        self.events: list[ToolOutcome] = []
        self.handle = object()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.artifact = Path(self.temporary_directory.name) / "firmware.elf"
        self.artifact.write_bytes(b"fixture ELF")

    def _services(
        self,
        *,
        state: str = "RUNNING",
        observed: int = 0x1234,
        state_error: BaseException | None = None,
        halt_error: BaseException | None = None,
        write_error: BaseException | None = None,
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

        def write(_handle: object, address: int, value: int, width: int) -> None:
            self.calls.append(f"write:{address:#x}:{value:#x}:{width}")
            if write_error is not None:
                raise write_error

        def read(_handle: object, address: int, width: int) -> int:
            self.calls.append(f"read:{address:#x}:{width}")
            if read_error is not None:
                raise read_error
            return observed

        def resume(_handle: object) -> None:
            self.calls.append("resume")
            if resume_error is not None:
                raise resume_error

        def record_event(*_args: object, **kwargs: object) -> None:
            self.events.append(kwargs["outcome_kind"])  # type: ignore[arg-type]

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
            read_target_block=lambda _handle, _address, _length: [],
            write_target_memory=write,
            check_memory_read=lambda _board, _address, _length: None,
            check_memory_write=lambda _board, _address, _width: None,
            get_state=get_state,
            halt=halt,
            resume=resume,
        )

    def _raw_write(self, services: MemoryToolServices, *, width: int = 32) -> str:
        return build_memory_handlers(services)["write_memory"](
            "board", 0x20000000, 0x1234, width, allow_address_fallback=True, reason="unsymbolized"
        )

    def test_lifecycle_is_exact_for_halted_and_all_non_halted_states(self) -> None:
        for state, expected in (
            ("hAlTeD", ["state", "write:0x20000000:0x1234:32", "read:0x20000000:32"]),
            ("RUNNING", ["state", "halt", "write:0x20000000:0x1234:32", "read:0x20000000:32", "resume"]),
            ("SLEEPING", ["state", "halt", "write:0x20000000:0x1234:32", "read:0x20000000:32", "resume"]),
            ("new-provider-state", ["state", "halt", "write:0x20000000:0x1234:32", "read:0x20000000:32", "resume"]),
        ):
            with self.subTest(state=state):
                self._write_coherent(state)
                self.assertEqual(self.calls, expected)
                self.assertEqual(self.events, [ToolOutcome.SUCCESS])
                self.calls.clear()
                self.events.clear()

    def _write_coherent(self, state: str) -> None:
        result = self._raw_write(self._services(state=state))
        self.assertIn("Wrote 0x1234 to mapped RAM at 0x20000000.", result)

    def test_every_width_uses_exact_address_and_width_for_readback(self) -> None:
        for width, value, expected_text in ((8, 0x12, "0x12"), (16, 0x1234, "0x1234"), (32, 0x12345678, "0x12345678")):
            with self.subTest(width=width):
                services = self._services(observed=value)
                result = build_memory_handlers(services)["write_memory"](
                    "board", 0x20000000, value, width, allow_address_fallback=True, reason="unsymbolized"
                )
                self.assertIn(f"Wrote {expected_text} to mapped RAM at 0x20000000.", result)
                self.assertEqual(
                    self.calls,
                    ["state", "halt", f"write:0x20000000:{expected_text}:{width}", f"read:0x20000000:{width}", "resume"],
                )
                self.calls.clear()

    def test_mismatch_is_width_formatted_non_success_and_restores(self) -> None:
        for width, expected, observed, text in (
            (8, 0x12, 0x34, "expected 0x12, observed 0x34"),
            (16, 0x1234, 0x5678, "expected 0x1234, observed 0x5678"),
            (32, 0x12345678, 0x9ABCDEF0, "expected 0x12345678, observed 0x9ABCDEF0"),
        ):
            with self.subTest(width=width):
                services = self._services(observed=observed)
                with self.assertRaisesRegex(RuntimeError, text):
                    build_memory_handlers(services)["write_memory"](
                        "board", 0x20000000, expected, width, allow_address_fallback=True, reason="unsymbolized"
                    )
                self.assertEqual(self.calls[-1], "resume")
                self.assertNotIn(ToolOutcome.SUCCESS, self.events)
                self.calls.clear()

    def test_pre_halt_failures_do_not_mutate_or_restore(self) -> None:
        for kwargs, expected in (
            ({"state_error": RuntimeError("state lost")}, ["state"]),
            ({"halt_error": RuntimeError("halt lost")}, ["state", "halt"]),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(RuntimeError, "lost"):
                    self._raw_write(self._services(**kwargs))
                self.assertEqual(self.calls, expected)
                self.assertEqual(self.events, [])
                self.calls.clear()

    def test_post_halt_failures_restore_and_preserve_primary_or_dual_failure(self) -> None:
        primary = KeyboardInterrupt("write interrupted")
        with self.assertRaises(KeyboardInterrupt) as raised:
            self._raw_write(self._services(write_error=primary))
        self.assertIs(raised.exception, primary)
        self.assertEqual(self.calls, ["state", "halt", "write:0x20000000:0x1234:32", "resume"])
        self.assertEqual(self.events, [])

        self.calls.clear()
        primary = ValueError("readback failed")
        with self.assertRaisesRegex(RuntimeError, r"ValueError: readback failed; execution restoration failed with OSError: resume failed") as raised:
            self._raw_write(self._services(read_error=primary, resume_error=OSError("resume failed")))
        self.assertIs(raised.exception.__cause__, primary)
        self.assertEqual(self.calls[-1], "resume")
        self.assertEqual(self.events, [])

    def test_restoration_failure_after_verified_write_is_not_success(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "resume failed"):
            self._raw_write(self._services(resume_error=RuntimeError("resume failed")))
        self.assertEqual(self.calls[-1], "resume")
        self.assertNotIn(ToolOutcome.SUCCESS, self.events)

    def test_symbol_and_raw_routes_share_coherent_semantics_and_preflight_stays_io_free(self) -> None:
        services = self._services()
        handlers = build_memory_handlers(services)
        with patch("pyocd_debug_mcp.tools.memory.is_elf_artifact", return_value=True):
            result = handlers["write_memory"]("board", "counter", 0x1234, 32, elf_artifact=str(self.artifact))
        self.assertIn("Wrote 0x1234 to mapped RAM at counter.", result)
        self.assertEqual(self.calls, ["state", "halt", "write:0x20000000:0x1234:32", "read:0x20000000:32", "resume"])
        self.calls.clear()
        refusal = handlers["write_memory"]("board", 0x20000000, 1, 64, allow_address_fallback=True, reason="unsymbolized")
        self.assertIn("memory/invalid-width", refusal)
        self.assertEqual(self.calls, [])

    def test_production_wiring_and_help_publish_the_contract(self) -> None:
        self.assertIs(server.memory_services.get_state, target_control.get_state)
        self.assertIs(server.memory_services.halt, target_control.halt)
        self.assertIs(server.memory_services.resume, target_control.resume)
        for text in (
            server.write_memory.__doc__ or "",
            server.memory_tool_handlers["write_memory"].__doc__ or "",
            str(plan_defs._GUIDANCE["write_memory"]),
        ):
            normalized = text.lower().replace("-", " ")
            for phrase in ("running or sleeping", "readback", "already halted", "later overwrite", "reported honestly", "reconnect"):
                self.assertIn(phrase, normalized)

    def test_registered_fastmcp_write_memory_docstring_is_self_contained(self) -> None:
        """The public FastMCP entrypoint, not only its handler, must teach safe use."""

        docstring = server.write_memory.__doc__ or ""
        normalized = " ".join(docstring.lower().replace("-", " ").split())

        for parameter in (
            "board_id",
            "symbol_or_address",
            "value",
            "width",
            "allow_address_fallback",
            "reason",
            "elf_artifact",
        ):
            self.assertIn(parameter, docstring)
        for phrase in (
            "use a symbol",
            "raw address",
            "allow_address_fallback=true",
            "mapped ram",
            "8, 16, or 32",
            "example",
            "write_memory(",
            "returns",
            "wrote 0x",
            "immediate coherent mutation",
            "later overwrite",
            "invalid widths or values",
            "missing raw fallback justification",
            "unmapped or prohibited memory",
            "reported honestly",
            "inspect or reconnect",
            "current elf",
            "deliberately halted",
            "running or sleeping",
            "readback",
            "already halted",
        ):
            self.assertIn(phrase, normalized)
