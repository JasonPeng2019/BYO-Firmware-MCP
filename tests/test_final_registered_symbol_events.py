"""Independent final AF02 controls through registered MCP and real session JSONL.

The hardware boundary records effects; parsing, policy admission, dispatch,
artifact revalidation, connection ownership, and event persistence remain real.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from pyocd_debug_mcp.capabilities.lite import normalize_lite_confirmation
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.tools import memory
from tests.elf_symbol_fixture import ElfSymbol, elf32_symbols
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    public_text,
    tiered_test_server,
)


class FinalRegisteredSymbolEvents(unittest.TestCase):
    def keep_evidence(self, stage, **facts):
        destination = os.environ.get("BYO_FINAL_CASE_EVIDENCE")
        if not destination:
            return
        root = Path(destination) / self._testMethodName
        root.mkdir(parents=True, exist_ok=True)
        log = self.connection.runtime_session.log_path
        if log.is_file():
            (root / (stage + ".session.jsonl")).write_bytes(log.read_bytes())
        (root / (stage + ".facts.json")).write_text(
            json.dumps(
                {
                    "backend_calls": self.backend.calls,
                    "same_healthy_connection": self.server.connection_manager.maybe_connection(
                        self.board
                    )
                    is self.connection,
                    "runtime_closed_at": self.connection.runtime_session.closed_at,
                    **facts,
                },
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )

    def setUp(self) -> None:
        self.root = self.enterContext(isolated_project())
        self.server = self.enterContext(tiered_test_server(self.root))
        self.board = "final_symbol_events"
        confirmation = normalize_lite_confirmation(
            self.board,
            {
                "decision": "confirm-or-correct",
                "regions": [
                    {
                        "name": "final simulated SRAM",
                        "kind": "ram",
                        "start": 0x20000000,
                        "end": 0x20001000,
                        "readable": True,
                        "writable": True,
                        "executable": False,
                        "source_pages": [1],
                        "source_note": "Independent final simulated RAM declaration",
                    }
                ],
                "flash": {"backend_target": None, "erase_sectors": []},
                "recovery": None,
            },
        )
        policies = CapabilityPolicyRepository(self.root)
        policies.commit(
            self.board,
            Tier.SETUP_LITE,
            map_snapshot=confirmation.map_snapshot,
            evidence=confirmation.evidence,
        )
        self.backend = DeterministicTargetBackend()
        self.server.configure_tiered_test_seams(backend=self.backend, policy_repository=policies)
        handle = self.backend.open(board=None, unique_id="final-simulated", target="nrf52840")
        self.server._promote_open_session(self.board, handle, gate_reason="final event control")
        self.addCleanup(self.server.disconnect, self.board)
        self.connection = self.server.connection_manager.maybe_connection(self.board)
        self.assertIsNotNone(self.connection)
        self.path = self.root / "final-events.elf"
        self.path.write_bytes(elf32_symbols([ElfSymbol("final_value", 0x20000024)]))
        self.backend.calls.clear()
        self.addCleanup(self.keep_evidence, "final-pre-disconnect")

    def call_read(self) -> str:
        return public_text(
            self.server,
            "read_memory_symbol",
            {
                "board_id": self.board,
                "symbol": "final_value",
                "elf_artifact": str(self.path),
            },
        )

    def events(self) -> list[dict]:
        # Read the real store while the connection is still healthy, before the
        # fixture's intentional disconnect can add its own event/backend effects.
        path = self.connection.runtime_session.log_path
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_handler_revalidation_refusal_has_stable_name_and_zero_effects(self) -> None:
        original = self.path.read_bytes()
        real_reverify = memory._reverify_symbol_artifact
        observed = []

        def reverify(*args):
            if len(observed) == 1:
                self.path.write_bytes(original + b"final independent mutation")
            result = real_reverify(*args)
            observed.append((threading.get_ident(), result))
            return result

        with patch.object(memory, "_reverify_symbol_artifact", side_effect=reverify):
            response = self.call_read()
        self.assertEqual(len(observed), 2)
        self.assertIsNone(observed[0][1])
        self.assertEqual(observed[1][1].code, "memory/symbol-artifact-unavailable")
        self.assertIn("symbol-artifact-unavailable", response)
        self.assertEqual(self.backend.calls, [])
        self.assertIs(self.server.connection_manager.maybe_connection(self.board), self.connection)
        self.assertIsNone(self.connection.runtime_session.closed_at)
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["tool_name"], "read_memory_symbol")
        self.assertEqual(events[0]["error_code"], "memory/symbol-artifact-unavailable")
        self.assertEqual(events[0]["outcome_kind"], "refused")
        self.assertEqual(events[0]["normalized_args"]["symbol"], "final_value")
        self.keep_evidence(
            "refused-before-fixture-cleanup",
            response=response,
            verifier_calls=[
                {"thread_id": thread, "code": getattr(result, "code", None)}
                for thread, result in observed
            ],
        )
        # A subsequent supported call proves the refused read left the session
        # usable. This modifies only this test's disposable ELF artifact.
        self.path.write_bytes(original)
        self.assertIn("value=0x12345678", self.call_read())
        self.assertEqual([c[0] for c in self.backend.calls], ["read_memory", "release_reset"])
        self.assertEqual(self.events()[-1]["outcome_kind"], "success")
        self.keep_evidence("healthy-subsequent-read")

    def test_independently_reached_ordinary_named_success_uses_real_store(self) -> None:
        response = self.call_read()
        self.assertIn("value=0x12345678", response)
        self.assertIn("20000024", response.lower())
        self.assertEqual([c[0] for c in self.backend.calls], ["read_memory", "release_reset"])
        self.assertEqual(self.backend.calls[0][2], 0x20000024)
        self.assertIs(self.server.connection_manager.maybe_connection(self.board), self.connection)
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["tool_name"], "read_memory_symbol")
        self.assertEqual(events[0]["outcome_kind"], "success")
        self.assertIsNone(events[0]["error_code"])
        self.assertEqual(events[0]["normalized_args"]["symbol"], "final_value")
        self.keep_evidence("independent-named-success", response=response)


if __name__ == "__main__":
    unittest.main()
