"""M14: ordinary FULL generic setup data backs the existing guarded mass erase.

The board is set up through the public FULL generic setup/validation tools at
the M02 explicit fake probe/session boundary, with an empty approved-board
catalog. The adapter's real ``recover``/``supports_recovery``, the unlock plan,
manual grant, gate and connection lifecycle stay real; only the target's
``mass_erase`` I/O is replaced by a recorder. No physical, probe or serial I/O.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
import json
import unittest
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

from pyocd_debug_mcp import probe_inventory
from pyocd_debug_mcp.capabilities.manual_permissions import ManualPermissionRepository
from pyocd_debug_mcp.capabilities.policy import Tier
from pyocd_debug_mcp.safety import regions as safety_regions
from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionKind,
    SafetyRegion,
    SourceAuthority,
)
from pyocd_debug_mcp.setup_flow import board_catalog
from pyocd_debug_mcp.setup_flow.device_support import resolve_builtin_target_geometry

from tests import m02_independent_support as m02

BOARD = m02.BOARD
EXECUTE = {"board_id": BOARD, "recovery_mechanism": "backend_mass_erase"}


def _fields(*, permission: str | None, strategy: str | None = None) -> dict[str, object]:
    return {
        "board_id": BOARD,
        "hypothesis": "The target needs its whole-chip recovery primitive.",
        "hypothesis_made": True,
        "strategy": strategy
        or "Run exactly one disclosed typed backend recovery after fresh approval.",
        "strategy_evaluated": True,
        "expected_fail_return": "A refusal without any backend erase.",
        "expected_success_return": "The board is disconnected and requires fresh validation.",
        "max_calls": 1,
        "max_calls_buffer": 0,
        "action_parameters": {"recovery_mechanism": "backend_mass_erase"},
        "user_permission": permission,
    }


@contextlib.contextmanager
def _erase_boundary(target: m02.MemoryBoundary, error: Exception | None = None) -> Iterator[list]:
    """Record the target mass-erase primitive invoked by the real adapter."""

    calls: list[m02.MemoryBoundary] = []

    def mass_erase() -> None:
        calls.append(target)
        if error is not None:
            raise error

    with patch.object(target, "mass_erase", mass_erase, create=True):
        yield calls


def _manual(f: Any) -> ManualPermissionRepository:
    manual = ManualPermissionRepository(f.root, f.server.server_run.run_id)
    manual.reset_startup()
    f.server.configure_tiered_test_seams(manual_permission_repository=manual)
    return manual


def _disclose(test: unittest.TestCase, f: Any, **changes: Any) -> dict[str, Any]:
    f.call("target_unlock-plan", dict.fromkeys(_fields(permission=None)))
    reply = json.loads(f.call("target_unlock-plan", _fields(permission=None, **changes)))
    test.assertEqual(reply["status"], "unlock_permission_requested", reply)
    return reply


def _grant(manual: ManualPermissionRepository, disclosure: dict[str, Any]) -> None:
    """Model the separately invoked $mass-erase skill for this exact disclosure."""

    grant = disclosure["manual_grant"]
    manual.write_unlocked_grant(
        action="mass-erase",
        grant_id="m14-grant",
        board_id=BOARD,
        policy_digest=grant["policy_digest"],
        binding_digest=grant["binding_digest"],
    )


def _execute(f: Any) -> str:
    result = asyncio.run(f.server.mcp.call_tool("target_unlock", dict(EXECUTE)))
    return json.dumps(result, default=str)


class GenericGuardedEraseTests(unittest.TestCase):
    def test_ordinary_full_generic_setup_offers_and_runs_one_guarded_mass_erase(self) -> None:
        with m02.public_fixture(self, guarded=True) as f, _erase_boundary(f.target) as erased:
            self.assertEqual(dict(board_catalog._CATALOG), {})
            state = f.server._capability_policies.resolve(BOARD)
            self.assertIs(state.tier, Tier.SETUP_FULL)
            self.assertEqual(state.map_snapshot["schema_version"], 3)
            self.assertIsNone(state.profile_snapshot.get("recover_mode"))
            geometry = state.map_snapshot["geometry"]
            sectors = geometry["erase"]["sectors"]

            caps = json.loads(f.call("get_capabilities", {"board_id": BOARD}))
            row = next(r for r in caps["capabilities"] if r["family"] == "target-recovery")
            self.assertTrue(row["available"], row)
            self.assertTrue(row["safe"], row)
            self.assertEqual(row["preferred_tool"], "target_unlock", row)
            self.assertEqual(row["recovery_mode"], "native-safe", row)

            manual = _manual(f)
            disclosure = _disclose(self, f)
            document = disclosure["disclosure"]
            # Known erased ranges come from the committed setup map, not a board table.
            expected = []
            for flash in geometry["physical_flash"]:
                inside = [
                    index
                    for index, sector in enumerate(sectors)
                    if flash["start"] <= sector["start"] and sector["end"] <= flash["end"]
                ]
                expected.append(
                    {
                        "start": flash["start"],
                        "end": flash["end"],
                        "bank": "internal",
                        "first_sector": inside[0],
                        "last_sector": inside[-1],
                        "sectors": inside,
                    }
                )
            self.assertEqual(document["erased_ranges"], expected)
            self.assertEqual(len(document["erased_ranges"][0]["sectors"]), len(sectors))
            self.assertFalse(document["all_nonvolatile_erased"])
            unknown = " ".join(document["unknown_effects"])
            self.assertIn("not what the bytes", unknown)
            self.assertIn("option", unknown)
            self.assertIn("protection", unknown)
            prompt = disclosure["agent_prompt"]
            self.assertNotIn("entire addressable nonvolatile memory", prompt)
            self.assertNotIn("Only the listed nonvolatile ranges will be erased", prompt)
            self.assertIn("cannot rule out", prompt)
            for span in expected:
                self.assertIn(
                    f"0x{span['start']:08X}-0x{span['end']:08X} "
                    f"(internal, sectors {span['first_sector']}-{span['last_sector']})",
                    prompt,
                )
            self.assertEqual(erased, [])

            _grant(manual, disclosure)
            approved = json.loads(f.call("target_unlock-plan", _fields(permission="one-time")))
            self.assertEqual(approved["status"], "unlock_plan_approved", approved)
            self.assertEqual(manual.status("mass-erase").state, "reserved")
            self.assertEqual(erased, [])

            self.assertIn("performed mass erase", _execute(f))
            self.assertEqual(erased, [f.target])
            self.assertEqual(manual.status("mass-erase").state, "consumed")
            self.assertIsNone(f.server.connection_manager.maybe_connection(BOARD))
            self.assertIsNone(f.server.gate_manager.snapshot(BOARD))

            # One fresh human grant permits exactly one destructive attempt.
            with self.assertRaisesRegex(Exception, "target_unlock"):
                _execute(f)
            self.assertEqual(len(erased), 1)
            self.assertEqual(f.target.calls, [])

    def test_uncertain_backend_failure_spends_grant_and_revokes_readiness(self) -> None:
        failure = RuntimeError("controlled uncertain mass-erase failure")
        with (
            m02.public_fixture(self, guarded=True) as f,
            _erase_boundary(f.target, failure) as erased,
        ):
            manual = _manual(f)
            disclosure = _disclose(self, f)
            _grant(manual, disclosure)
            json.loads(f.call("target_unlock-plan", _fields(permission="one-time")))
            with self.assertRaisesRegex(Exception, "controlled uncertain mass-erase failure"):
                _execute(f)
            self.assertEqual(erased, [f.target])
            self.assertEqual(manual.status("mass-erase").state, "consumed")
            self.assertIsNone(f.server.connection_manager.maybe_connection(BOARD))
            self.assertIsNone(f.server.gate_manager.snapshot(BOARD))
            with self.assertRaisesRegex(Exception, "target_unlock"):
                _execute(f)
            self.assertEqual(len(erased), 1)

    def test_missing_grant_refuses_before_any_backend_erase(self) -> None:
        with m02.public_fixture(self, guarded=True) as f, _erase_boundary(f.target) as erased:
            manual = _manual(f)
            _disclose(self, f)
            with self.assertRaisesRegex(Exception, "manual/"):
                f.call("target_unlock-plan", _fields(permission="one-time"))
            with self.assertRaisesRegex(Exception, "target_unlock"):
                _execute(f)
            self.assertEqual(manual.status("mass-erase").state, "locked")
            self.assertEqual(erased, [])
            self.assertIsNotNone(f.server.connection_manager.maybe_connection(BOARD))

    def test_changed_plan_cannot_reuse_the_disclosed_grant(self) -> None:
        with m02.public_fixture(self, guarded=True) as f, _erase_boundary(f.target) as erased:
            manual = _manual(f)
            _grant(manual, _disclose(self, f))
            with self.assertRaisesRegex(Exception, "plan field changed after disclosure"):
                f.call(
                    "target_unlock-plan",
                    _fields(permission="one-time", strategy="A different recovery strategy."),
                )
            with self.assertRaisesRegex(Exception, "target_unlock"):
                _execute(f)
            self.assertEqual(erased, [])
            self.assertNotEqual(manual.status("mass-erase").state, "consumed")

    def test_changed_session_cannot_reuse_the_disclosed_grant(self) -> None:
        with m02.public_fixture(self, guarded=True) as f, _erase_boundary(f.target) as erased:
            manual = _manual(f)
            first = _disclose(self, f)
            _grant(manual, first)
            f.call("disconnect", {"board_id": BOARD})
            # The configured profile route opens a new session for the same board.
            # Normal reconnect enumerates CLI inventory after the setup-only fake ends.
            with patch.object(
                probe_inventory,
                "list_connected_probes_cli",
                return_value=[
                    probe_inventory.ProbeInfo(
                        uid=m02.UID,
                        description="Controlled ST-Link",
                        raw="Controlled ST-Link",
                        family="stlink",
                    )
                ],
            ):
                self.assertIn("Connected", f.call("connect", {"board_id": BOARD}))
            second = _disclose(self, f)
            self.assertNotEqual(
                second["manual_grant"]["binding_digest"], first["manual_grant"]["binding_digest"]
            )
            with self.assertRaisesRegex(Exception, "manual/binding-mismatch"):
                f.call("target_unlock-plan", _fields(permission="one-time"))
            with self.assertRaisesRegex(Exception, "target_unlock"):
                _execute(f)
            self.assertEqual(manual.status("mass-erase").state, "unlocked")
            self.assertEqual(erased, [])

    def test_full_recovery_stays_unavailable_without_geometry_or_backend_or_when_manual_only(
        self,
    ) -> None:
        with m02.public_fixture(self, guarded=True) as f:
            state = f.server._capability_policies.resolve(BOARD)
            router = f.server._tier_router

            def mode(candidate: Any, backend: bool) -> str:
                payload = router.capabilities(
                    BOARD,
                    project_root=f.root,
                    native_recovery_backend_available=backend,
                    state=candidate,
                )
                row = next(r for r in payload["capabilities"] if r["family"] == "target-recovery")
                return row["recovery_mode"]

            no_erase = dict(state.map_snapshot)
            no_erase["geometry"] = {**no_erase["geometry"], "erase": {"kind": "unavailable"}}
            manual_only = {**state.profile_snapshot, "recover_mode": "manual_only"}
            self.assertEqual(mode(state, True), "native-safe")
            self.assertEqual(mode(state, False), "unavailable")
            self.assertEqual(mode(replace(state, map_snapshot=no_erase), True), "unavailable")
            self.assertEqual(
                mode(replace(state, profile_snapshot=manual_only), True), "unavailable"
            )


class GenericRecoveryDisclosureTests(unittest.TestCase):
    def test_flash_without_setup_erase_geometry_is_an_unknown_effect_not_an_erased_range(
        self,
    ) -> None:
        geometry = resolve_builtin_target_geometry("nrf52840")
        provenance = (Provenance(SourceAuthority.DEVICE_SUPPORT, "builtin-target", "test"),)
        regions = [
            SafetyRegion(
                f"pack flash: {item.name}",
                RegionKind.PHYSICAL_FLASH,
                AddressRange(item.start, item.end),
                provenance,
            )
            for item in geometry.flash_regions
        ]
        sectors = [
            {"start": start, "end": end, "bank": "internal"}
            for start, end in geometry.erase_sectors
        ]
        covered = [
            region
            for region in regions
            if any(
                region.address_range.overlaps(AddressRange(s["start"], s["end"])) for s in sectors
            )
        ]
        uncovered = [region for region in regions if region not in covered]
        self.assertTrue(covered and uncovered)

        disclosure = safety_regions.build_generic_recovery_erase_disclosure(
            regions, {"sectors": sectors}
        )

        self.assertEqual(
            [span.address_range for span in disclosure.spans],
            [region.address_range for region in covered],
        )
        self.assertFalse(disclosure.all_nonvolatile_erased)
        self.assertEqual(
            {region.name for region in disclosure.affected_regions},
            {region.name for region in covered},
        )
        unknown = " ".join(disclosure.unknown_effects)
        for region in uncovered:
            self.assertIn(
                f"0x{region.address_range.start:08X}-0x{region.address_range.end:08X}", unknown
            )
        self.assertIn("unknown_effects", disclosure.to_document())

    def test_missing_erase_geometry_cannot_produce_a_disclosure(self) -> None:
        provenance = (Provenance(SourceAuthority.DEVICE_SUPPORT, "pack", "test"),)
        flash = SafetyRegion(
            "pack flash: Flash",
            RegionKind.PHYSICAL_FLASH,
            AddressRange(0x08000000, 0x08001000),
            provenance,
        )
        with self.assertRaisesRegex(Exception, "erase sector geometry"):
            safety_regions.build_generic_recovery_erase_disclosure([flash], {"sectors": []})


if __name__ == "__main__":
    unittest.main()
