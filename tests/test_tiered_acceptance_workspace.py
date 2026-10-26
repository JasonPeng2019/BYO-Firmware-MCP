"""AT09: shipped manual-only skills and server use one project-root protocol.

Set BYO_MCP_TEST_WORKSPACE_ROOT to the paired workspace source checkout when
testing another layout. Otherwise use sibling CodexClaudeWorkflow or the legacy
sibling .agent-workspace source. Runtime grant state is never a source fixture.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from dataclasses import asdict
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType, SimpleNamespace

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

from tests.test_generic_builtin_safety_map import nrf_registered_pack_fixture, observe_nrf_control
from tests.tiered_acceptance_support import (
    fixture,
    isolated_project,
    manual_permission_root,
    public_text,
)


def _workspace_source() -> Path:
    """Select explicit fixture input or the supported sibling source layout."""
    configured = os.environ.get("BYO_MCP_TEST_WORKSPACE_ROOT")
    if configured:
        # A bad explicit input must stay visible rather than selecting other code.
        return Path(configured).expanduser().resolve(strict=True)
    siblings = Path(__file__).resolve().parents[2]
    checkout = siblings / "CodexClaudeWorkflow"
    if (checkout / "internal" / "manual_permissions.py").is_file():
        return checkout
    return siblings / ".agent-workspace"


WORKSPACE = _workspace_source()
WORKSPACE_HELPER = WORKSPACE / "internal" / "manual_permissions.py"
ATTACH_PROJECT = WORKSPACE / "bin" / "attach-project"
HARDWARE_RUNNER = Path(__file__).with_name("manual") / "manual_tiered_hardware_check.py"
BOARD = "left_controller"
POLICY = "a" * 64
BINDING = "b" * 64


def _workspace_helper(path: Path = WORKSPACE_HELPER):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("tiered_workspace_manual_permissions", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load shipped workspace helper: {WORKSPACE_HELPER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _attach_project_module():  # type: ignore[no-untyped-def]
    loader = SourceFileLoader("tiered_workspace_attach_project", str(ATTACH_PROJECT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load attached-workspace installer: {ATTACH_PROJECT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _hardware_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "tiered_hardware_runner_for_test", HARDWARE_RUNNER
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load AT10 runner: {HARDWARE_RUNNER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _RunnerSession:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = list(payloads)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(self, tool: str, arguments: dict[str, object]) -> SimpleNamespace:
        self.calls.append((tool, arguments))
        payload = self.payloads.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))], isError=False)


class TieredWorkspaceAcceptanceTests(unittest.TestCase):
    def test_server_and_shipped_helper_share_exact_root_epoch_schema_and_binding_protocol(
        self,
    ) -> None:
        """AT09/C08: an isolated customer project can grant exactly what the server consumes."""

        helper = _workspace_helper()
        with isolated_project() as root:
            repository = ManualPermissionRepository(root, "run-fixture")
            repository.reset_startup()
            grant = helper.unlock_manual_grant(
                project_root=root,
                action="downgrade",
                board_id=BOARD,
                policy_digest=POLICY,
            )
            self.assertEqual(grant["status"], "manual_grant_unlocked")
            self.assertEqual(grant["project_root"], str(root))
            self.assertEqual(grant["server_run_id"], "run-fixture")
            self.assertEqual(grant["policy_digest"], POLICY)
            record = json.loads(
                (manual_permission_root(root) / "downgrade.json").read_text(encoding="utf-8")
            )
            self.assertEqual(record["action"], "downgrade")
            self.assertEqual(record["state"], "unlocked")
            self.assertEqual(record["server_run_id"], "run-fixture")
            self.assertEqual(record["grant_id"], grant["grant_id"])
            self.assertEqual(record["board_id"], BOARD)
            self.assertEqual(record["policy_digest"], POLICY)
            self.assertIsNone(record["binding_digest"])

            token = repository.issue_downgrade_token(BOARD, str(grant["grant_id"]), POLICY)
            repository.consume_downgrade(BOARD, token, POLICY)
            self.assertEqual(repository.status("downgrade").state, "consumed")

    def test_mass_erase_helper_needs_its_own_disclosure_binding_and_cannot_supply_downgrade(
        self,
    ) -> None:
        """AT07/AT09: action records are not interchangeable grants."""

        helper = _workspace_helper()
        with isolated_project() as root:
            repository = ManualPermissionRepository(root, "run-fixture")
            repository.reset_startup()
            missing = helper.unlock_manual_grant(
                project_root=root,
                action="mass-erase",
                board_id=BOARD,
                policy_digest=POLICY,
            )
            self.assertEqual(missing["status"], "refused")
            self.assertEqual(missing["code"], "manual/binding-mismatch")
            result = helper.unlock_manual_grant(
                project_root=root,
                action="mass-erase",
                board_id=BOARD,
                policy_digest=POLICY,
                binding_digest=BINDING,
            )
            self.assertEqual(result["status"], "manual_grant_unlocked")
            claim = repository.reserve_mass_erase(BOARD, str(result["grant_id"]), POLICY, BINDING)
            repository.consume_mass_erase(BOARD, POLICY, BINDING, claim)
            self.assertEqual(repository.status("mass-erase").state, "consumed")
            self.assertEqual(repository.status("downgrade").state, "locked")

    def test_firmware_mode_selects_both_manual_only_sources_without_shipping_runtime_state(
        self,
    ) -> None:
        """AT09: the actual mode selection references both source skill directories."""

        mode = (WORKSPACE / "modes" / "firmware.toml").read_text(encoding="utf-8")
        for name in ("downgrade", "mass-erase"):
            self.assertIn(f'"{name}"', mode)
            skill = WORKSPACE / "skills-src" / "firmware" / name / "SKILL.md"
            source = skill.read_text(encoding="utf-8")
            self.assertIn("disable-model-invocation: true", source)
            self.assertIn("user-invocable: true", source)
            self.assertIn("manual-permission", source)
            self.assertTrue((skill.parent / "agents" / "openai.yaml").is_file())
        shipped_runtime = list((WORKSPACE / "skills-src").rglob("manual-permissions"))
        self.assertEqual(
            shipped_runtime, [], "runtime grants must not live under shipped skill sources"
        )

    def test_protocol_fixture_has_the_frozen_schema_without_runtime_artifacts(self) -> None:
        """AT09: fixture data checks compatibility, not a claim that human invocation was proven."""

        document = fixture("workspace_protocol.json")
        self.assertEqual(
            document["project_relative_directory"], ".agent-workspace/runtime/manual-permissions"
        )
        self.assertEqual(document["epoch"]["schema"], 1)
        self.assertEqual(document["downgrade"]["action"], "downgrade")
        self.assertEqual(document["downgrade"]["binding_digest"], None)
        self.assertEqual(document["mass_erase"]["action"], "mass-erase")
        self.assertEqual(document["mass_erase"]["binding_digest"], BINDING)
        for action in ("downgrade", "mass_erase"):
            self.assertEqual(document[action]["state"], "unlocked")
            self.assertIn("grant_id", document[action])

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

    def test_at10_runner_rejects_old_fixture_and_verifies_public_responses(self) -> None:
        """AT10: the hardware checklist is strict before transport and during each call."""

        runner = _hardware_runner()
        with self.assertRaisesRegex(runner.FixtureError, "schema_version must be 2"):
            runner.validate_fixture_document({"schema_version": 1})

        session = _RunnerSession([{"status": "ok", "result": 37}, {"status": "ok", "echo": 37}])

        async def no_pause(_message: str) -> None:
            raise AssertionError("this fixture has no operator pause")

        with tempfile.TemporaryDirectory() as temporary:
            rows, captures = asyncio.run(
                runner.run_steps(
                    session,
                    Path(temporary),
                    {"board_id": "physical_board"},
                    [
                        {
                            "id": "initial_read",
                            "tool": "read_memory_raw",
                            "arguments": {"board_id": "physical_board"},
                            "expect": {"payload": {"status": "ok", "result": 37}},
                            "capture": "initial",
                        },
                        {
                            "id": "restore",
                            "tool": "write_memory_raw",
                            "arguments": {"value": "$capture.initial.result"},
                            "expect": {"payload": {"status": "ok", "echo": 37}},
                        },
                    ],
                    pause=no_pause,
                )
            )
        self.assertTrue(all(row["passed"] for row in rows))
        self.assertEqual(captures["initial"], {"status": "ok", "result": 37})
        self.assertEqual(session.calls[1], ("write_memory_raw", {"value": 37}))

        failed = _RunnerSession([{"status": "error", "code": "backend/read-failed"}])
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(runner.CheckFailed, "expected 'ok'"),
        ):
            asyncio.run(
                runner.run_steps(
                    failed,
                    Path(temporary),
                    {"board_id": "physical_board"},
                    [
                        {
                            "id": "raw_read",
                            "tool": "read_memory_raw",
                            "arguments": {},
                            "expect": {"payload": {"status": "ok"}},
                        }
                    ],
                    pause=no_pause,
                )
            )

    def test_at10_runner_rejects_a_semantically_fake_checklist_step_before_transport(self) -> None:
        """AT10: an ID cannot disguise get_capabilities as a safe contained read."""

        runner = _hardware_runner()
        board = {
            "board_id": "physical_board",
            "probe_uid": "probe-a",
            "target": "fixture-target",
            "serial_binding": "fixture-serial",
            "expected_tier": "setup-lite",
        }
        other = {
            "board_id": "other_board",
            "probe_uid": "probe-b",
            "target": "other-target",
            "serial_binding": "other-serial",
        }
        with self.assertRaisesRegex(runner.FixtureError, "action_batch"):
            runner._validate_step_contract(
                "HW02",
                {
                    "id": "safe_read",
                    "tool": "get_capabilities",
                    "arguments": {"board_id": "physical_board", "address": 0x20000000},
                    "expect": {"payload": {"status": "ok", "operation": "read_memory_address"}},
                },
                board,
                [other],
                "fixture.cases.HW02.boards.nrf52840.steps[0]",
            )


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
        rows = {row.name: row for row in fixture.geometry.peripheral_regions}
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
            self.assertEqual(fixture.backend.calls, [("read_memory", uid, row.start, 32)])
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
                    ("read_memory", uid, row.start, 32),
                    ("write_memory", uid, row.start, 0x410FC240, 32),
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
