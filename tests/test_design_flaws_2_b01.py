"""Public workflow regressions for project paths, pyOCD lookup and report privacy."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pyocd.core.session import Session

from pyocd_debug_mcp.adapters import swd_pyocd
from pyocd_debug_mcp.artifact_collector import ArtifactRole, collect_artifacts
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyError, CapabilityPolicyRepository
from pyocd_debug_mcp.discovery_failures import no_native_probe_failure, no_native_uart_failure
from pyocd_debug_mcp.firmstore.profiles import _display_identity, _require_board_id, ProfileError
from pyocd_debug_mcp.guardrails.flash_gate import resolve_flash_request
from pyocd_debug_mcp.guardrails.plan_defs import definition_for_action
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine
from pyocd_debug_mcp.project_paths import resolve_project_path
from pyocd_debug_mcp.services.session_runtime import ActionContext
from pyocd_debug_mcp.services.symbols import _normalize_elf_path
from pyocd_debug_mcp.tools.artifacts import build_artifact_handlers
from tests.monitor_support import MonitorTestCase
from tests.tiered_acceptance_support import isolated_project, tiered_test_server


class ProjectPathWorkflows(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        self.project, self.runtime = root / "project", root / "runtime"
        self.project.mkdir()
        self.runtime.mkdir()
        (self.project / "app.elf").write_bytes(b"\x7fELF-project")
        (self.runtime / "app.elf").write_bytes(b"wrong-runtime-file")
        original_cwd = Path.cwd()
        self.addCleanup(os.chdir, original_cwd)
        os.chdir(self.runtime)
        environment = patch.dict(os.environ, {
            "BYO_MCP_ARTIFACT_ROOT": str(self.project),
            "BYO_RUNTIME_ROOT": str(self.runtime),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_symbols_and_plan_and_flash_use_same_project_artifact(self):
        expected = self.project / "app.elf"
        self.assertEqual(_normalize_elf_path("app.elf"), expected)
        definition = definition_for_action("flash_application")
        binding = PlanEngine._bind_artifact(None, definition, {"artifact": "app.elf"}, session_id=None)
        self.assertEqual(binding.resolved_path, expected)
        request = resolve_flash_request(
            object(), explicit_path="app.elf",
            action_context=ActionContext("test", "flash_application"),
        )
        self.assertEqual(request.artifact_path, expected)
        self.assertEqual(request.identity.sha256, binding.sha256)

    def test_mcp_collector_reads_and_writes_in_project(self):
        result = json.loads(build_artifact_handlers()["collect_build_artifacts"](
            output_dir="dist/bundle", elf_path="app.elf",
        ))
        self.assertEqual(Path(result["output_dir"]), self.project / "dist/bundle")
        self.assertFalse((self.runtime / "dist").exists())
        self.assertEqual(Path(result["canonical_paths"]["elf"]).read_bytes(), b"\x7fELF-project")

    def test_absolute_runtime_output_refused_before_creating_directories(self):
        destination = self.runtime / "new" / "bundle"
        with self.assertRaisesRegex(ValueError, "runtime"):
            collect_artifacts({ArtifactRole.ELF: self.project / "app.elf"}, destination)
        self.assertFalse(destination.parent.exists())

    def test_absolute_input_is_not_rebased(self):
        self.assertEqual(resolve_project_path(self.runtime / "app.elf"), self.runtime / "app.elf")

    def test_standalone_cli_retains_cwd_resolution(self):
        with patch.dict(os.environ, {"BYO_MCP_ARTIFACT_ROOT": "", "BYO_RUNTIME_ROOT": ""}):
            result = collect_artifacts({ArtifactRole.ELF: "app.elf"}, "cli-bundle")
        self.assertEqual(result.output_dir, self.runtime / "cli-bundle")


class SessionLookupIsolation(unittest.TestCase):
    def test_real_pyocd_script_control_then_isolated_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            marker = root / "script-ran"
            script = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
            (root / "pyocd_user.py").write_text(script, encoding="utf-8")
            (root / "pyocd.yaml").write_text("frequency: 123\n", encoding="utf-8")
            previous = Path.cwd()
            self.addCleanup(os.chdir, previous)
            os.chdir(root)

            class Probe:
                unique_id = "fixture-probe"

                def create_associated_board(self):
                    return SimpleNamespace(target=None)

            # Positive control: this actual dependency/probe fixture loads scripts.
            ordinary = Session(Probe(), auto_open=False, options={"project_dir": str(root)})
            self.assertTrue(marker.exists())
            ordinary.close()
            marker.unlink()

            with (
                patch.dict(os.environ, {"PYOCD_PROJECT_DIR": str(root)}),
                patch.object(swd_pyocd, "_choose_unique_probe", return_value=Probe()),
            ):
                session = swd_pyocd.PyOCDSWDInterface._choose_session(probe_uid="fixture-probe", options={})
            self.assertFalse(marker.exists())
            self.assertNotEqual(session.options.get("frequency"), 123)
            isolated = Path(session.project_dir)
            self.assertTrue(isolated.is_dir())
            self.assertEqual(Path.cwd(), root)
            swd_pyocd._close_session(session)
            self.assertFalse(isolated.exists())
            os.chdir(previous)

    def test_constructor_failure_restores_cwd_and_removes_empty_folder(self):
        captured = []
        previous = Path.cwd()

        def fail(*args, **kwargs):
            folder = Path(kwargs["options"]["project_dir"])
            captured.append(folder)
            os.chdir(folder)
            raise RuntimeError("fixture failure")

        with (
            patch.object(swd_pyocd, "_choose_unique_probe", return_value=object()),
            patch.object(swd_pyocd, "Session", side_effect=fail),
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                swd_pyocd.PyOCDSWDInterface._choose_session(probe_uid=None, options={})
        self.assertEqual(Path.cwd(), previous)
        self.assertFalse(captured[0].exists())


class MechanicalReportPrivacy(MonitorTestCase):
    def test_raw_exception_paths_and_identifiers_do_not_enter_any_report_field(self):
        monitor = self.make_monitor()
        monitor.bind_workspace(None)
        error = RuntimeError("cannot open /clients/acme-secret/build/app.elf for probe-SECRET-ID")
        monitor.begin("read_memory", {}, None).failed(error)
        files = self.report_files()
        self.assertEqual(len(files), 1)
        report = json.loads(files[0].read_text(encoding="utf-8"))
        serialized = json.dumps(report)
        self.assertNotIn("acme-secret", serialized)
        self.assertNotIn("probe-SECRET-ID", serialized)
        self.assertIn("RuntimeError", report["description"])
        self.assertTrue(report["error_signature"])


class DiscoveryAndIdentity(unittest.TestCase):
    def test_installed_remedies_do_not_require_checkout_or_uv_commands(self):
        with patch.dict(os.environ, {"BYO_SIDECAR_COMPILED": "1"}):
            probe = no_native_probe_failure()
            uart = no_native_uart_failure()
        for failure in (probe, uart):
            self.assertNotIn("uv run", failure.message)
            self.assertNotIn("From the BYO Server checkout", failure.message)
            self.assertIn("setup_overview", failure.message)

    def test_source_diagnostics_remain_actionable(self):
        with patch.dict(os.environ, {"BYO_SIDECAR_COMPILED": ""}):
            self.assertIn("uv run", no_native_probe_failure().message)

    def test_shared_profile_equivalence_handles_spaces_and_compatibility_characters(self):
        for value in ("Nucleo", "Nucleo ", "Ｎucleo"):
            self.assertEqual(_display_identity(value), "nucleo")

    def test_reserved_policy_ids_are_refused_before_directory_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = CapabilityPolicyRepository(Path(directory))
            for value in ("aux", "con", "nul", "com1", "lpt9"):
                with self.subTest(value=value):
                    with self.assertRaises(CapabilityPolicyError):
                        repository.resolve(value)
                    with self.assertRaises(ProfileError):
                        _require_board_id(value)
            self.assertFalse((Path(directory) / ".firm").exists())

    def test_setup_names_and_duplicate_index_use_the_same_identity(self):
        with isolated_project() as project, tiered_test_server(project) as server:
            self.assertEqual(server._proposed_board_id("Aux", set()), "aux_board")
            self.assertEqual(server._proposed_board_id("Aux", {"aux_board"}), "aux_board_2")
            profiles = [
                SimpleNamespace(board_id="first", display_name="Nucleo"),
                SimpleNamespace(board_id="second", display_name="Ｎucleo "),
            ]
            with patch.object(server._profile_repository, "load_all", return_value=profiles):
                result = server._setup_overview(None)
            self.assertEqual(result["status"], "setup_overview_blocked")
            self.assertIn("Duplicate", result["agent_prompt"])


if __name__ == "__main__":
    unittest.main()
