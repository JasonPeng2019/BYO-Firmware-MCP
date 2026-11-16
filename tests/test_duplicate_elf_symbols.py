"""VI10: original ELF definitions, public refusals and address recovery."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.capabilities.lite import normalize_lite_confirmation
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.tools import memory
from tests.elf_symbol_fixture import FUNC, TEXT, ElfSymbol, elf32_symbols
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    public_text,
    tiered_test_server,
)


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

    def invoke(self, name: str, parameters: dict, *, permission: str | None = None) -> str:
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
                    **({"user_permission": permission} if permission is not None else {}),
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


    def test_registered_execution_and_register_bindings(self) -> None:
        # Exercise registration, schema conversion, guards and real composition
        # services. Calling the worker dispatcher alone misses these bindings.
        direct = (
            ("halt", {}, ("halt",)),
            ("resume", {}, ("resume",)),
            ("step", {}, ("step", "read_core_register")),
            ("reset_and_run", {}, ("reset",)),
            ("get_state", {}, ("get_state",)),
            ("read_cpu_register", {"name": "R0"}, ("read_core_register",)),
            ("read_execution_state", {"name": "PC"}, ("read_core_register",)),
        )
        for name, parameters, expected in direct:
            with self.subTest(tool=name):
                self.backend.calls.clear()
                result = public_text(self.server, name, {"board_id": self.board, **parameters})
                self.assertNotIn("Refused", result)
                calls = [call for call in self.backend.calls if call[0] != "release_reset"]
                self.assertEqual(tuple(call[0] for call in calls), expected, result)
                self.assertTrue(all(call[1] == "vi10-simulated" for call in calls))
                if name.startswith("read_"):
                    self.assertEqual(calls[-1][2], parameters["name"].lower())
        for name, parameters, permission, expected in (
            ("write_cpu_register", {"name": "R0", "value": "0x1234"}, None,
             ("write_core_register", "vi10-simulated", "r0", 0x1234)),
            ("set_execution_state", {"name": "PC", "value": "0x08000009"}, "one-time",
             ("write_core_register", "vi10-simulated", "pc", 0x08000009)),
            ("reset_and_halt", {}, None, ("reset_and_halt", "vi10-simulated")),
        ):
            with self.subTest(tool=name):
                self.backend.calls.clear()
                result = self.invoke(name, parameters, permission=permission)
                self.assertNotIn("Refused", result)
                self.assertIn(expected, self.backend.calls, result)
        for name, register in (("read_cpu_register", "pc"),
                               ("read_execution_state", "r0"),
                               ("read_cpu_register", "not_a_register")):
            with self.subTest(refused=name, register=register):
                self.backend.calls.clear()
                result = public_text(self.server, name, {"board_id": self.board, "name": register})
                self.assertIn("Refused", result)
                self.assertFalse(any(call[0] == "read_core_register" for call in self.backend.calls))

    def test_registered_connection_and_disconnect_bindings(self) -> None:
        board = "public_connection"
        self.backend.calls.clear()
        result = public_text(self.server, "connect", {
            "board_id": board, "probe_uid": "public-probe", "target": "nrf52840",
        })
        self.assertNotIn("Refused", result)
        self.addCleanup(self.server.disconnect, board)
        opened = [call for call in self.backend.calls if call[0] == "open"]
        self.assertEqual(len(opened), 1, result)
        self.assertEqual(opened[0][1]["unique_id"], "public-probe")
        self.assertEqual(opened[0][1]["target"], "nrf52840")
        public_text(self.server, "get_board_info", {"board_id": board})
        self.backend.calls.clear()
        result = public_text(self.server, "disconnect", {"board_id": board})
        self.assertNotIn("Refused", result)
        self.assertIn(("close", "public-probe"), self.backend.calls)
        self.assertIsNone(self.server.connection_manager.maybe_connection(board))

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
