"""AT09: shipped manual-only skills and server use one project-root protocol."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType, SimpleNamespace

from pyocd_debug_mcp.capabilities.manual_permissions import ManualPermissionRepository

from tests.tiered_acceptance_support import fixture, isolated_project, manual_permission_root


WORKSPACE = Path(__file__).resolve().parents[2] / ".agent-workspace"
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


if __name__ == "__main__":
    unittest.main()
