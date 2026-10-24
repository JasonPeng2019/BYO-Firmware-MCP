"""VI01: persisted lite declarations constrain the real safe action boundary."""

from __future__ import annotations

import itertools
import json
import struct
import unittest
from pathlib import Path

from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.capabilities.lite import normalize_lite_confirmation
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.capabilities.routing import TierRouter
from pyocd_debug_mcp.safety.enforce import SafetyPolicyError
from pyocd_debug_mcp.safety.regions import ActionCategory, AddressRange, RegionKind
from tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    known_good_full_policy,
    public_text,
    tiered_test_server,
)


BOARD = "lite_overlaps"
RAM = 0x20000000
FLASH = 0x08000000
PERIPHERAL = 0x40000000


def region(kind: str, start: int, end: int, **permissions: bool) -> dict[str, object]:
    return {
        "name": f"confirmed {kind}",
        "kind": kind,
        "start": hex(start),
        "end": hex(end),
        "readable": True,
        "writable": True,
        "executable": False,
        "source_pages": [42],
        "source_note": "explicit operator-confirmed geometry and access permissions",
        **permissions,
    }


def cortex_elf(path: Path, start: int) -> Path:
    """Serialize a valid ARM ELF32 load image; no compiler or policy parser seam."""

    ident = b"\x7fELF\x01\x01\x01" + b"\0" * 9
    header = ident + struct.pack(
        "<HHIIIIIHHHHHH",
        2,
        40,
        1,
        start + 9,
        52,
        0,
        0x05000000,
        52,
        32,
        1,
        40,
        0,
        0,
    )
    program = struct.pack("<8I", 1, 84, start, start, 16, 16, 5, 4)
    vector_and_code = struct.pack("<4I", RAM + 0x80, start + 9, 0xBF00BF00, 0xBF00BF00)
    path.write_bytes(header + program + vector_and_code)
    return path


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

    def refused(
        self, name: str, parameters: dict[str, object], message: str = "not fully confirmed"
    ) -> None:
        before = list(self.backend.calls)
        try:
            result = self.action(name, parameters)
        except ToolError as exc:
            result = str(exc)
        self.assertIn(message, result)
        self.assertEqual(self.backend.calls, before, "refusal must precede all backend effects")

    def test_audited_prohibited_subrange_in_ram_write(self) -> None:
        self.commit([region("prohibited", RAM + 4, RAM + 8)])
        with self.assertRaises(SafetyPolicyError):
            self.server._check_memory_safety(BOARD, RAM + 4, 32)
        self.refused(
            "write_memory",
            {
                "symbol_or_address": hex(RAM + 4),
                "value": 1,
                "width": 32,
            },
        )

    def test_audited_application_flash_over_bootloader(self) -> None:
        self.commit(
            [
                region("application_flash", FLASH, FLASH + 0x100, executable=True),
                region("bootloader", FLASH, FLASH + 0x20, executable=True),
            ]
        )
        artifact = cortex_elf(self.root / "overlap.elf", FLASH)
        with self.assertRaises(SafetyPolicyError):
            self.server._check_flash_safety("flash_application", BOARD, artifact)
        self.refused("flash_application", {"artifact": str(artifact)})

    def test_audited_write_only_peripheral_read(self) -> None:
        self.commit(
            [
                region("peripheral", PERIPHERAL, PERIPHERAL + 0x100),
                region("peripheral_write_only", PERIPHERAL, PERIPHERAL + 4, readable=False),
            ]
        )
        self.refused(
            "read_memory_address",
            {
                "address": PERIPHERAL,
                "width": 32,
                "length": 4,
            },
        )

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

    def test_adjacent_protections_do_not_intersect_equal_endpoints(self) -> None:
        for ordered in itertools.permutations(
            [
                region("ram", 0x1000, 0x1020),
                region("prohibited", 0x0FFC, 0x1000),
                region("prohibited", 0x1020, 0x1024),
            ]
        ):
            with self.subTest(order=ordered):
                self.commit(list(ordered))
                self.assertEqual(
                    self.server._check_lite_range(
                        BOARD, ActionCategory.MEMORY_WRITE, AddressRange(0x1000, 0x1020)
                    ),
                    RegionKind.RAM,
                )

    def test_flash_role_conflicts_and_read_only_permissions_in_both_orders(self) -> None:
        for action, kind, other in (
            (ActionCategory.FLASH_APPLICATION, "application_flash", "bootloader"),
            (ActionCategory.FLASH_BOOTLOADER, "bootloader", "application_flash"),
            (ActionCategory.REGISTER_WRITE, "peripheral", "peripheral_read_only"),
        ):
            for start, end in ((0x1000, 0x1020), (0x1004, 0x1008), (0x1007, 0x1020)):
                for ordered in itertools.permutations(
                    [
                        region(kind, 0x1000, 0x1020),
                        region(other, start, end),
                    ]
                ):
                    with self.subTest(action=action, restriction=(start, end), order=ordered):
                        self.commit(list(ordered))
                        with self.assertRaises(SafetyPolicyError):
                            self.server._check_lite_range(
                                BOARD, action, AddressRange(0x1004, 0x100C)
                            )

    def test_explicit_permission_restrictions_win_over_same_kind_permissions(self) -> None:
        for action, kind, permission in (
            (ActionCategory.MEMORY_READ, "ram", "readable"),
            (ActionCategory.MEMORY_WRITE, "ram", "writable"),
            (ActionCategory.REGISTER_WRITE, "peripheral", "writable"),
            (ActionCategory.FLASH_APPLICATION, "application_flash", "writable"),
            (ActionCategory.FLASH_BOOTLOADER, "bootloader", "writable"),
            (ActionCategory.BREAKPOINT, "ram", "executable"),
        ):
            for ordered in itertools.permutations(
                [
                    region(kind, 0x1000, 0x1020, executable=True),
                    region(kind, 0x1007, 0x1008, **{permission: False}),
                ]
            ):
                with self.subTest(action=action, order=ordered):
                    self.commit(list(ordered))
                    with self.assertRaises(SafetyPolicyError):
                        self.server._check_lite_range(BOARD, action, AddressRange(0x1004, 0x100C))

    def test_write_only_partial_overlap_refuses_reads_and_masked_writes(self) -> None:
        for kind in ("peripheral_write_only", "peripheral"):
            for ordered in itertools.permutations(
                [
                    region("peripheral", PERIPHERAL, PERIPHERAL + 0x100),
                    region(kind, PERIPHERAL + 3, PERIPHERAL + 8, readable=False),
                ]
            ):
                with self.subTest(kind=kind, order=ordered):
                    self.commit(list(ordered))
                    self.refused(
                        "read_memory_address",
                        {
                            "address": PERIPHERAL,
                            "width": 32,
                            "length": 4,
                        },
                    )
                    self.refused(
                        "register_write",
                        {
                            "address": hex(PERIPHERAL),
                            "mask": "0xff",
                            "value": "0x5a",
                        },
                        "read-modify-write",
                    )

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

    def test_plain_ram_and_peripheral_actions_keep_working(self) -> None:
        self.commit([region("peripheral", PERIPHERAL, PERIPHERAL + 0x100)])
        self.action("write_memory", {"symbol_or_address": hex(RAM), "value": 0x5A, "width": 32})
        self.action("read_memory_address", {"address": RAM, "width": 32, "length": 4})
        self.action("read_memory_address", {"address": PERIPHERAL, "width": 32, "length": 4})
        self.action(
            "register_write",
            {
                "address": hex(PERIPHERAL),
                "mask": "0xff",
                "value": "0x5a",
            },
        )
        self.assertIn(("write_memory", "lite-probe", RAM, 0x5A, 32), self.backend.calls)
        self.assertIn(("read_memory_block", "lite-probe", RAM, 4), self.backend.calls)
        self.assertIn(("read_memory_block", "lite-probe", PERIPHERAL, 4), self.backend.calls)
        self.assertIn(("read_memory", "lite-probe", PERIPHERAL, 32), self.backend.calls)
        self.assertIn(
            ("write_memory", "lite-probe", PERIPHERAL, 0x1234565A, 32), self.backend.calls
        )

    def test_flash_checks_valid_elf_and_entire_erase_sector(self) -> None:
        artifact = cortex_elf(self.root / "sector.elf", FLASH)
        app = region("application_flash", FLASH, FLASH + 0x100, executable=True)
        self.commit([app, region("bootloader", FLASH + 0x80, FLASH + 0x100, executable=True)])
        self.refused("flash_application", {"artifact": str(artifact)})
        self.commit([app])
        self.server._check_flash_safety("flash_application", BOARD, artifact)

    def test_normal_flash_roles_and_geometry_remain_usable(self) -> None:
        artifact = cortex_elf(self.root / "ordinary.elf", FLASH)
        for kind, tool in (
            ("application_flash", "flash_application"),
            ("bootloader", "flash_bootloader"),
        ):
            with self.subTest(role=kind):
                self.commit(
                    [
                        region("physical_flash", FLASH, FLASH + 0x100, writable=False),
                        region(kind, FLASH, FLASH + 0x100, executable=True),
                    ]
                )
                self.server._check_flash_safety(tool, BOARD, artifact)

    def test_physical_flash_alone_and_incomplete_coverage_do_not_grant_access(self) -> None:
        self.commit([region("physical_flash", FLASH, FLASH + 0x100)])
        for action in (
            ActionCategory.MEMORY_READ,
            ActionCategory.MEMORY_WRITE,
            ActionCategory.FLASH_APPLICATION,
            ActionCategory.FLASH_BOOTLOADER,
        ):
            with self.subTest(action=action):
                with self.assertRaises(SafetyPolicyError):
                    self.server._check_lite_range(BOARD, action, AddressRange(FLASH, FLASH + 4))
        self.commit([region("ram", 0x1000, 0x1004), region("ram", 0x1004, 0x1008)])
        with self.assertRaises(SafetyPolicyError):
            self.server._check_lite_range(
                BOARD, ActionCategory.MEMORY_WRITE, AddressRange(0x1000, 0x1008)
            )

    def test_full_tier_stale_snapshot_still_requires_refresh(self) -> None:
        profile, snapshot = known_good_full_policy()
        repository = CapabilityPolicyRepository(self.root)
        committed = repository.commit(
            "legacy_full",
            Tier.SETUP_FULL,
            profile_snapshot=profile,
            map_snapshot=snapshot,
        )
        self.server.configure_tiered_test_seams(
            policy_repository=CapabilityPolicyRepository(self.root)
        )
        self.assertEqual(
            self.server._tier_router.state_for("legacy_full").generation_id, committed.generation_id
        )
        # This historical schema-v2 fixture commits but lacks current reviewed
        # board authority. Full-tier use must still refuse it before all I/O.
        for action, requested in (
            (ActionCategory.MEMORY_READ, AddressRange(RAM, RAM + 4)),
            (ActionCategory.MEMORY_WRITE, AddressRange(RAM, RAM + 4)),
            (ActionCategory.MEMORY_READ, AddressRange(RAM + 0xFFC, RAM + 0x1004)),
            (ActionCategory.REGISTER_WRITE, AddressRange(PERIPHERAL, PERIPHERAL + 4)),
            (ActionCategory.FLASH_APPLICATION, AddressRange(0, 4)),
        ):
            with self.subTest(action=action, requested=requested):
                before = list(self.backend.calls)
                with self.assertRaises(SafetyPolicyError) as caught:
                    self.server._check_active_snapshot_range("legacy_full", action, requested)
                self.assertEqual(caught.exception.code, "safety/map-refresh-required")
                self.assertEqual(self.backend.calls, before)


if __name__ == "__main__":
    unittest.main()
