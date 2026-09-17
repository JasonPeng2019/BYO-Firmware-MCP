"""The compiled-sidecar entry point keeps the installer contract executable."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp import __version__
from pyocd_debug_mcp import sidecar


class SidecarRuntimeTests(unittest.TestCase):
    @staticmethod
    def _release_manifest() -> dict[str, object]:
        return {
            "schema": 1,
            "product": "byo",
            "version": __version__,
            "sidecar_protocol": 1,
            "worker_protocol": 1,
            "workflow_protocol": 1,
            "capsule_schema": 1,
            "project_state_schema": 1,
        }

    def test_self_test_validates_the_release_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw_runtime:
            runtime = Path(raw_runtime)
            (runtime / "release-manifest.json").write_text(
                json.dumps(self._release_manifest()),
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = sidecar.main(
                    [
                        "self-test",
                        "--runtime-root",
                        str(runtime),
                        "--launcher-version",
                        __version__,
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "passed")

    def test_compiled_worker_uses_the_sidecar_with_explicit_runtime_context(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        runtime = Path(tempfile.mkdtemp()).resolve()
        executable = runtime / "sidecar" / "byo-mcp-sidecar"

        with (
            patch.object(sidecar, "_is_compiled", return_value=True),
            patch.object(sidecar, "_running_sidecar", return_value=executable),
        ):
            argv = sidecar._provider_worker_argv(project, runtime, __version__, 1)

        self.assertEqual(
            argv,
            (
                str(executable),
                "provider-worker",
                "--project-root",
                str(project),
                "--runtime-root",
                str(runtime),
                "--launcher-version",
                __version__,
                "--workflow-protocol",
                "1",
            ),
        )

    def test_runtime_context_replaces_ambient_roots_and_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            project = root / "project"
            runtime = root / "runtime"
            project.mkdir()
            runtime.mkdir()
            (project / ".agent-workspace").mkdir()
            (project / ".agent-workspace" / "manifest.json").write_text(
                json.dumps({"schema": 1, "product": "byo", "workflow_protocol": 1}),
                encoding="utf-8",
            )
            (runtime / "release-manifest.json").write_text(
                json.dumps(self._release_manifest()), encoding="utf-8"
            )
            arguments = sidecar.build_parser().parse_args(
                [
                    "serve",
                    "--project-root",
                    str(project),
                    "--runtime-root",
                    str(runtime),
                    "--launcher-version",
                    __version__,
                    "--workflow-protocol",
                    "1",
                ]
            )
            original_cwd = Path.cwd()
            try:
                with patch.dict(
                    os.environ,
                    {
                        "BYO_MCP_ARTIFACT_ROOT": str(root / "ambient-project"),
                        "PYOCD_MCP_RUNS_ROOT": str(root / "ambient-runs"),
                    },
                ):
                    context = sidecar._install_runtime_context(arguments, require_capsule=True)
                    self.assertEqual(context.project_root, project.resolve())
                    self.assertEqual(context.runtime_root, runtime.resolve())
                    self.assertEqual(os.environ["BYO_MCP_ARTIFACT_ROOT"], str(project.resolve()))
                    self.assertEqual(
                        os.environ["PYOCD_MCP_RUNS_ROOT"], str(project / ".firm" / "runs")
                    )
                    self.assertEqual(Path.cwd(), runtime.resolve())
            finally:
                os.chdir(original_cwd)

    def test_manual_permission_command_emits_one_json_result_and_refuses_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            project = root / "project"
            runtime = root / "runtime"
            project.mkdir()
            runtime.mkdir()
            (project / ".agent-workspace" / "runtime" / "manual-permissions").mkdir(parents=True)
            (project / ".agent-workspace" / "manifest.json").write_text(
                json.dumps({"schema": 1, "product": "byo", "workflow_protocol": 1}),
                encoding="utf-8",
            )
            (runtime / "release-manifest.json").write_text(
                json.dumps(self._release_manifest()), encoding="utf-8"
            )
            permissions = project / ".agent-workspace" / "runtime" / "manual-permissions"
            (permissions / "epoch.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "server_run_id": "server-run-1",
                        "created_at": "2026-09-17T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            for action in ("downgrade", "mass-erase"):
                (permissions / f"{action}.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "action": action,
                            "state": "locked",
                            "server_run_id": "server-run-1",
                            "grant_id": None,
                            "board_id": None,
                            "policy_digest": None,
                            "binding_digest": None,
                            "claim_id": None,
                            "created_at": "2026-09-17T00:00:00Z",
                        }
                    ),
                    encoding="utf-8",
                )
            command = [
                "manual-permission",
                "--project-root",
                str(project),
                "--runtime-root",
                str(runtime),
                "--launcher-version",
                __version__,
                "--workflow-protocol",
                "1",
                "--action",
                "downgrade",
                "--board-id",
                "board-1",
                "--policy-digest",
                "a" * 64,
            ]
            original_cwd = Path.cwd()
            try:
                first = io.StringIO()
                with contextlib.redirect_stdout(first):
                    self.assertEqual(sidecar.main(command), 0)
                second = io.StringIO()
                with contextlib.redirect_stdout(second):
                    self.assertEqual(sidecar.main(command), 1)
            finally:
                os.chdir(original_cwd)

            self.assertEqual(len(first.getvalue().splitlines()), 1)
            self.assertEqual(json.loads(first.getvalue())["status"], "manual_grant_unlocked")
            self.assertEqual(len(second.getvalue().splitlines()), 1)
            self.assertEqual(json.loads(second.getvalue())["code"], "manual/already-reserved")


if __name__ == "__main__":
    unittest.main()
