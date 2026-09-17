"""Focused tier-routing contracts before server integration."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pyocd_debug_mcp.capabilities.lite import normalize_lite_confirmation
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.capabilities.routing import TierRouteRefusal, TierRouter
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP


class TierRouterTests(unittest.TestCase):
    @staticmethod
    def _lite_map(board_id: str) -> dict[str, object]:
        return normalize_lite_confirmation(
            board_id,
            {
                "decision": "confirm-or-correct",
                "regions": [
                    {
                        "name": "SRAM",
                        "kind": "ram",
                        "start": 0x20000000,
                        "end": 0x20001000,
                        "readable": True,
                        "writable": True,
                        "executable": False,
                        "source_pages": [1],
                        "source_note": "operator confirmation",
                    }
                ],
                "flash": {"backend_target": None, "erase_sectors": []},
                "recovery": None,
            },
        ).map_snapshot

    @staticmethod
    def _lite_map_with_read_only_flash(board_id: str) -> dict[str, object]:
        """Return confirmed geometry where flash reads are intentionally not writes."""

        return normalize_lite_confirmation(
            board_id,
            {
                "decision": "confirm-or-correct",
                "regions": [
                    {
                        "name": "SRAM",
                        "kind": "ram",
                        "start": "0x20000000",
                        "end": "0x20001000",
                        "readable": True,
                        "writable": True,
                        "executable": False,
                        "source_pages": [1],
                        "source_note": "operator confirmation",
                    },
                    {
                        "name": "application flash",
                        "kind": "application_flash",
                        "start": "0x00004000",
                        "end": "0x0003F000",
                        "readable": True,
                        "writable": False,
                        "executable": True,
                        "source_pages": [2],
                        "source_note": "operator-confirmed allocation",
                    },
                    {
                        "name": "bootloader",
                        "kind": "bootloader",
                        "start": "0x0003F000",
                        "end": "0x00040000",
                        "readable": True,
                        "writable": False,
                        "executable": True,
                        "source_pages": [2],
                        "source_note": "operator-confirmed protected bootloader",
                    },
                ],
                "flash": {
                    "backend_target": "nrf52840",
                    "erase_sectors": [{"start": "0x00004000", "size": 4096}],
                },
                "recovery": None,
            },
        ).map_snapshot

    def test_wrong_tier_precedes_missing_plan_and_lite_raw_warns(self) -> None:
        """Routing names the policy repair before legacy guards and warns on lite bypass."""

        with tempfile.TemporaryDirectory() as temporary:
            repository = CapabilityPolicyRepository(Path(temporary))
            router = TierRouter(repository)
            repository.resolve("raw_board")

            with self.assertRaisesRegex(TierRouteRefusal, "no safety map") as raw_refusal:
                router.require_safe("read_memory_address", "raw_board")
            self.assertEqual(raw_refusal.exception.code, "tier/wrong-route")

            repository.commit(
                "lite_board",
                Tier.SETUP_LITE,
                map_snapshot=self._lite_map("lite_board"),
            )
            warning = router.require_raw("read_memory_raw", "lite_board")
            self.assertIsNotNone(warning)
            self.assertTrue(warning["display_to_human"])

            from tests.tiered_acceptance_support import known_good_full_policy

            profile, full_map = known_good_full_policy()
            repository.commit(
                "legacy_full",
                Tier.SETUP_FULL,
                profile_snapshot=profile,
                map_snapshot=full_map,
            )
            with self.assertRaisesRegex(TierRouteRefusal, "fully set up") as full_refusal:
                router.require_raw("read_memory_raw", "legacy_full")
            self.assertEqual(full_refusal.exception.code, "tier/wrong-route")
            self.assertEqual(
                full_refusal.exception.remedies,
                ("read_memory_address-plan", "read_memory_address"),
            )

    def test_captured_route_context_never_merges_a_later_lite_warning(self) -> None:
        """A full route refusal retains its source generation across a provider race."""

        registry = RegistryFastMCP("tier-routing-context")
        registry._json_refusal_context["read_memory_raw"] = lambda _name, _board: {
            "tier": "setup-lite",
            "raw": True,
            "warning": {"display_to_human": True},
        }
        payload = json.loads(
            registry._json_refusal(
                "read_memory_raw",
                "board",
                "use the planned safe route",
                code="tier/wrong-route",
                context={"tier": "setup-full"},
            )
        )
        self.assertEqual(payload["tier"], "setup-full")
        self.assertNotIn("raw", payload)
        self.assertNotIn("warning", payload)

    def test_capabilities_reports_every_operation_family_and_lite_prerequisites(self) -> None:
        """Discovery is per-board policy data, not a two-row memory shortcut."""

        with tempfile.TemporaryDirectory() as temporary:
            repository = CapabilityPolicyRepository(Path(temporary))
            router = TierRouter(repository)
            repository.commit(
                "lite_board",
                Tier.SETUP_LITE,
                map_snapshot=self._lite_map("lite_board"),
            )

            payload = router.capabilities("lite_board", project_root=Path(temporary))
            families = {row["family"]: row for row in payload["capabilities"]}

            self.assertIn("connection", families)
            self.assertIn("serial-exchange", families)
            self.assertIn("flash-application", families)
            self.assertIn("target-recovery", families)
            self.assertTrue(families["memory-read"]["available"])
            self.assertTrue(families["breakpoint"]["available"])
            self.assertFalse(families["breakpoint"]["safe"])
            self.assertIn("confirmed executable region", families["breakpoint"]["prerequisites"])
            self.assertTrue(families["flash-application"]["available"])
            self.assertFalse(families["flash-application"]["safe"])
            self.assertIn(
                "confirmed flash backend target", families["flash-application"]["prerequisites"]
            )

    def test_lite_executable_ram_is_discoverable_for_breakpoints(self) -> None:
        """Discovery must match the lite containment rule for executable SRAM."""

        with tempfile.TemporaryDirectory() as temporary:
            repository = CapabilityPolicyRepository(Path(temporary))
            snapshot = self._lite_map("lite_board")
            regions = snapshot["regions"]
            assert isinstance(regions, list)
            regions[0]["executable"] = True
            repository.commit("lite_board", Tier.SETUP_LITE, map_snapshot=snapshot)

            payload = TierRouter(repository).capabilities(
                "lite_board", project_root=Path(temporary)
            )
            families = {row["family"]: row for row in payload["capabilities"]}
            self.assertTrue(families["breakpoint"]["safe"])

    def test_read_only_lite_flash_is_not_advertised_as_safe(self) -> None:
        """Discovery must require the same flash write permission as containment."""

        with tempfile.TemporaryDirectory() as temporary:
            repository = CapabilityPolicyRepository(Path(temporary))
            repository.commit(
                "lite_board",
                Tier.SETUP_LITE,
                map_snapshot=self._lite_map_with_read_only_flash("lite_board"),
            )

            payload = TierRouter(repository).capabilities(
                "lite_board", project_root=Path(temporary)
            )
            families = {row["family"]: row for row in payload["capabilities"]}
            for family in ("flash-application", "flash-bootloader"):
                with self.subTest(family=family):
                    self.assertFalse(families[family]["safe"])
                    self.assertEqual(families[family]["preferred_tool"], "flash_raw")

    def test_capability_discovery_can_use_the_callers_captured_policy_snapshot(self) -> None:
        """Discovery never combines live identity with a later pointer generation."""

        with tempfile.TemporaryDirectory() as temporary:
            repository = CapabilityPolicyRepository(Path(temporary))
            router = TierRouter(repository)
            initial = router.state_for("raw_board")
            repository.commit(
                "raw_board", Tier.SETUP_LITE, map_snapshot=self._lite_map("raw_board")
            )

            payload = router.capabilities("raw_board", project_root=Path(temporary), state=initial)
            self.assertEqual(payload["tier"], "no-setup")


if __name__ == "__main__":
    unittest.main()
