"""AT09: shipped manual-only skills and server use one project-root protocol.

Set BYO_MCP_TEST_WORKSPACE_ROOT to the paired workspace source checkout when
testing another layout. Otherwise use sibling CodexClaudeWorkflow or the legacy
sibling .agent-workspace source. Runtime grant state is never a source fixture.
"""

from __future__ import annotations
from tests.workspace_fixture_support import _attach_project_module, _workspace_helper

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.capabilities.manual_permissions import ManualPermissionRepository
from pyocd_debug_mcp.guardrails.plan_defs import PermissionMode, definition_for_action
from pyocd_debug_mcp.safety.enforce import SafetyPolicyError
from pyocd_debug_mcp.safety.regions import (
    ActionCategory,
    AddressRange,
    Allowed,
    Refusal,
    RegionKind,
)

from tests.nrf_fixture_support import nrf_registered_pack_fixture, observe_nrf_control
from tests.tiered_acceptance_support import manual_permission_root, public_text


BOARD = "left_controller"
POLICY = "a" * 64


class TieredWorkspaceAcceptanceTests(unittest.TestCase):
    def test_two_physical_attached_workspaces_have_distinct_runtime_grants(self) -> None:
        """AT09/C08: attaching project B cannot read or consume project A's grant files."""

        attach = _attach_project_module()
        with (
            tempfile.TemporaryDirectory(prefix="tiered-attached-a-") as first_text,
            tempfile.TemporaryDirectory(prefix="tiered-attached-b-") as second_text,
        ):
            first_root = Path(first_text)
            second_root = Path(second_text)
            first_workspace = attach._ensure_hidden_workspace_entry(first_root)
            second_workspace = attach._ensure_hidden_workspace_entry(second_root)
            self.assertFalse(first_workspace.is_symlink())
            self.assertFalse(second_workspace.is_symlink())
            self.assertNotEqual(first_workspace.resolve(), second_workspace.resolve())
            self.assertTrue((first_workspace / "runtime").is_dir())
            self.assertTrue((second_workspace / "runtime").is_dir())

            first_repo = ManualPermissionRepository(first_root, "run-fixture")
            second_repo = ManualPermissionRepository(second_root, "run-fixture")
            first_repo.reset_startup()
            second_repo.reset_startup()
            first_helper = _workspace_helper(first_workspace / "internal" / "manual_permissions.py")
            first_helper.unlock_manual_grant(
                project_root=first_root,
                action="downgrade",
                board_id=BOARD,
                policy_digest=POLICY,
            )
            self.assertEqual(first_repo.status("downgrade").state, "unlocked")
            self.assertEqual(second_repo.status("downgrade").state, "locked")
            self.assertFalse((manual_permission_root(second_root) / "downgrade.claim").exists())


class NrfPublicRegeneratedMapControls(unittest.TestCase):
    """R17-C02/FR04-M1: real registered Nordic pack, public guarded controls, no hardware."""

    def setUp(self) -> None:
        self.fixture = nrf_registered_pack_fixture(self)

    def plan(self, action: str, parameters: dict[str, object]) -> dict[str, object]:
        server, board = self.fixture.server, self.fixture.board_id
        tool = action + "-plan"
        # Initialize the actual registered plan tool with its complete NULL form.
        public_text(
            server,
            tool,
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
        response = json.loads(
            public_text(
                server,
                tool,
                {
                    "board_id": board,
                    "hypothesis": "The regenerated Nordic map enforces this register's SVD policy",
                    "strategy": "Use one public action with a device I/O effect recorder",
                    "hypothesis_made": True,
                    "strategy_evaluated": True,
                    "expected_fail_return": "The actual guard refuses before device I/O",
                    "expected_success_return": "One mapped operation reaches the recorder",
                    "max_calls": 1,
                    "max_calls_buffer": 0,
                    "action_parameters": parameters,
                },
            )
        )
        self.assertEqual(response["status"], "plan_accepted", response)
        active = server.plan_engine.active_plan(action, board)
        self.assertIsNotNone(active)
        self.assertEqual(active.remaining_calls, 1)
        # These two ordinary routes deliberately require a plan, not a human
        # permission grant. Do not invent or inject permission authorization.
        self.assertIs(definition_for_action(action).permission_mode, PermissionMode.NONE)
        self.assertIsNone(server.permission_store.active_grant(action, board))
        return response

    def invoke(self, action: str, parameters: dict[str, object]) -> str:
        try:
            return public_text(
                self.fixture.server, action, {"board_id": self.fixture.board_id, **parameters}
            )
        except ToolError as exc:
            return str(exc)

    def test_real_nrf_nvmc_uicr_public_refusals_have_zero_backend_effects(self) -> None:
        fixture = self.fixture
        rows = {row.name.split(" (+", 1)[0]: row for row in fixture.geometry.peripheral_regions}
        for name in (
            "NVMC.CONFIG",
            "NVMC.ERASEPAGE",
            "NVMC.ERASEALL",
            "NVMC.ERASEUICR",
            "UICR.APPROTECT",
            "UICR.NFCPINS",
        ):
            with self.subTest(register=name):
                row = rows[name]
                requested = AddressRange(row.start, row.end)
                details: dict[str, object] = {"register": row.to_document()}
                fixture.backend.calls.clear()
                try:
                    self.assertTrue(row.prohibited)
                    self.assertEqual(requested.size, 4)
                    overlaps = [
                        region
                        for region in fixture.document.regions
                        if region.kind is RegionKind.PROHIBITED
                        and region.address_range.contains(requested)
                    ]
                    details["prohibited_overlaps"] = [r.to_document() for r in overlaps]
                    self.assertTrue(overlaps, name)
                    decision = fixture.document.safety_map.check(
                        ActionCategory.REGISTER_WRITE, (requested,)
                    )
                    details["map_decision"] = asdict(decision)
                    self.assertIsInstance(decision, Refusal)
                    self.assertEqual(decision.offending_range, requested)
                    self.assertIs(decision.classification, RegionKind.PROHIBITED)
                    self.assertEqual(decision.code, "safety/prohibited")
                    self.assertEqual(
                        decision.remedy,
                        "Choose an operation that does not touch prohibited memory.",
                    )
                    with self.assertRaises(SafetyPolicyError) as caught:
                        fixture.server._check_active_snapshot_range(
                            fixture.board_id, ActionCategory.REGISTER_WRITE, requested
                        )
                    refusal = caught.exception
                    self.assertEqual(refusal.code, "safety/prohibited")
                    self.assertEqual(refusal.remedy, ("board_safety_refresh",))
                    details["policy_refusal"] = {
                        "code": refusal.code,
                        "remedy": refusal.remedy,
                        "text": str(refusal),
                    }
                    parameters = {"address": row.start, "mask": 0xFFFFFFFF, "value": 0}
                    details["plan"] = self.plan("register_write", parameters)
                    response = self.invoke("register_write", parameters)
                    details["public_response"] = response
                    # Legacy public tools return text; code/remedy are asserted
                    # at their real typed boundary, and that refusal is relayed.
                    self.assertIn(str(refusal), response)
                    self.assertNotIn("mapped peripheral write allowed", response)
                    retained = fixture.server.plan_engine.active_plan(
                        "register_write", fixture.board_id
                    )
                    self.assertIsNotNone(retained)
                    self.assertEqual(retained.remaining_calls, 1)
                    details["refused_plan"] = asdict(retained)
                    self.assertEqual(fixture.backend.calls, [])
                    # Only genuinely write-only SVD registers refuse reads.
                    # Readable prohibited-write regions retain their read policy.
                    if not row.readable:
                        read_decision = fixture.document.safety_map.check(
                            ActionCategory.MEMORY_READ, (requested,)
                        )
                        self.assertIsInstance(read_decision, Refusal)
                        self.assertEqual(read_decision.code, "safety/wrong-region-kind")
                        self.assertEqual(read_decision.remedy, "Choose a readable mapped region.")
                        details["read_decision"] = asdict(read_decision)
                        with self.assertRaises(SafetyPolicyError) as read_caught:
                            fixture.server._check_active_snapshot_range(
                                fixture.board_id, ActionCategory.MEMORY_READ, requested
                            )
                        self.assertEqual(read_caught.exception.code, "safety/wrong-region-kind")
                        self.assertEqual(read_caught.exception.remedy, ("board_safety_refresh",))
                        read_parameters = {"address": row.start, "width": 32, "length": None}
                        details["read_plan"] = self.plan("read_memory_address", read_parameters)
                        read_response = self.invoke("read_memory_address", read_parameters)
                        details["read_public_response"] = read_response
                        self.assertIn(str(read_caught.exception), read_response)
                        self.assertEqual(fixture.backend.calls, [])
                finally:
                    observe_nrf_control(self, fixture, name, **details)

    def test_real_nrf_independent_gpio_read_write_requires_and_consumes_public_plans(self) -> None:
        fixture = self.fixture
        row = next(r for r in fixture.geometry.peripheral_regions if r.name == "P0.OUT")
        requested = AddressRange(row.start, row.end)
        details: dict[str, object] = {"register": row.to_document()}
        fixture.backend.calls.clear()
        try:
            self.assertEqual((row.start, row.end), (0x50000504, 0x50000508))
            self.assertTrue(row.readable and row.writable)
            self.assertFalse(row.prohibited)
            for category in (ActionCategory.MEMORY_READ, ActionCategory.REGISTER_WRITE):
                self.assertIsInstance(
                    fixture.document.safety_map.check(category, (requested,)), Allowed
                )
            write = {"address": row.start, "mask": 1, "value": 0}
            no_plan = self.invoke("register_write", write)
            details["no_plan_response"] = no_plan
            self.assertIn("Tool 'register_write' is locked", no_plan)
            self.assertIn("register_write-plan", no_plan)
            self.assertEqual(fixture.backend.calls, [])
            read = {"address": row.start, "width": 32, "length": None}
            details["read_plan"] = self.plan("read_memory_address", read)
            read_response = self.invoke("read_memory_address", read)
            details["read_response"] = read_response
            self.assertIn("0x410FC241", read_response)
            uid = fixture.server._handle(fixture.board_id).probe_uid
            self.assertEqual(
                fixture.backend.calls,
                [("read_memory", uid, row.start, 32), ("release_reset", uid)],
            )
            self.assertIsNone(
                fixture.server.plan_engine.active_plan("read_memory_address", fixture.board_id)
            )
            details["write_plan"] = self.plan("register_write", write)
            write_response = self.invoke("register_write", write)
            details["write_response"] = write_response
            self.assertIn("mapped peripheral write allowed", write_response)
            self.assertEqual(
                fixture.backend.calls,
                [
                    ("read_memory", uid, row.start, 32),
                    ("release_reset", uid),
                    ("read_memory", uid, row.start, 32),
                    ("write_memory", uid, row.start, 0x410FC240, 32),
                    ("release_reset", uid),
                ],
            )
            self.assertIsNone(
                fixture.server.plan_engine.active_plan("register_write", fixture.board_id)
            )
            before_repeat = list(fixture.backend.calls)
            repeated = self.invoke("register_write", write)
            details["exhausted_plan_response"] = repeated
            self.assertIn("Tool 'register_write' is locked", repeated)
            self.assertEqual(fixture.backend.calls, before_repeat)
        finally:
            observe_nrf_control(self, fixture, "independent-P0.OUT-control", **details)


if __name__ == "__main__":
    unittest.main()
