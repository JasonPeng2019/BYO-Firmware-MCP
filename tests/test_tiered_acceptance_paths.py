"""AT08: indirect batch, symbol, finalizer, and lifecycle routes preserve tier policy."""

from __future__ import annotations

import asyncio
import json
import unittest

from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier

from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    assert_lite_raw_warning,
    confirmed_lite_policy,
    isolated_project,
    known_good_full_policy,
    public_call,
    public_text,
    tiered_test_server,
)


class TieredIndirectPathAcceptanceTests(unittest.TestCase):
    def test_allowed_no_setup_batch_reenters_public_dispatch_and_preserves_board_ownership(
        self,
    ) -> None:
        """AT08: a legal child uses the same raw route and cannot target a sibling board."""

        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(
                backend=backend, auto_target_resolver=lambda _board: "nrf52840"
            )
            asyncio.run(server.mcp.call_tool("connect", {"board_id": "raw_board"}))
            text = public_text(
                server,
                "action_batch",
                {
                    "board_id": "raw_board",
                    "actions": [
                        {
                            "tool_name": "read_memory_raw",
                            "arguments": {"board_id": "raw_board", "address": 0x20000000},
                        }
                    ],
                },
            )
            payload = json.loads(text.split("\n", 1)[0])
            self.assertEqual(payload["status"], "batch_completed")
            self.assertEqual(payload["completed"][0]["tool_name"], "read_memory_raw")
            self.assertTrue(any(call[0] == "read_memory" for call in backend.calls))

            with self.assertRaisesRegex(Exception, "not shared board"):
                asyncio.run(
                    server.mcp.call_tool(
                        "action_batch",
                        {
                            "board_id": "raw_board",
                            "actions": [
                                {
                                    "tool_name": "read_memory_raw",
                                    "arguments": {
                                        "board_id": "sibling_board",
                                        "address": 0x20000000,
                                    },
                                }
                            ],
                        },
                    )
                )

    def test_action_batch_child_reenters_public_tier_guard_and_cannot_smuggle_full_raw_access(
        self,
    ) -> None:
        """AT08: a batch's nested dispatcher cannot bypass the full raw refusal."""

        with isolated_project() as root, tiered_test_server(root) as server:
            policies = CapabilityPolicyRepository(root)
            profile, memory_map = known_good_full_policy()
            policies.commit(
                "legacy_full",
                Tier.SETUP_FULL,
                profile_snapshot=profile,
                map_snapshot=memory_map,
            )
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(backend=backend, policy_repository=policies)
            text = public_text(
                server,
                "action_batch",
                {
                    "board_id": "legacy_full",
                    "actions": [
                        {
                            "tool_name": "read_memory_raw",
                            "arguments": {"board_id": "legacy_full", "address": 0x20000000},
                        }
                    ],
                },
            )
            payload = json.loads(text.split("\n", 1)[0])
            self.assertEqual(payload["status"], "batch_failed")
            self.assertEqual(payload["failure"]["tool_name"], "read_memory_raw")
            self.assertIn("fully set up", payload["failure"]["message"])
            self.assertEqual(
                backend.calls, [], "denied batch child must not open or touch a backend"
            )

    def test_no_setup_symbol_route_refuses_before_symbol_resolution_or_backend_io(self) -> None:
        """AT08: symbol metadata never grants no-setup hardware authority."""

        with isolated_project() as root, tiered_test_server(root) as server:
            policies = CapabilityPolicyRepository(root)
            policies.resolve("raw_board")
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(backend=backend, policy_repository=policies)
            # ``read_memory_symbol`` predates the frozen new-tool envelope,
            # but it must still refuse by tier before looking at the bad ELF.
            with self.assertRaises(Exception) as raised:
                asyncio.run(
                    server.mcp.call_tool(
                        "read_memory_symbol",
                        {
                            "board_id": "raw_board",
                            "elf_path": str(root / "missing.elf"),
                            "symbol_name": "firmware_entry",
                        },
                    )
                )
            self.assertIn("no safety map", str(raised.exception))
            self.assertIn("raw operation", str(raised.exception))
            self.assertEqual(backend.calls, [])

    def test_raw_serial_rejects_finalizer_schema_instead_of_composing_a_safe_lifecycle(
        self,
    ) -> None:
        """AT08: raw serial has no on-exit channel, even when the caller supplies one."""

        with isolated_project() as root, tiered_test_server(root) as server:
            policies = CapabilityPolicyRepository(root)
            policies.resolve("raw_board")
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(backend=backend, policy_repository=policies)
            payload = public_call(
                server,
                "write_serial_raw",
                {
                    "board_id": "raw_board",
                    "port": "COM77",
                    "baudrate": 115200,
                    "text": "hello",
                    "on_exit": {"action": "reset_and_run"},
                },
            )
            self.assertEqual(payload["status"], "refused")
            self.assertIsInstance(payload["code"], str)
            self.assertEqual(payload["operation"], "write_serial_raw")
            self.assertIn("on_exit", payload["message"])
            self.assertEqual(backend.calls, [])

    def test_lite_raw_serial_finalizer_refusal_preserves_the_mandatory_bypass_warning(self) -> None:
        """AT08: malformed raw composition must not erase setup-lite's human warning."""

        with isolated_project() as root, tiered_test_server(root) as server:
            policies = CapabilityPolicyRepository(root)
            policies.commit(
                "lite_board",
                Tier.SETUP_LITE,
                map_snapshot=confirmed_lite_policy("lite_board"),
                evidence={"confirmed": True},
            )
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(backend=backend, policy_repository=policies)
            payload = public_call(
                server,
                "write_serial_raw",
                {
                    "board_id": "lite_board",
                    "port": "COM77",
                    "baudrate": 115200,
                    "text": "hello",
                    "on_exit": {"action": "reset_and_run"},
                },
            )
            self.assertEqual(payload["status"], "refused")
            self.assertEqual(payload["tier"], "setup-lite")
            self.assertIs(payload["raw"], True)
            assert_lite_raw_warning(self, payload, "write_serial_raw")
            self.assertIn("on_exit", payload["message"])
            self.assertEqual(backend.calls, [], "finalizer rejection must precede serial I/O")


if __name__ == "__main__":
    unittest.main()
