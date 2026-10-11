"""AT09: shipped manual-only skills and server use one project-root protocol."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

from pyocd_debug_mcp.capabilities.manual_permissions import ManualPermissionRepository

from tests.tiered_acceptance_support import fixture, isolated_project, manual_permission_root


# AT09 must be reproducible from a standalone clone.  The formerly referenced
# parent-directory workspace was developer-machine state and is not part of the
# repository, so it could make the acceptance suite fail (or pass against the
# wrong version) after cloning elsewhere.
WORKSPACE = Path(__file__).resolve().parents[1] / "client-workspace"
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
    def test_attach_reparse_detection_uses_windows_metadata(self) -> None:
        attach = _attach_project_module()
        marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)) or 0x400
        metadata = SimpleNamespace(st_file_attributes=marker, st_reparse_tag=0)
        self.assertTrue(attach._is_reparse_point(metadata))

    def test_attach_fails_before_publication_without_descriptor_paths(self) -> None:
        attach = _attach_project_module()
        with tempfile.TemporaryDirectory(prefix="tiered-attach-project-") as project_text:
            project = Path(project_text)
            existing = project / ".agent-workspace"
            existing.mkdir()
            marker = existing / "preserved.txt"
            marker.write_text("preserved\n", encoding="utf-8")

            with (
                patch.object(attach, "_descriptor_directory", return_value=None),
                self.assertRaisesRegex(OSError, "descriptor-path support"),
            ):
                attach._ensure_hidden_workspace_entry(project)

            self.assertEqual(marker.read_text(encoding="utf-8"), "preserved\n")
            self.assertEqual(list(project.glob(".agent-workspace-stage-*")), [])

    @unittest.skipUnless(os.name == "nt", "native Windows junction/reparse coverage")
    def test_native_windows_workspace_junction_is_rejected_without_external_writes(self) -> None:
        attach = _attach_project_module()
        with (
            tempfile.TemporaryDirectory(prefix="tiered-attach-project-") as project_text,
            tempfile.TemporaryDirectory(prefix="tiered-attach-external-") as external_text,
        ):
            project = Path(project_text)
            external = Path(external_text)
            junction = project / ".agent-workspace"
            created = subprocess.run(
                ["cmd", "/d", "/c", "mklink", "/J", str(junction), str(external)],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest(f"cannot create native junction: {created.stderr.strip()}")

            with self.assertRaisesRegex(ValueError, "reparse point"):
                attach._ensure_hidden_workspace_entry(project)
            self.assertEqual(list(external.iterdir()), [])

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

    def test_firmware_mode_selects_reporting_and_manual_sources_without_runtime_state(
        self,
    ) -> None:
        """AT09: attached mode exposes reporting while destructive skills stay manual."""

        mode = (WORKSPACE / "modes" / "firmware.toml").read_text(encoding="utf-8")
        self.assertIn('"byo-issue-report"', mode)
        reporting = WORKSPACE / "skills-src" / "firmware" / "byo-issue-report"
        reporting_source = (reporting / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("disable-model-invocation: true", reporting_source)
        self.assertTrue((reporting / "agents" / "openai.yaml").is_file())
        self.assertTrue((reporting / "templates" / "issue-report.json").is_file())
        self.assertTrue((reporting / "templates" / "routine-checkin.json").is_file())
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

    def test_attached_workspace_contains_the_selected_issue_reporting_skill(self) -> None:
        attach = _attach_project_module()
        with tempfile.TemporaryDirectory(prefix="tiered-attached-reporting-") as project_text:
            attached = attach._ensure_hidden_workspace_entry(Path(project_text))
            mode = (attached / "modes" / "firmware.toml").read_text(encoding="utf-8")
            self.assertIn('"byo-issue-report"', mode)
            reporting = attached / "skills-src" / "firmware" / "byo-issue-report"
            self.assertTrue((reporting / "SKILL.md").is_file())
            self.assertTrue((reporting / "agents" / "openai.yaml").is_file())
            self.assertTrue((reporting / "templates" / "issue-report.json").is_file())
            self.assertTrue((reporting / "templates" / "routine-checkin.json").is_file())

    def test_attached_runtime_state_is_ignored_by_git(self) -> None:
        attach = _attach_project_module()
        with tempfile.TemporaryDirectory(prefix="tiered-attached-gitignore-") as project_text:
            project = Path(project_text)
            subprocess.run(["git", "init", "--quiet", str(project)], check=True)
            attached = attach._ensure_hidden_workspace_entry(project)
            runtime_record = attached / "runtime" / "grant.json"
            runtime_record.write_text('{"private": true}\n', encoding="utf-8")

            ignored = subprocess.run(
                ["git", "check-ignore", "--quiet", str(runtime_record)],
                cwd=project,
                check=False,
            )

            self.assertEqual(ignored.returncode, 0)
            self.assertEqual((attached / ".gitignore").read_text(encoding="utf-8"), "/runtime/\n")

    def test_server_only_start_does_not_create_an_untracked_client_workspace(self) -> None:
        with (
            tempfile.TemporaryDirectory(prefix="tiered-server-only-") as project_text,
            tempfile.TemporaryDirectory(prefix="tiered-server-monitor-") as monitor_text,
        ):
            project = Path(project_text)
            subprocess.run(["git", "init", "--quiet", str(project)], check=True)
            (project / ".gitignore").write_text("/.firm/\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore"], cwd=project, check=True)
            before = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout
            environment = os.environ.copy()
            environment["BYO_MCP_ARTIFACT_ROOT"] = str(project)
            # This test isolates server import from the client-workspace
            # installer. Keep the independent monitor store outside the Git
            # project even when the host's application-data path is unusable.
            environment["BYO_MCP_MONITOR_ROOT"] = monitor_text
            environment["PYOCD_CAN_MODE"] = "off"
            for name in ("PYOCD_CAN_CONFIG", "PYOCD_CAN_CONFIG_SHA256", "PYOCD_CAN_READ_SECONDS"):
                environment.pop(name, None)

            imported = subprocess.run(
                [sys.executable, "-c", "import pyocd_debug_mcp.server"],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertFalse((project / ".agent-workspace").exists())
            after = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout
            self.assertEqual(after, before)

    def test_server_start_honors_a_later_runtime_ignore_negation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tiered-server-ignore-negation-") as project_text:
            project = Path(project_text)
            subprocess.run(["git", "init", "--quiet", str(project)], check=True)
            (project / ".gitignore").write_text(
                "/.firm/\n/.agent-workspace/runtime/\n!/.agent-workspace/runtime/\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["BYO_MCP_ARTIFACT_ROOT"] = str(project)
            environment["PYOCD_CAN_MODE"] = "off"
            for name in ("PYOCD_CAN_CONFIG", "PYOCD_CAN_CONFIG_SHA256", "PYOCD_CAN_READ_SECONDS"):
                environment.pop(name, None)

            imported = subprocess.run(
                [sys.executable, "-c", "import pyocd_debug_mcp.server"],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertFalse((project / ".agent-workspace").exists())
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout
            self.assertNotIn(".agent-workspace", status)

    def test_pre_git_root_ignore_with_wildcard_negations_is_not_authority(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="tiered-pre-git-negation-",
            dir="/tmp" if os.name != "nt" else None,
        ) as project_text:
            project = Path(project_text)
            (project / ".gitignore").write_text(
                "/.agent-workspace/runtime/\n"
                "!/.agent-workspace/runtime*/\n"
                "!/.agent-workspace/runtime/**\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["BYO_MCP_ARTIFACT_ROOT"] = str(project)
            environment["PYOCD_CAN_MODE"] = "off"
            for name in ("PYOCD_CAN_CONFIG", "PYOCD_CAN_CONFIG_SHA256", "PYOCD_CAN_READ_SECONDS"):
                environment.pop(name, None)

            imported = subprocess.run(
                [sys.executable, "-c", "import pyocd_debug_mcp.server"],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertFalse((project / ".agent-workspace" / "runtime").exists())

    def test_pre_git_canonical_attached_ignore_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="tiered-pre-git-attached-",
            dir="/tmp" if os.name != "nt" else None,
        ) as project_text:
            project = Path(project_text)
            attached = project / ".agent-workspace"
            attached.mkdir()
            (attached / ".gitignore").write_bytes(b"/runtime/\n")

            from pyocd_debug_mcp import server

            self.assertTrue(server._manual_runtime_is_ignored(project))

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

    def test_attach_rejects_top_level_workspace_symlink_without_external_writes(self) -> None:
        attach = _attach_project_module()
        with (
            tempfile.TemporaryDirectory(prefix="tiered-attach-project-") as project_text,
            tempfile.TemporaryDirectory(prefix="tiered-attach-external-") as external_text,
        ):
            project = Path(project_text)
            external = Path(external_text)
            (project / ".agent-workspace").symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symbolic link"):
                attach._ensure_hidden_workspace_entry(project)

            self.assertEqual(list(external.iterdir()), [])

    def test_attach_rejects_nested_workspace_symlink_without_external_writes(self) -> None:
        attach = _attach_project_module()
        with (
            tempfile.TemporaryDirectory(prefix="tiered-attach-project-") as project_text,
            tempfile.TemporaryDirectory(prefix="tiered-attach-external-") as external_text,
        ):
            project = Path(project_text)
            external = Path(external_text)
            destination = project / ".agent-workspace"
            destination.mkdir()
            (destination / "bin").symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symbolic link"):
                attach._ensure_hidden_workspace_entry(project)

            self.assertEqual(list(external.iterdir()), [])

    def test_attach_rejects_hardlinked_workspace_file_without_overwriting_peer(self) -> None:
        attach = _attach_project_module()
        with (
            tempfile.TemporaryDirectory(prefix="tiered-attach-project-") as project_text,
            tempfile.TemporaryDirectory(prefix="tiered-attach-external-") as external_text,
        ):
            project = Path(project_text)
            external = Path(external_text)
            victim = external / "victim.txt"
            victim.write_text("do not replace\n", encoding="utf-8")
            destination = project / ".agent-workspace"
            destination.mkdir()
            os.link(victim, destination / "README.md")

            with self.assertRaisesRegex(ValueError, "multiply linked"):
                attach._ensure_hidden_workspace_entry(project)

            self.assertEqual(victim.read_text(encoding="utf-8"), "do not replace\n")

    def test_attach_copy_time_symlink_swap_cannot_write_outside_staging_tree(self) -> None:
        attach = _attach_project_module()
        with (
            tempfile.TemporaryDirectory(prefix="tiered-attach-project-") as project_text,
            tempfile.TemporaryDirectory(prefix="tiered-attach-external-") as external_text,
        ):
            project = Path(project_text)
            external = Path(external_text)
            original_copytree = attach.shutil.copytree
            swapped = False

            def swap_target(*args: Any, **kwargs: Any) -> Path:
                nonlocal swapped
                source, target = Path(args[0]), Path(args[1])
                if not swapped and source == WORKSPACE / "bin":
                    Path(target).symlink_to(external, target_is_directory=True)
                    swapped = True
                return original_copytree(*args, **kwargs)

            with (
                patch.object(attach.shutil, "copytree", side_effect=swap_target),
                self.assertRaises((FileExistsError, ValueError)),
            ):
                attach._ensure_hidden_workspace_entry(project)

            self.assertEqual(list(external.iterdir()), [])
            self.assertFalse((project / ".agent-workspace").exists())

        with tempfile.TemporaryDirectory(prefix="tiered-attach-root-parent-") as parent_text:
            parent = Path(parent_text)
            project = parent / "project"
            external = parent / "external"
            displaced = parent / "project-original"
            project.mkdir()
            external.mkdir()
            original_temporary = attach._temporary_sibling
            swapped_root = False

            def swap_project_root(root: Path, prefix: str) -> Path:
                nonlocal swapped_root
                if not swapped_root:
                    os.replace(project, displaced)
                    project.symlink_to(external, target_is_directory=True)
                    swapped_root = True
                return original_temporary(root, prefix)

            try:
                with (
                    patch.object(attach, "_temporary_sibling", side_effect=swap_project_root),
                    self.assertRaisesRegex(ValueError, "project root.*symbolic link"),
                ):
                    attach._ensure_hidden_workspace_entry(project)
                self.assertEqual(list(external.iterdir()), [])
            finally:
                if project.is_symlink():
                    project.unlink()
                if displaced.exists():
                    os.replace(displaced, project)

        with (
            tempfile.TemporaryDirectory(prefix="tiered-attach-project-") as project_text,
            tempfile.TemporaryDirectory(prefix="tiered-attach-external-") as external_text,
        ):
            project = Path(project_text)
            external = Path(external_text)
            destination = project / ".agent-workspace"
            preserved = destination / "thirdparty"
            preserved.mkdir(parents=True)
            (preserved / "safe.txt").write_text("safe\n", encoding="utf-8")
            (external / "private-key.txt").write_text("secret\n", encoding="utf-8")
            original_merge = attach._merge_preserved_assets
            swapped_source = False

            def swap_preserved_source(source: Path, stage: Path) -> None:
                nonlocal swapped_source
                candidate = source / "thirdparty"
                if not swapped_source and candidate.exists():
                    moved = project / "preserved-original"
                    os.replace(candidate, moved)
                    candidate.symlink_to(external, target_is_directory=True)
                    swapped_source = True
                original_merge(source, stage)

            with (
                patch.object(attach, "_merge_preserved_assets", side_effect=swap_preserved_source),
                self.assertRaisesRegex(ValueError, "preserved source.*symbolic link"),
            ):
                attach._ensure_hidden_workspace_entry(project)
            self.assertFalse((project / ".agent-workspace").exists())
            self.assertFalse(any(project.glob(".agent-workspace-stage-*")))

        with (
            tempfile.TemporaryDirectory(prefix="tiered-attach-project-") as project_text,
            tempfile.TemporaryDirectory(prefix="tiered-attach-external-") as external_text,
        ):
            project = Path(project_text)
            external = Path(external_text)
            created_stage: list[Path] = []
            original_temporary = attach._temporary_sibling
            original_copy = attach._copy_immutable_assets

            def remember_stage(root: Path, prefix: str) -> Path:
                stage = original_temporary(root, prefix)
                created_stage.append(stage)
                return stage

            def swap_whole_stage(stage_access: Path, identity: tuple[int, int]) -> None:
                stage = created_stage[0]
                displaced = project / "displaced-stage"
                os.replace(stage, displaced)
                stage.symlink_to(external, target_is_directory=True)
                original_copy(stage_access, identity)

            with (
                patch.object(attach, "_temporary_sibling", side_effect=remember_stage),
                patch.object(attach, "_copy_immutable_assets", side_effect=swap_whole_stage),
                self.assertRaisesRegex(ValueError, "changed during attachment|symbolic link"),
            ):
                attach._ensure_hidden_workspace_entry(project)

            self.assertEqual(list(external.iterdir()), [])
            self.assertFalse((project / ".agent-workspace").exists())

    def test_attach_replaces_permissive_mode_and_preserves_private_runtime(self) -> None:
        attach = _attach_project_module()
        with tempfile.TemporaryDirectory(prefix="tiered-attach-project-") as project_text:
            project = Path(project_text)
            destination = project / ".agent-workspace"
            runtime = destination / "runtime"
            runtime.mkdir(parents=True)
            state = runtime / "state.json"
            state.write_text('{"preserved":true}\n', encoding="utf-8")
            os.chmod(destination, 0o777)
            os.chmod(runtime, 0o777)

            attached = attach._ensure_hidden_workspace_entry(project)

            self.assertEqual(stat.S_IMODE(attached.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((attached / "runtime").stat().st_mode), 0o700)
            self.assertEqual((attached / "runtime/state.json").read_bytes(), state.read_bytes())

    def test_attach_preserves_unrelated_top_level_and_nested_workspace_assets(self) -> None:
        attach = _attach_project_module()
        with tempfile.TemporaryDirectory(prefix="tiered-attach-project-") as project_text:
            project = Path(project_text)
            destination = project / ".agent-workspace"
            nested = destination / "skills-src" / "other-plugin" / "SKILL.md"
            nested.parent.mkdir(parents=True)
            nested.write_text("third-party skill\n", encoding="utf-8")
            top_level = destination / "operator-settings.toml"
            top_level.write_text("theme = 'custom'\n", encoding="utf-8")

            attached = attach._ensure_hidden_workspace_entry(project)

            self.assertEqual(
                (attached / "skills-src/other-plugin/SKILL.md").read_text(encoding="utf-8"),
                "third-party skill\n",
            )
            self.assertEqual(
                (attached / "operator-settings.toml").read_text(encoding="utf-8"),
                "theme = 'custom'\n",
            )
            self.assertTrue((attached / "bin/attach-project").is_file())

    def test_attach_serializes_concurrent_replacements_without_losing_runtime(self) -> None:
        attach = _attach_project_module()
        with tempfile.TemporaryDirectory(prefix="tiered-attach-project-") as project_text:
            project = Path(project_text)
            runtime = project / ".agent-workspace" / "runtime"
            runtime.mkdir(parents=True)
            (runtime / "grant.json").write_text('{"grant":true}\n', encoding="utf-8")
            moved = threading.Event()
            release = threading.Event()
            original_replace = attach.os.replace
            paused = False

            def pause_after_backup(source: Path, target: Path, *args: Any, **kwargs: Any) -> None:
                nonlocal paused
                is_destination = False
                try:
                    is_destination = os.path.samefile(source, project / ".agent-workspace")
                except OSError:
                    pass
                original_replace(source, target, *args, **kwargs)
                if not paused and is_destination:
                    paused = True
                    moved.set()
                    self.assertTrue(release.wait(5.0))

            failures: list[BaseException] = []

            def run_attach() -> None:
                try:
                    attach._ensure_hidden_workspace_entry(project)
                except BaseException as exc:  # pragma: no cover - asserted below
                    failures.append(exc)

            with patch.object(attach.os, "replace", side_effect=pause_after_backup):
                first = threading.Thread(target=run_attach)
                second = threading.Thread(target=run_attach)
                first.start()
                self.assertTrue(moved.wait(5.0))
                second.start()
                release.set()
                first.join(10.0)
                second.join(10.0)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(
                (project / ".agent-workspace/runtime/grant.json").read_text(encoding="utf-8"),
                '{"grant":true}\n',
            )
            self.assertEqual(list(project.glob(".agent-workspace-backup-*")), [])

    def test_attach_symlink_swap_does_not_chmod_external_directory(self) -> None:
        attach = _attach_project_module()
        with (
            tempfile.TemporaryDirectory(prefix="tiered-attach-project-") as project_text,
            tempfile.TemporaryDirectory(prefix="tiered-attach-external-") as external_text,
        ):
            project = Path(project_text)
            external = Path(external_text)
            destination = project / ".agent-workspace"
            destination.mkdir()
            displaced = project / "displaced-workspace"
            os.chmod(external, 0o755)
            original_replace = attach.os.replace
            swapped = False

            def swap_before_move(source: Path, target: Path, *args: Any, **kwargs: Any) -> None:
                nonlocal swapped
                is_destination = False
                try:
                    is_destination = os.path.samefile(source, destination)
                except OSError:
                    pass
                if not swapped and is_destination:
                    swapped = True
                    original_replace(destination, displaced)
                    destination.symlink_to(external, target_is_directory=True)
                original_replace(source, target, *args, **kwargs)

            with (
                patch.object(attach.os, "replace", side_effect=swap_before_move),
                self.assertRaisesRegex(ValueError, "changed during attachment"),
            ):
                attach._ensure_hidden_workspace_entry(project)

            self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o755)
            self.assertEqual(list(external.iterdir()), [])

        with (
            tempfile.TemporaryDirectory(prefix="tiered-attach-project-") as project_text,
            tempfile.TemporaryDirectory(prefix="tiered-attach-external-") as external_text,
        ):
            project = Path(project_text)
            external = Path(external_text)
            displaced = project / "displaced-final-stage"
            original_require = attach._require_directory_identity
            swapped = False

            def swap_after_final_stage_check(
                path: Path,
                expected: tuple[int, int],
                *,
                label: str,
            ) -> None:
                nonlocal swapped
                original_require(path, expected, label=label)
                if not swapped and label == "staged workspace":
                    swapped = True
                    os.replace(path, displaced)
                    path.symlink_to(external, target_is_directory=True)

            with (
                patch.object(
                    attach,
                    "_require_directory_identity",
                    side_effect=swap_after_final_stage_check,
                ),
                self.assertRaisesRegex(ValueError, "changed during publication"),
            ):
                attach._ensure_hidden_workspace_entry(project)

            self.assertFalse((project / ".agent-workspace").exists())
            self.assertEqual(list(external.iterdir()), [])

        with (
            tempfile.TemporaryDirectory(prefix="tiered-attach-project-") as project_text,
            tempfile.TemporaryDirectory(prefix="tiered-attach-external-") as external_text,
        ):
            project = Path(project_text)
            external = Path(external_text)
            os.chmod(external, 0o755)
            original_copy = attach._copy_immutable_assets

            def insert_runtime_link(stage: Path, identity: tuple[int, int]) -> None:
                original_copy(stage, identity)
                (stage / "runtime").symlink_to(external, target_is_directory=True)

            with (
                patch.object(attach, "_copy_immutable_assets", side_effect=insert_runtime_link),
                self.assertRaises((FileExistsError, ValueError)),
            ):
                attach._ensure_hidden_workspace_entry(project)

            self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o755)
            self.assertEqual(list(external.iterdir()), [])

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
