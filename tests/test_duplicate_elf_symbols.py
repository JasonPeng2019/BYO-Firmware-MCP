"""VI10: original ELF definitions, public refusals and address recovery."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection
from mcp.server.fastmcp.exceptions import ToolError
from pyocd.debug.elf.elf import ELFBinaryFile

from pyocd_debug_mcp.capabilities.lite import normalize_lite_confirmation
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.services import symbols, target_control
from pyocd_debug_mcp.services.symbols import ResolvedSymbol, find_symbols, resolve_symbol
from pyocd_debug_mcp.target_errors import SymbolLookupError, TargetConnectionError
from pyocd_debug_mcp.tools.breakpoints import BreakpointToolServices, build_breakpoint_handlers
from pyocd_debug_mcp.tools.memory import MemoryToolServices, build_memory_handlers
from pyocd_debug_mcp.tools import memory
from tests.elf_symbol_fixture import (
    ABS,
    BSS,
    COMMON,
    FILE,
    FUNC,
    GLOBAL,
    LOCAL,
    NOTYPE,
    SECTION,
    TEXT,
    UNDEF,
    WEAK,
    ElfSymbol,
    duplicate_counter_elf,
    elf32_symbols,
)
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    public_text,
    tiered_test_server,
)


class OriginalElfDefinitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "symbols.axf"

    def image(self, entries: list[ElfSymbol], **kwargs: object) -> Path:
        self.path.write_bytes(elf32_symbols(entries, **kwargs))
        return self.path

    def test_valid_elf_preserves_two_local_definitions_that_pyocd_loses(self) -> None:
        self.path.write_bytes(duplicate_counter_elf())
        with self.path.open("rb") as stream:
            elf = ELFFile(stream)
            self.assertEqual(elf.header["e_type"], "ET_EXEC")
            self.assertEqual(elf.header["e_machine"], "EM_ARM")
            self.assertEqual(elf.elfclass, 32)
            table = elf.get_section_by_name(".symtab")
            self.assertIsInstance(table, SymbolTableSection)
            definitions = list(table.iter_symbols())[1:]
            self.assertEqual([item["st_info"]["bind"] for item in definitions], ["STB_LOCAL"] * 2)
            self.assertEqual([item["st_value"] for item in definitions], [0x20000000, 0x20000004])
            self.assertEqual(table["sh_info"], 3)
        original = ELFBinaryFile(str(self.path))
        try:
            self.assertEqual(len(original.symbol_decoder.symbol_dict), 1)
            self.assertEqual(
                original.symbol_decoder.get_symbol_for_name("counter").address, 0x20000004
            )
        finally:
            original.close()
        expected = (
            ResolvedSymbol("counter", 0x20000000, 4, "STT_OBJECT"),
            ResolvedSymbol("counter", 0x20000004, 4, "STT_OBJECT"),
        )
        self.assertEqual(find_symbols(self.path, "COUNTER"), expected)
        self.assertEqual(find_symbols(self.path, "  count  "), expected)
        with self.assertRaises(SymbolLookupError) as raised:
            resolve_symbol(self.path, "counter")
        self.assertIn("ambiguous", str(raised.exception))
        self.assertIn("0x20000000", str(raised.exception))
        self.assertIn("0x20000004", str(raised.exception))
        self.assertIn("explicit address", str(raised.exception))

    def test_unique_fields_and_raw_thumb_values_match_actual_pyocd(self) -> None:
        self.image(
            [
                ElfSymbol("counter", 0x20000000, binding=GLOBAL),
                ElfSymbol("zero", 0x20000008, size=0),
                ElfSymbol("entry", 0x08000001, type=FUNC, section=TEXT, binding=WEAK),
            ]
        )
        original = ELFBinaryFile(str(self.path))
        try:
            for name in ("counter", "zero", "entry"):
                with self.subTest(name=name):
                    old = original.symbol_decoder.get_symbol_for_name(name)
                    self.assertEqual(
                        resolve_symbol(self.path, name),
                        ResolvedSymbol(old.name, old.address, old.size, old.type),
                    )
        finally:
            original.close()

    def test_undefined_imports_and_common_do_not_become_address_definitions(self) -> None:
        self.image(
            [
                ElfSymbol("counter", 0, section=UNDEF, binding=GLOBAL),
                ElfSymbol("counter", 0x20000000, binding=GLOBAL),
                ElfSymbol("import_only", 0, section=UNDEF, binding=WEAK),
                ElfSymbol("common", 4, section=COMMON, binding=GLOBAL),
                ElfSymbol("absolute", 0x20000010, section=ABS, binding=GLOBAL),
                ElfSymbol("at_zero", 0, section=ABS, binding=GLOBAL),
            ]
        )
        self.assertEqual(resolve_symbol(self.path, "counter").address, 0x20000000)
        self.assertEqual(resolve_symbol(self.path, "absolute").address, 0x20000010)
        self.assertEqual(resolve_symbol(self.path, "at_zero").address, 0)
        for name in ("import_only", "common"):
            with self.subTest(name=name):
                self.assertEqual(find_symbols(self.path, name), ())
                with self.assertRaisesRegex(SymbolLookupError, "not found"):
                    resolve_symbol(self.path, name)

    def test_existing_function_object_admission_is_preserved(self) -> None:
        self.image(
            [
                ElfSymbol("admit_object", 0x20000000),
                ElfSymbol("admit_function", 0x08000001, type=FUNC, section=TEXT),
                ElfSymbol("admit_notype", 0x20000008, type=NOTYPE),
                ElfSymbol("admit_section", 0x20000000, type=SECTION, size=0),
                ElfSymbol("admit_file", 0, type=FILE, size=0, section=ABS),
            ]
        )
        self.assertEqual(
            [item.name for item in find_symbols(self.path, "admit")],
            ["admit_function", "admit_object"],
        )

    def test_same_definition_in_symtab_and_dynsym_is_not_ambiguous(self) -> None:
        entries = [
            ElfSymbol("counter", 0x20000000, binding=GLOBAL),
            ElfSymbol("alias", 0x20000000, binding=WEAK),
            ElfSymbol("zero", 0x20000010, size=0, binding=GLOBAL),
            ElfSymbol("entry", 0x08000001, type=FUNC, section=TEXT, binding=GLOBAL),
        ]
        self.image(entries, dynamic=list(reversed(entries)))
        for entry in entries:
            with self.subTest(name=entry.name):
                self.assertEqual(len(find_symbols(self.path, entry.name)), 1)
                self.assertEqual(resolve_symbol(self.path, entry.name).address, entry.address)
        self.assertEqual(
            resolve_symbol(self.path, "counter").address, resolve_symbol(self.path, "alias").address
        )

    def test_distinct_definition_contributed_by_dynsym_is_retained(self) -> None:
        self.image(
            [ElfSymbol("counter", 0x20000000, binding=GLOBAL)],
            dynamic=[
                ElfSymbol("counter", 0x20000000, binding=GLOBAL),
                ElfSymbol("counter", 0x20000004, binding=GLOBAL),
                ElfSymbol("dynamic_only", 0x20000008, binding=GLOBAL),
            ],
        )
        self.assertEqual(
            [item.address for item in find_symbols(self.path, "counter")], [0x20000000, 0x20000004]
        )
        self.assertEqual(resolve_symbol(self.path, "dynamic_only").address, 0x20000008)
        with self.assertRaisesRegex(SymbolLookupError, "ambiguous"):
            resolve_symbol(self.path, "counter")

    def test_dynsym_only_and_nonstandard_table_names_are_not_dropped(self) -> None:
        entry = ElfSymbol("counter", 0x20000000, binding=GLOBAL)
        for options in ({"symtab": False, "dynamic": [entry]}, {"symtab_name": ".symbols"}):
            with self.subTest(options=options):
                self.image([entry], **options)
                self.assertEqual(resolve_symbol(self.path, "counter").address, entry.address)
                self.assertEqual(len(find_symbols(self.path, "counter")), 1)

    def test_definition_identity_keeps_section_and_binding_but_not_table_membership(self) -> None:
        entries = [
            ElfSymbol("counter", 0x20000200, section=ABS, binding=LOCAL),
            ElfSymbol("counter", 0x20000200, section=ABS, binding=GLOBAL),
            ElfSymbol("counter", 0x20000200, section=BSS, binding=LOCAL),
        ]
        self.image(entries, dynamic=[entries[1]])
        self.assertEqual(len(find_symbols(self.path, "counter")), 3)
        with self.assertRaisesRegex(SymbolLookupError, "ambiguous"):
            resolve_symbol(self.path, "counter")

    def test_aliases_and_identical_repeated_representations(self) -> None:
        counter = ElfSymbol("counter", 0x20000000)
        self.image([counter, counter, replace(counter, name="alias")])
        self.assertEqual(len(find_symbols(self.path, "counter")), 1)
        self.assertEqual(
            resolve_symbol(self.path, "counter").address, resolve_symbol(self.path, "alias").address
        )

    def test_sorting_is_complete_and_independent_of_entry_order(self) -> None:
        entries = [
            ElfSymbol("Counter", 0x20000004),
            ElfSymbol("counter", 0x20000000, size=8),
            ElfSymbol("counter", 0x20000000, size=4),
            ElfSymbol("counter", 0x20000000, type=FUNC),
            ElfSymbol("counter_extra", 0x20000008),
        ]
        self.image(entries)
        first = find_symbols(self.path, "count")
        self.image(list(reversed(entries)))
        self.assertEqual(find_symbols(self.path, "COUNT"), first)
        self.assertEqual(
            [(item.name, item.address, item.size, item.type) for item in first],
            [
                ("counter", 0x20000000, 4, "STT_FUNC"),
                ("counter", 0x20000000, 4, "STT_OBJECT"),
                ("counter", 0x20000000, 8, "STT_OBJECT"),
                ("Counter", 0x20000004, 4, "STT_OBJECT"),
                ("counter_extra", 0x20000008, 4, "STT_OBJECT"),
            ],
        )

    def test_exact_name_query_and_relative_project_path_contracts(self) -> None:
        self.image([ElfSymbol("Counter", 0x20000000), ElfSymbol("counter_extra", 0x20000004)])
        with patch.dict(os.environ, {"BYO_MCP_ARTIFACT_ROOT": str(self.root)}):
            self.assertEqual(resolve_symbol("symbols.axf", "Counter").address, 0x20000000)
            self.assertEqual(len(find_symbols("symbols.axf", "counter")), 2)
            self.assertEqual(find_symbols(self.path, "*Counter*"), ())
        for name in ("counter", "Counter ", "count", "missing"):
            with self.subTest(name=name), self.assertRaisesRegex(SymbolLookupError, "not found"):
                resolve_symbol(self.path, name)

    def test_missing_empty_and_malformed_controls_close_real_streams(self) -> None:
        with self.assertRaisesRegex(SymbolLookupError, "must not be empty"):
            find_symbols(self.path, " ")
        with self.assertRaisesRegex(SymbolLookupError, "does not exist"):
            resolve_symbol(self.path, "counter")
        opened = []
        original_open = Path.open

        def tracked_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            opened.append(stream)
            return stream

        self.path.write_bytes(duplicate_counter_elf())
        with patch.object(Path, "open", tracked_open):
            find_symbols(self.path, "counter")
            with self.assertRaises(SymbolLookupError):
                resolve_symbol(self.path, "counter")
        self.path.write_bytes(b"not ELF")
        with patch.object(Path, "open", tracked_open), self.assertRaises(ELFError):
            find_symbols(self.path, "counter")
        self.assertEqual(len(opened), 3)
        self.assertTrue(all(stream.closed for stream in opened))


class MemoryAndSharedCallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "symbols.elf"
        self.path.write_bytes(
            elf32_symbols(
                [
                    ElfSymbol("counter", 0x20000000),
                    ElfSymbol("counter", 0x20000004),
                    ElfSymbol("unique", 0x20000008),
                    ElfSymbol("zero", 0x2000000C, size=0),
                    ElfSymbol("tiny", 0x20000010, size=1),
                    ElfSymbol("unaligned", 0x20000012),
                    ElfSymbol("entry", 0x08000001, type=FUNC, section=TEXT),
                ]
            )
        )
        self.touches: list[tuple] = []
        self.events: list[dict] = []
        self.handle = SimpleNamespace()
        self.services = MemoryToolServices(
            runtime_for=lambda _: None,
            active_session_id=lambda _: "test-session",
            duration_ms=lambda _: 0,
            record_event=lambda *args, **kwargs: self.events.append(kwargs),
            format_refusal=lambda refusal, **_: f"Refused [{refusal.code}]: {refusal.message}",
            handle_for=lambda _: self.handle,
            symbol_artifact_for=lambda _: self.path,
            find_symbols=find_symbols,
            resolve_symbol=resolve_symbol,
            read_target_memory=self.read,
            read_target_block=lambda *args: self.touches.append(("block", *args)) or [],
            write_target_memory=lambda *args: self.touches.append(("write", *args)),
            check_memory_read=lambda *args: self.touches.append(("check-read", *args)),
            check_memory_write=lambda *args: self.touches.append(("check-write", *args)),
        )
        self.handlers = build_memory_handlers(self.services)

    def read(self, *args):
        self.touches.append(("read", *args))
        return 0x12345678

    def test_public_find_and_ambiguous_reads_writes_refuse_without_safety_or_backend_io(
        self,
    ) -> None:
        for explicit in (None, str(self.path)):
            with self.subTest(elf=explicit):
                found = self.handlers["find_symbol"]("board", "COUNTER", explicit)
                self.assertIn("counter@0x20000000", found)
                self.assertIn("counter@0x20000004", found)
                read = self.handlers["read_memory_symbol"](
                    "board", "counter", elf_artifact=explicit
                )
                write = self.handlers["write_memory"]("board", "counter", 7, elf_artifact=explicit)
                for text in (read, write):
                    self.assertIn("Refused", text)
                    self.assertIn("ambiguous", text)
                    self.assertIn("0x20000004", text)
                self.assertEqual(self.touches, [])

    def test_unique_read_write_and_explicit_address_recovery_reach_backend(self) -> None:
        result = self.handlers["read_memory_symbol"]("board", "unique")
        self.assertIn("value=0x12345678", result)
        self.assertEqual(self.touches[-1], ("read", self.handle, 0x20000008, 32))
        self.handlers["write_memory"]("board", "unique", 9)
        self.assertEqual(self.touches[-1], ("write", self.handle, 0x20000008, 9, 32))
        for address in (0x20000000, "0x20000004"):
            with self.subTest(address=address):
                self.handlers["read_memory_address"]("board", address)
                self.assertEqual(self.touches[-1][0], "read")
                self.handlers["write_memory"](
                    "board",
                    address,
                    11,
                    allow_address_fallback=True,
                    reason="Selected one duplicate counter address from the ELF search",
                )
                self.assertEqual(self.touches[-1][0], "write")

    def test_scalar_width_alignment_and_size_checks_still_precede_backend(self) -> None:
        for name, code in (
            ("entry", "symbol-is-function"),
            ("zero", "symbol-size-unknown"),
            ("tiny", "symbol-width-exceeds-object"),
            ("unaligned", "symbol-address-unaligned"),
        ):
            with self.subTest(name=name):
                self.assertIn(code, self.handlers["read_memory_symbol"]("board", name))
                self.assertIn(code, self.handlers["write_memory"]("board", name, 1))
        self.assertIn(
            "invalid-width", self.handlers["read_memory_symbol"]("board", "unique", width=64)
        )
        self.assertIn(
            "value-out-of-range", self.handlers["write_memory"]("board", "unique", 1 << 32)
        )
        self.assertIn(
            "symbol-first-required", self.handlers["write_memory"]("board", 0x20000004, 1)
        )
        self.assertIn(
            "address-fallback-reason-required",
            self.handlers["write_memory"]("board", 0x20000004, 1, allow_address_fallback=True),
        )
        self.assertEqual(self.touches, [])

    def test_reverification_overrides_ambiguity_when_the_artifact_changed(self) -> None:
        def changing_resolver(path, name):
            try:
                return resolve_symbol(path, name)
            finally:
                path.write_bytes(path.read_bytes() + b"changed")

        handler = build_memory_handlers(replace(self.services, resolve_symbol=changing_resolver))
        self.assertIn(
            "symbol-artifact-unavailable", handler["read_memory_symbol"]("board", "counter")
        )
        self.assertEqual(self.touches, [])

    def test_real_parse_errors_and_missing_symbols_are_public_refusals(self) -> None:
        self.assertIn("symbol-not-found", self.handlers["read_memory_symbol"]("board", "missing"))
        self.path.write_bytes(b"\x7fELFtruncated")
        for name, args in (
            ("find_symbol", ("board", "counter")),
            ("read_memory_symbol", ("board", "counter")),
            ("write_memory", ("board", "counter", 1)),
        ):
            with self.subTest(name=name):
                self.assertIn("symbol-artifact-parse-failed", self.handlers[name](*args))
        self.assertEqual(self.touches, [])

    def test_breakpoint_shared_callback_refuses_ambiguity_and_preserves_thumb_numeric_paths(
        self,
    ) -> None:
        services = BreakpointToolServices(
            runtime_for=self.services.runtime_for,
            active_session_id=self.services.active_session_id,
            duration_ms=self.services.duration_ms,
            record_event=self.services.record_event,
            format_refusal=self.services.format_refusal,
            handle_for=self.services.handle_for,
            resolve_symbol=resolve_symbol,
            set_target_breakpoint=lambda *args: self.touches.append(("breakpoint", *args)),
            remove_target_breakpoint=lambda *args: self.touches.append(("remove", *args)),
            check_breakpoint=lambda *args: self.touches.append(("check-breakpoint", *args)),
        )
        handler = build_breakpoint_handlers(services)["set_breakpoint"]
        with self.assertRaisesRegex(SymbolLookupError, "ambiguous"):
            handler("board", "counter", str(self.path))
        self.assertEqual(self.touches, [])
        for value in ("entry", 0x08000001, "0x08000001"):
            with self.subTest(value=value):
                handler("board", value, str(self.path))
                self.assertEqual(self.touches[-1], ("breakpoint", self.handle, 0x08000000))

    def test_legacy_helper_ambiguity_precedes_even_get_state(self) -> None:
        with ExitStack() as stack:
            sentinels = [
                stack.enter_context(patch.object(symbols.target_control, method))
                for method in ("get_state", "halt", "read_memory", "resume")
            ]
            with self.assertRaisesRegex(SymbolLookupError, "ambiguous"):
                symbols.read_symbol_u32(self.handle, self.path, "counter")
            for sentinel in sentinels:
                sentinel.assert_not_called()

    def test_legacy_unique_helper_retains_halt_read_resume_behavior(self) -> None:
        with (
            patch.object(
                symbols.target_control,
                "get_state",
                side_effect=lambda *_: self.touches.append(("state",)) or "RUNNING",
            ),
            patch.object(
                symbols.target_control,
                "halt",
                side_effect=lambda *_: self.touches.append(("halt",)),
            ),
            patch.object(symbols.target_control, "read_memory", side_effect=self.read),
            patch.object(
                symbols.target_control,
                "resume",
                side_effect=lambda *_: self.touches.append(("resume",)),
            ),
        ):
            result = symbols.read_symbol_u32(self.handle, self.path, "unique")
        self.assertEqual(result.value_u32, 0x12345678)
        self.assertEqual([item[0] for item in self.touches], ["state", "halt", "read", "resume"])


class RegisteredMcpSymbolTests(unittest.TestCase):
    """Real MCP dispatch/plans/containment with a recording hardware boundary."""

    def setUp(self) -> None:
        self.root = self.enterContext(isolated_project())
        self.server = self.enterContext(tiered_test_server(self.root))
        self.board = "vi10_lite"
        self.backend = DeterministicTargetBackend()
        confirmation = normalize_lite_confirmation(
            self.board,
            {
                "decision": "confirm-or-correct",
                "regions": [
                    {
                        "name": "simulated SRAM",
                        "kind": "ram",
                        "start": 0x20000000,
                        "end": 0x20001000,
                        "readable": True,
                        "writable": True,
                        "executable": False,
                        "source_pages": [1],
                        "source_note": "Explicit simulated VI10 RAM geometry",
                    },
                    {
                        "name": "simulated code",
                        "kind": "application_flash",
                        "start": 0x08000000,
                        "end": 0x08001000,
                        "readable": True,
                        "writable": False,
                        "executable": True,
                        "source_pages": [2],
                        "source_note": "Explicit simulated VI10 code geometry",
                    },
                ],
                "flash": {
                    "backend_target": "nrf52840",
                    "erase_sectors": [{"start": 0x08000000, "end": 0x08001000}],
                },
                "recovery": None,
            },
        )
        self.policies = CapabilityPolicyRepository(self.root)
        self.policies.commit(
            self.board,
            Tier.SETUP_LITE,
            map_snapshot=confirmation.map_snapshot,
            evidence=confirmation.evidence,
        )
        self.server.configure_tiered_test_seams(
            backend=self.backend, policy_repository=self.policies
        )
        handle = self.backend.open(board=None, unique_id="vi10-simulated", target="nrf52840")
        self.server._promote_open_session(self.board, handle, gate_reason="VI10 recording backend")
        self.addCleanup(self.server.disconnect, self.board)
        self.path = self.root / "symbols.elf"
        self.path.write_bytes(
            elf32_symbols(
                [
                    ElfSymbol("counter", 0x20000000),
                    ElfSymbol("counter", 0x20000004),
                    ElfSymbol("unique", 0x20000008),
                    ElfSymbol("entry", 0x08000001, type=FUNC, section=TEXT),
                    ElfSymbol("entry", 0x08000005, type=FUNC, section=TEXT),
                    ElfSymbol("unique_entry", 0x08000009, type=FUNC, section=TEXT),
                ]
            )
        )
        self.backend.calls.clear()

    def invoke(self, name: str, parameters: dict) -> str:
        plan_name = name + "-plan"
        public_text(
            self.server,
            plan_name,
            dict.fromkeys(
                (
                    "board_id",
                    "hypothesis",
                    "strategy",
                    "hypothesis_made",
                    "strategy_evaluated",
                    "expected_fail_return",
                    "expected_success_return",
                    "max_calls",
                    "max_calls_buffer",
                    "action_parameters",
                    "user_permission",
                )
            ),
        )
        accepted = json.loads(
            public_text(
                self.server,
                plan_name,
                {
                    "board_id": self.board,
                    "hypothesis": "The ELF candidates identify the intended mapped target location",
                    "strategy": "Resolve the exact symbol before any simulated target operation",
                    "hypothesis_made": True,
                    "strategy_evaluated": True,
                    "expected_fail_return": "Ambiguous names refuse before any target state access",
                    "expected_success_return": "Unique or selected numeric address reaches the recording backend",
                    "max_calls": 1,
                    "max_calls_buffer": 0,
                    "action_parameters": parameters,
                },
            )
        )
        self.assertEqual(accepted["status"], "plan_accepted", accepted)
        try:
            return public_text(self.server, name, {"board_id": self.board, **parameters})
        except ToolError as exc:
            return str(exc)

    def write_parameters(self, target, **extra) -> dict:
        return {
            "symbol_or_address": target,
            "value": 7,
            "width": 32,
            "allow_address_fallback": False,
            "reason": None,
            "elf_artifact": str(self.path),
            **extra,
        }

    def read_symbol(self, symbol="unique", **extra) -> str:
        return public_text(
            self.server,
            "read_memory_symbol",
            {"board_id": self.board, "symbol": symbol, "elf_artifact": str(self.path), **extra},
        )

    def assert_read_refusal(self, expected: str, symbol="unique", **extra) -> str:
        codes = []
        try:
            result = self.read_symbol(symbol, **extra)
        except ToolError as exc:
            result = str(exc)
            current = exc
            seen = set()
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                codes.append(str(getattr(current, "code", "")))
                current = current.__cause__ or current.__context__
        self.assertEqual(self.backend.calls, [])
        self.assertIsNotNone(self.server.connection_manager.maybe_connection(self.board))
        self.assertTrue(
            expected in result or any(expected in code for code in codes), (result, codes)
        )
        return result

    def test_registered_surface_and_find_read_ambiguity_before_all_backend_calls(self) -> None:
        names = {tool.name for tool in asyncio.run(self.server.mcp.list_tools())}
        self.assertIn("find_symbol", names)
        self.assertIn("read_memory_symbol", names)
        self.assertNotIn("read_symbol_u32", self.server.mcp._tool_manager._tools)
        self.assertNotIn("set_breakpoint", names)  # Hidden until the plan handshake.
        found = public_text(
            self.server,
            "find_symbol",
            {
                "board_id": self.board,
                "query": "counter",
                "elf_artifact": str(self.path),
            },
        )
        self.assertIn("counter@0x20000000", found)
        self.assertIn("counter@0x20000004", found)
        refused = self.assert_read_refusal("ambiguous", "counter")
        self.assertIn("0x20000000", refused)
        self.assertIn("0x20000004", refused)
        self.assertIn("explicit address", refused)

    def test_guarded_write_ambiguity_stops_before_preparation_and_backend_io(self) -> None:
        response = self.invoke("write_memory", self.write_parameters("counter"))
        self.assertIn("ambiguous", response)
        self.assertIn("0x20000000", response)
        self.assertIn("0x20000004", response)
        self.assertEqual(self.backend.calls, [])
        self.assertIsNotNone(self.server.connection_manager.maybe_connection(self.board))

    def test_guarded_breakpoint_ambiguity_stops_before_state_or_halt(self) -> None:
        response = self.invoke(
            "set_breakpoint", {"symbol_or_address": "entry", "elf_artifact": str(self.path)}
        )
        self.assertIn("ambiguous", response)
        self.assertIn("0x08000001", response)
        self.assertIn("0x08000005", response)
        self.assertEqual(self.backend.calls, [])

    def test_registered_unique_read_without_a_plan_and_post_io_cleanup(self) -> None:
        connection = self.server.connection_manager.maybe_connection(self.board)
        self.assertIsNotNone(connection)
        response = self.read_symbol()
        self.assertIn("value=0x12345678", response)
        self.assertTrue(
            any(call[0] == "read_memory" and call[2] == 0x20000008 for call in self.backend.calls)
        )
        self.assertEqual([call[0] for call in self.backend.calls], ["read_memory", "release_reset"])
        self.assertIs(self.server.connection_manager.maybe_connection(self.board), connection)
        events = [
            json.loads(line)
            for line in connection.runtime_session.log_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["tool_name"], "read_memory_symbol")
        self.assertEqual(events[0]["outcome_kind"], "success")
        self.assertIsNone(events[0]["error_code"])
        self.assertEqual(events[0]["normalized_args"]["symbol"], "unique")

    def test_registered_guarded_prepared_symbol_write_positive_control(self) -> None:
        response = self.invoke("write_memory", self.write_parameters("unique"))
        self.assertIn("Wrote", response)
        self.assertTrue(
            any(call[0] == "write_memory" and call[2] == 0x20000008 for call in self.backend.calls)
        )

    def test_registered_missing_malformed_and_missing_name_refuse_without_cleanup_io(self) -> None:
        self.assert_read_refusal(
            "symbol-artifact-unavailable", elf_artifact=str(self.root / "gone.elf")
        )
        self.path.write_bytes(b"\x7fELF" + b"\0" * 4)
        self.assert_read_refusal("symbol-artifact-parse-failed")
        self.path.write_bytes(elf32_symbols([ElfSymbol("unique", 0x20000008)]))
        self.assert_read_refusal("symbol-not-found", "missing")

    def test_registered_scalar_and_mapped_memory_refusals_precede_all_backend_io(self) -> None:
        entries = [
            (ElfSymbol("function", 0x08000009, type=FUNC, section=TEXT), "symbol-is-function", 32),
            (ElfSymbol("unaligned", 0x20000001), "symbol-address-unaligned", 32),
            (ElfSymbol("zero", 0x20000000, size=0), "symbol-size-unknown", 32),
            (ElfSymbol("small", 0x20000000, size=1), "symbol-width-exceeds-object", 32),
            (ElfSymbol("outside", 0x30000000), "safety/", 32),
            (ElfSymbol("width", 0x20000000), "invalid-width", 64),
        ]
        for entry, expected, width in entries:
            with self.subTest(symbol=entry.name):
                self.path.write_bytes(elf32_symbols([entry]))
                self.assert_read_refusal(expected, entry.name, width=width)

    def test_registered_mutation_during_preflight_or_handler_refuses_without_reset_release(
        self,
    ) -> None:
        original = self.path.read_bytes()
        reverify = memory._reverify_symbol_artifact
        for mutate_on in (1, 2):
            with self.subTest(mutate_on=mutate_on):
                self.path.write_bytes(original)
                calls = 0
                verified = []
                connection = self.server.connection_manager.maybe_connection(self.board)
                self.assertIsNotNone(connection)
                log_path = connection.runtime_session.log_path
                event_count = (
                    len(log_path.read_text(encoding="utf-8").splitlines())
                    if log_path.exists()
                    else 0
                )

                def mutate(*args):
                    nonlocal calls
                    calls += 1
                    if calls == mutate_on:
                        self.path.write_bytes(original + b"changed after symbol parsing")
                    refusal = reverify(*args)
                    verified.append(refusal)
                    return refusal

                with patch.object(memory, "_reverify_symbol_artifact", side_effect=mutate):
                    response = self.assert_read_refusal("symbol-artifact-unavailable")
                self.assertEqual(calls, mutate_on)
                self.assertEqual(verified[-1].code, "memory/symbol-artifact-unavailable")
                self.assertIs(
                    self.server.connection_manager.maybe_connection(self.board), connection
                )
                if mutate_on == 2:
                    # Preflight passed the real verifier; only the handler's
                    # second revalidation refused. Read the actual store before
                    # fixture disconnect, which legitimately adds cleanup I/O.
                    self.assertIsNone(verified[0])
                    self.assertIn("Refused [memory/symbol-artifact-unavailable]", response)
                    self.assertIn("changed during symbol resolution", response)
                    events = [
                        json.loads(line)
                        for line in log_path.read_text(encoding="utf-8").splitlines()[event_count:]
                    ]
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["tool_name"], "read_memory_symbol")
                    self.assertEqual(events[0]["outcome_kind"], "refused")
                    self.assertEqual(events[0]["error_code"], "memory/symbol-artifact-unavailable")
                    self.assertEqual(events[0]["normalized_args"]["symbol"], "unique")
                    self.assertEqual(self.backend.calls, [])

    def test_registered_no_setup_precedes_monitor_and_local_resolution(self) -> None:
        self.policies.commit(self.board, Tier.NO_SETUP)
        with (
            patch.object(self.server._monitor, "check_block") as monitor,
            patch.object(self.server, "prepare_symbol_read") as prepare,
        ):
            self.assert_read_refusal("tier/wrong-route")
            monitor.assert_not_called()
            prepare.assert_not_called()

    def test_registered_valid_replacement_between_preflight_and_handler_is_refused(self) -> None:
        prepare = self.server.prepare_symbol_read

        def replace_after_preflight(*args, **kwargs):
            prepared = prepare(*args, **kwargs)
            self.path.write_bytes(elf32_symbols([ElfSymbol("unique", 0x20000010)]))
            return prepared

        with patch.object(self.server, "prepare_symbol_read", side_effect=replace_after_preflight):
            self.assert_read_refusal("symbol-artifact-unavailable")

    def test_registered_monitor_block_precedes_symbol_resolution(self) -> None:
        from pyocd_debug_mcp.guardrails.plan_engine import PlanRefusal

        with patch.object(
            self.server._monitor,
            "check_block",
            side_effect=PlanRefusal("monitor/blocked", "simulated stale monitor anchor"),
        ):
            self.assert_read_refusal("monitor/blocked", "counter")

    def test_registered_read_waits_for_board_serialization(self) -> None:
        lock = self.server.connection_manager.lock_for(self.board)
        finished = threading.Event()
        outcome = []

        def read():
            try:
                outcome.append(self.read_symbol())
            except BaseException as exc:
                outcome.append(exc)
            finally:
                finished.set()

        reached_preflight = threading.Event()
        original_preflight = self.server.prepare_symbol_read

        def preflight(*args, **kwargs):
            reached_preflight.set()
            return original_preflight(*args, **kwargs)

        with lock, patch.object(self.server, "prepare_symbol_read", side_effect=preflight):
            worker = threading.Thread(target=read, name="vi10-serialized-read")
            worker.start()
            try:
                self.assertFalse(reached_preflight.wait(0.05))
                self.assertFalse(finished.is_set())
                self.assertEqual(self.backend.calls, [])
            finally:
                self.addCleanup(worker.join, 5)
        self.assertTrue(finished.wait(5))
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], str)
        self.assertIn("value=0x12345678", outcome[0])

    def test_registered_read_transport_loss_evicts_stale_connection(self) -> None:
        self.backend.read_error = TargetConnectionError("VI10 simulated read transport loss")
        with self.assertRaisesRegex(ToolError, "transport loss"):
            self.read_symbol()
        self.assertIsNone(self.server.connection_manager.maybe_connection(self.board))
        self.assertIn("close", [call[0] for call in self.backend.calls])

    def test_registered_read_cancellation_keeps_owned_transport_cleanup(self) -> None:
        from pyocd_debug_mcp.kernel.operations import current_operation

        def cancelled_read(*args, **kwargs):
            operation = current_operation()
            self.assertIsNotNone(operation)
            operation.request_cancel("VI10 simulated client cancellation during target read")
            operation.checkpoint()

        with patch.object(target_control, "read_memory", side_effect=cancelled_read):
            with self.assertRaises(ToolError):
                self.read_symbol()
        self.assertIsNone(self.server.connection_manager.maybe_connection(self.board))
        self.assertIn("close", [call[0] for call in self.backend.calls])

    def test_registered_numeric_address_read_write_recovery_and_thumb_breakpoints(self) -> None:
        response = self.invoke(
            "read_memory_address", {"address": "0x20000004", "width": 32, "length": None}
        )
        self.assertIn("0x12345678", response)
        response = self.invoke(
            "write_memory",
            self.write_parameters(
                "0x20000004",
                allow_address_fallback=True,
                reason="Selected the second duplicate counter address from find_symbol",
                elf_artifact=None,
            ),
        )
        self.assertIn("Wrote", response)
        for target in ("unique_entry", "0x08000005"):
            with self.subTest(target=target):
                response = self.invoke(
                    "set_breakpoint", {"symbol_or_address": target, "elf_artifact": str(self.path)}
                )
                self.assertIn("Breakpoint set", response)
        operations = [
            call
            for call in self.backend.calls
            if call[0] in {"read_memory", "write_memory", "set_breakpoint"}
        ]
        self.assertEqual(
            [call[2] for call in operations], [0x20000004, 0x20000004, 0x08000008, 0x08000004]
        )


if __name__ == "__main__":
    unittest.main()
