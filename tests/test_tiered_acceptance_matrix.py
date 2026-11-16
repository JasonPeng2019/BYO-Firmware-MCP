"""AT05: exhaustive raw/safe matrix routing and full-tier refusal precedence."""

from __future__ import annotations

import unittest

from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.capabilities.routing import TierRouteRefusal, TierRouter

from tests.tiered_acceptance_support import (
    assert_refusal,
    confirmed_lite_policy,
    isolated_project,
    known_good_full_policy,
    public_call,
    tiered_test_server,
)


RAW_TO_SAFE = {
    "read_memory_raw": "read_memory_address",
    "write_memory_raw": "write_memory",
    "register_write_raw": "register_write",
    "write_cpu_register_raw": "write_cpu_register",
    "set_execution_state_raw": "set_execution_state",
    "set_breakpoint_raw": "set_breakpoint",
    "reset_and_halt_raw": "reset_and_halt",
    "flash_raw": "flash_application",
    "read_serial_raw": "read_serial",
    "write_serial_raw": "write_serial",
    "serial_exchange_raw": "serial_exchange",
    "target_unlock_raw": "target_unlock",
}
FULL_BOARD = "legacy_full"


class TieredMatrixAcceptanceTests(unittest.TestCase):
    """The central matrix must make the same decision for every raw family."""

    def setUp(self) -> None:
        self._project = isolated_project()
        self.root = self._project.__enter__()
        self.addCleanup(self._project.__exit__, None, None, None)
        self.repository = CapabilityPolicyRepository(self.root)
        self.router = TierRouter(self.repository)
        self.repository.resolve("raw_board")
        self.repository.commit(
            "lite_board",
            Tier.SETUP_LITE,
            map_snapshot=confirmed_lite_policy("lite_board"),
            evidence={"source": "confirmed"},
        )
        full_profile, full_map = known_good_full_policy()
        self.repository.commit(
            FULL_BOARD,
            Tier.SETUP_FULL,
            profile_snapshot=full_profile,
            map_snapshot=full_map,
            evidence={"identity": "exact"},
        )


    def test_corrupt_policy_refuses_both_raw_and_safe_before_any_other_guard(self) -> None:
        """AT05: damaged committed policy is never a fallback to raw capability."""

        corrupted = self.repository.board_root("corrupt_board")
        corrupted.mkdir(parents=True)
        (corrupted / "current.json").write_text("not-json", encoding="utf-8")
        for operation, method in (
            ("read_memory_raw", self.router.require_raw),
            ("read_memory_address", self.router.require_safe),
        ):
            with self.subTest(operation=operation), self.assertRaises(TierRouteRefusal) as raised:
                method(operation, "corrupt_board")
            self.assertEqual(raised.exception.code, "tier/corrupt-policy")


    def test_every_full_raw_tool_is_refused_by_the_public_mcp_dispatch_before_schema_or_io(
        self,
    ) -> None:
        """AT05: all matrix raw rows take the same public full-tier route.

        Only ``board_id`` is supplied deliberately.  Registry route guards must
        reject full-tier raw access before tool-specific parsing can turn one
        matrix row into a validation error (or, worse, touch a backend).
        """

        with tiered_test_server(self.root) as server:
            server.configure_tiered_test_seams(policy_repository=self.repository)
            for raw, safe in RAW_TO_SAFE.items():
                with self.subTest(raw=raw, safe=safe):
                    payload = public_call(server, raw, {"board_id": FULL_BOARD})
                    assert_refusal(self, payload, "tier/wrong-route", operation=raw)
                    self.assertEqual(payload["board_id"], FULL_BOARD, payload)
                    self.assertEqual(payload["tier"], "setup-full", payload)
                    self.assertIn(safe, payload["message"], payload)

    def test_every_safe_matrix_row_is_publicly_refused_in_no_setup_before_plan_or_schema(
        self,
    ) -> None:
        """AT05: no safe family may turn an absent policy into a plan/schema loophole."""

        with tiered_test_server(self.root) as server:
            server.configure_tiered_test_seams(policy_repository=self.repository)
            for raw, safe in RAW_TO_SAFE.items():
                with self.subTest(raw=raw, safe=safe):
                    # Safe tools retain their historical text-error transport,
                    # unlike frozen JSON raw refusals.  The public MCP call
                    # must nevertheless reach the tier guard before each
                    # tool's otherwise-required schema fields are examined.
                    import asyncio

                    with self.assertRaisesRegex(Exception, "no safety map") as raised:
                        asyncio.run(server.mcp.call_tool(safe, {"board_id": "raw_board"}))
                    self.assertIn("raw operation", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
