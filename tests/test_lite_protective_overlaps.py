"""VI01: persisted lite declarations constrain the real safe action boundary."""

from __future__ import annotations
from tests.lite_flash_support import FLASH, RAM, region

import itertools
import json
import unittest


from pyocd_debug_mcp.capabilities.lite import normalize_lite_confirmation
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.capabilities.routing import TierRouter
from pyocd_debug_mcp.safety.enforce import SafetyPolicyError
from pyocd_debug_mcp.safety.regions import ActionCategory, AddressRange
from tiered_acceptance_support import DeterministicTargetBackend, isolated_project, public_text, tiered_test_server


BOARD = "lite_overlaps"
PERIPHERAL = 0x40000000


class LiteProtectiveOverlapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.enterContext(isolated_project())
        self.server = self.enterContext(tiered_test_server(self.root))
        self.backend = DeterministicTargetBackend()
        self.server.configure_tiered_test_seams(
            backend=self.backend, auto_target_resolver=lambda _board: "nrf52840"
        )
        handle = self.backend.open(board=None, unique_id="lite-probe", target="nrf52840")
        self.server._promote_open_session(BOARD, handle, gate_reason="VI01 simulated target")
        self.addCleanup(self.server.disconnect, BOARD)

    def commit(self, rows: list[dict[str, object]]) -> None:
        confirmation = normalize_lite_confirmation(
            BOARD,
            {
                "decision": "confirm-or-correct",
                "regions": [region("ram", RAM, RAM + 0x100), *rows],
                "flash": {
                    "backend_target": "nrf52840",
                    "erase_sectors": [{"start": FLASH, "end": FLASH + 0x100}],
                },
                "recovery": None,
            },
        )
        committed = CapabilityPolicyRepository(self.root).commit(
            BOARD,
            Tier.SETUP_LITE,
            map_snapshot=confirmation.map_snapshot,
            evidence=confirmation.evidence,
        )
        # Reopen storage: neither the normalizer's object nor an in-memory state is authority.
        repository = CapabilityPolicyRepository(self.root)
        self.server.configure_tiered_test_seams(policy_repository=repository)
        resolved = TierRouter(repository).state_for(BOARD)
        self.assertEqual(resolved.tier, Tier.SETUP_LITE)
        self.assertEqual(resolved.generation_id, committed.generation_id)
        self.assertEqual(resolved.policy_digest, committed.policy_digest)
        self.assertEqual(resolved.map_snapshot, confirmation.map_snapshot)

    def action(self, name: str, parameters: dict[str, object]) -> str:
        """Use the registered plan/action pair with real containment and backend services."""

        if name == "write_memory":
            parameters = {
                "reason": "VI01 bounded simulated RAM write",
                "elf_artifact": None,
                "allow_address_fallback": True,
                **parameters,
            }
        plan = f"{name}-plan"
        public_text(
            self.server,
            plan,
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
        args = {
            "board_id": BOARD,
            "hypothesis": "The persisted declarations determine access to the requested range.",
            "strategy": "Execute one bounded safe operation against the simulated backend.",
            "hypothesis_made": True,
            "strategy_evaluated": True,
            "expected_fail_return": "Containment refuses before any target operation.",
            "expected_success_return": "The confirmed operation reaches the backend.",
            "max_calls": 1,
            "max_calls_buffer": 0,
            "action_parameters": parameters,
        }
        if name == "flash_bootloader":
            args["user_permission"] = "one-time"
        accepted = json.loads(public_text(self.server, plan, args))
        self.assertEqual(accepted["status"], "plan_accepted", accepted)
        return public_text(self.server, name, {"board_id": BOARD, **parameters})


    def test_public_masked_register_write_preserves_unselected_bits(self) -> None:
        self.commit([region("peripheral", PERIPHERAL, PERIPHERAL + 0x100)])
        self.backend.calls.clear()
        result = self.action("register_write", {
            "address": hex(PERIPHERAL), "mask": "0xff", "value": "0xab",
        })
        self.assertNotIn("Refused", result)
        effects = [call for call in self.backend.calls if call[0] in {"read_memory", "write_memory"}]
        self.assertEqual(effects, [
            ("read_memory", "lite-probe", PERIPHERAL, 32),
            ("write_memory", "lite-probe", PERIPHERAL, 0x123456AB, 32),
        ])

    def test_prohibited_intersections_and_permutations_for_every_action(self) -> None:
        for action, kind in (
            (ActionCategory.MEMORY_READ, "ram"),
            (ActionCategory.MEMORY_WRITE, "ram"),
            (ActionCategory.REGISTER_WRITE, "peripheral"),
            (ActionCategory.FLASH_APPLICATION, "application_flash"),
            (ActionCategory.FLASH_BOOTLOADER, "bootloader"),
            (ActionCategory.BREAKPOINT, "ram"),
        ):
            for start, end in (
                (0x1000, 0x1020),
                (0x1004, 0x1008),
                (0x1000, 0x1005),
                (0x1007, 0x1020),
                (0x1004, 0x100C),
            ):
                rows = [
                    region(kind, 0x1000, 0x1020, executable=True),
                    region("prohibited", start, end),
                ]
                for ordered in itertools.permutations(rows):
                    with self.subTest(action=action, restriction=(start, end), order=ordered):
                        self.commit(list(ordered))
                        before = list(self.backend.calls)
                        with self.assertRaises(SafetyPolicyError) as caught:
                            self.server._check_lite_range(
                                BOARD, action, AddressRange(0x1004, 0x100C)
                            )
                        self.assertEqual(caught.exception.code, "safety/lite-containment-refused")
                        self.assertEqual(self.backend.calls, before)


    def test_write_only_full_mask_writes_without_read_for_every_order(self) -> None:
        for ordered in itertools.permutations(
            [
                region("peripheral", PERIPHERAL, PERIPHERAL + 0x100),
                region("peripheral_write_only", PERIPHERAL + 3, PERIPHERAL + 8, readable=False),
            ]
        ):
            with self.subTest(order=ordered):
                self.commit(list(ordered))
                before = len(self.backend.calls)
                result = self.action(
                    "register_write",
                    {
                        "address": hex(PERIPHERAL),
                        "mask": "0xffffffff",
                        "value": "0x5a",
                    },
                )
                self.assertIn("no preceding read", result)
                self.assertEqual(
                    self.backend.calls[before:],
                    [
                        ("write_memory", "lite-probe", PERIPHERAL, 0x5A, 32),
                        ("release_reset", "lite-probe"),
                    ],
                )


if __name__ == "__main__":
    unittest.main()
